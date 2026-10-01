"""The terminal WebSocket runs provisioning as root: only the dashboard's own pages may open it."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

MAIN_PATH = Path(__file__).resolve().parent.parent / "parker-ui" / "main.py"


@pytest.fixture
def ui(monkeypatch):
    monkeypatch.delenv("PARKER_ALLOWED_ORIGINS", raising=False)
    spec = importlib.util.spec_from_file_location("parker_ui_main_ws", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Never spawn the real (sudo) provisioning process from a test.
    def no_spawn(*a, **k):
        raise AssertionError("the terminal process must not start in these tests")
    monkeypatch.setattr(module.subprocess, "Popen", no_spawn)
    return module


@pytest.mark.parametrize("origin,host,ok", [
    ("https://parker.example.com", "parker.example.com", True),
    ("https://PARKER.example.com/", "parker.example.com", True),
    ("http://127.0.0.1:9000", "127.0.0.1:9000", True),
    ("https://evil.example.net", "parker.example.com", False),
    ("https://parker.example.com.evil.net", "parker.example.com", False),
    ("https://parker.example.com:8443", "parker.example.com", False),   # different port = different origin
    ("null", "parker.example.com", False),
    ("", "parker.example.com", False),
    (None, "parker.example.com", False),
    ("https://parker.example.com", None, False),
])
def test_origin_allowed(ui, origin, host, ok):
    assert ui.origin_allowed(origin, host) is ok


def test_extra_origins_from_the_environment(ui, monkeypatch):
    monkeypatch.setenv("PARKER_ALLOWED_ORIGINS", "https://parker.example.com, https://alt.example.com/")
    # Host header rewritten by a proxy: same-origin comparison would fail, the allow-list still works.
    assert ui.origin_allowed("https://parker.example.com", "127.0.0.1:9000") is True
    assert ui.origin_allowed("https://alt.example.com", "127.0.0.1:9000") is True
    assert ui.origin_allowed("https://evil.example.net", "127.0.0.1:9000") is False


def test_websocket_from_a_foreign_origin_is_refused(ui):
    client = TestClient(ui.app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/terminal", headers={"origin": "https://evil.example.net"}):
            pass
    assert exc.value.code == 1008


def test_websocket_without_an_origin_is_refused(ui):
    client = TestClient(ui.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/terminal"):
            pass


def test_websocket_from_the_dashboard_itself_is_accepted(ui):
    client = TestClient(ui.app)
    with client.websocket_connect("/ws/terminal", headers={"origin": "http://testserver"}) as ws:
        ws.close()          # connected; leave before sending "start" so nothing is spawned


def test_index_page_still_renders(ui):
    r = TestClient(ui.app).get("/")
    assert r.status_code == 200 and "Parker" in r.text


def test_dashboard_starts_when_env_is_not_readable(ui, tmp_path, monkeypatch):
    """The installer makes .env root-only; the dashboard (a different user) must still start."""
    env_file = tmp_path / ".env"
    env_file.write_text("PARKER_T_SECRET=1\n")

    def denied(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ui.Path, "open", denied)
    monkeypatch.delenv("PARKER_T_SECRET", raising=False)

    ui.load_env(env_file)                      # must not raise

    import os
    assert "PARKER_T_SECRET" not in os.environ
