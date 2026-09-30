import asyncio
import os
import pty
import re
import select
import signal
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Parker Dashboard")
BASE_DIR = Path(__file__).resolve().parent

def parse_env_line(line):
    """
    Parses one .env line into (key, value), or None for blanks/comments.
    Supports `export KEY=v`, quoted values, and trailing ` # comments` on unquoted values.
    NOTE: identical copy of parse_env_line() in ../parker.py (tests keep them in sync).
    """
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None

    if line.startswith("export "):
        line = line[len("export "):].lstrip()

    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()

    if not key:
        return None

    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end != -1:
            return key, value[1:end]
    elif value.startswith("#"):
        value = ""
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()

    return key, value

def load_env(file_path=".env"):
    """Simple native .env loader to avoid extra dependencies."""
    env_path = Path(file_path)
    if env_path.is_file():
        with env_path.open("r") as f:
            for line in f:
                parsed = parse_env_line(line)
                if parsed:
                    os.environ[parsed[0]] = parsed[1]

# Load environment variables (check parent directory first, fall back to current directory)
parent_env = BASE_DIR.parent / ".env"
if parent_env.is_file():
    load_env(parent_env)
else:
    load_env(BASE_DIR / ".env")


# Setup templates and static files
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Configuration — derived from project layout, overridable via .env
PARKER_ROOT = BASE_DIR.parent
PARKER_SCRIPT_PATH = os.getenv("PARKER_SCRIPT_PATH", str(PARKER_ROOT / "parker.py"))
PARKER_VENV_PYTHON = os.getenv("PARKER_VENV_PYTHON", str(PARKER_ROOT / "venv" / "bin" / "python3"))


def allowed_origins():
    """Extra origins (comma separated) allowed to open the terminal WebSocket."""
    raw = os.getenv("PARKER_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/").lower() for item in raw.split(",") if item.strip()}


def origin_allowed(origin, host):
    """
    The terminal WebSocket runs provisioning as root, so it must only be reachable
    from this dashboard's own pages. Browsers always send Origin on WebSocket
    handshakes; a missing or foreign Origin is rejected (cross-site WebSocket hijacking).
    """
    if not origin:
        return False

    origin = origin.strip().rstrip("/").lower()

    if origin in allowed_origins():
        return True

    try:
        origin_host = urlsplit(origin).netloc
    except ValueError:
        return False

    return bool(origin_host) and bool(host) and origin_host == host.strip().lower()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request})


@app.websocket("/ws/terminal")
async def terminal_session(websocket: WebSocket):
    """Run parker.py in a pseudo-terminal and bridge it to the browser."""
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        # Closing before accept() makes the handshake fail with HTTP 403.
        await websocket.close(code=1008)
        return

    await websocket.accept()
    process = None
    master_fd = None

    async def send_output():
        nonlocal process, master_fd
        loop = asyncio.get_running_loop()
        while process and process.poll() is None:
            ready, _, _ = await loop.run_in_executor(
                None,
                lambda: select.select([master_fd], [], [], 0.1)
            )
            if not ready:
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            await websocket.send_json({
                "type": "output",
                "data": data.decode("utf-8", errors="replace")
            })

        if process:
            return_code = process.wait()
            await websocket.send_json({"type": "exit", "code": return_code})

    try:
        start = await websocket.receive_json()
        dry_run = bool(start.get("dry_run", False))

        cmd = ["sudo", "-n", PARKER_VENV_PYTHON, PARKER_SCRIPT_PATH]
        if dry_run:
            cmd.append("--dry-run")

        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)

        async def receive_input():
            while process.poll() is None:
                message = await websocket.receive_json()
                if message.get("type") == "input":
                    value = str(message.get("data", ""))
                    os.write(master_fd, (value + "\n").encode("utf-8"))
                elif message.get("type") == "interrupt":
                    process.send_signal(signal.SIGINT)

        output_task = asyncio.create_task(send_output())
        input_task = asyncio.create_task(receive_input())
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        for task in done:
            task.result()

    except WebSocketDisconnect:
        pass
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass


if __name__ == "__main__":
    import uvicorn
    # Loopback only: the dashboard is exposed exclusively through the Cloudflare Tunnel.
    uvicorn.run(app, host="127.0.0.1", port=9000)
