import asyncio
import os
import pty
import select
import signal
import subprocess
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Parker Dashboard")
BASE_DIR = Path(__file__).resolve().parent

def load_env(file_path=".env"):
    """Simple native .env loader to avoid extra dependencies."""
    env_path = Path(file_path)
    if env_path.is_file():
        with env_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()

# Load environment variables from .env file
load_env(BASE_DIR / ".env")

# Setup templates and static files
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Configuration — derived from project layout, overridable via .env
PARKER_ROOT = BASE_DIR.parent
PARKER_SCRIPT_PATH = os.getenv("PARKER_SCRIPT_PATH", str(PARKER_ROOT / "parker.py"))
PARKER_VENV_PYTHON = os.getenv("PARKER_VENV_PYTHON", str(PARKER_ROOT / "venv" / "bin" / "python3"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request})


@app.websocket("/ws/terminal")
async def terminal_session(websocket: WebSocket):
    """Run parker.py in a pseudo-terminal and bridge it to the browser."""
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
    uvicorn.run(app, host="0.0.0.0", port=9000)
