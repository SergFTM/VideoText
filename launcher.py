"""VideoText desktop launcher.

Double-click entry point for the packaged Windows build. Responsibilities:
  1. Start uvicorn in a background thread.
  2. Wait for /health to return 200.
  3. Open the default browser on http://localhost:<PORT>.
  4. Keep the process alive; quit via tray-icon → Quit, or Ctrl+C in console.

Packaging notes:
  - PyInstaller bundles this with `--onedir` and `--console` so first-time
    users see any startup errors instead of a silent crash.
  - Paths inside the bundle are resolved via sys._MEIPASS (see `_app_root()`).
  - Port is 8000 by default but auto-falls back to the next free port to
    avoid conflicts with another local dev server.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8000


# ── Helpers ────────────────────────────────────────────────────────

def _app_root() -> Path:
    """Directory where bundled data files live.

    Inside a PyInstaller one-dir build: the folder next to launcher.exe.
    Running from source: the repo root (parent of this file).
    """
    if getattr(sys, "frozen", False):
        # PyInstaller sets sys._MEIPASS only for --onefile. For --onedir
        # the exe sits next to the _internal folder — data is at sys.executable's dir.
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _pick_port(preferred: int) -> int:
    """Return `preferred` if free; else walk forward until we find a free port."""
    for candidate in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError(f"no free port found starting from {preferred}")


def _wait_for_health(port: int, timeout_s: int = 60) -> bool:
    """Poll /health until 200 OK or timeout. Returns True on success."""
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ── uvicorn runner ─────────────────────────────────────────────────

def _run_server(port: int) -> None:
    """Run uvicorn on the main process; blocks forever."""
    os.chdir(_app_root())
    import uvicorn  # imported here so startup errors surface in console
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


# ── Main ──────────────────────────────────────────────────────────

def main() -> int:
    port = _pick_port(DEFAULT_PORT)
    print(f"[VideoText] starting on http://127.0.0.1:{port}")

    # Browser opener runs in a background thread so main can block in uvicorn.
    def open_browser_when_ready() -> None:
        if _wait_for_health(port):
            webbrowser.open(f"http://127.0.0.1:{port}/")
        else:
            print("[VideoText] server did not become healthy in 60s — check the log above")

    threading.Thread(target=open_browser_when_ready, daemon=True).start()

    try:
        _run_server(port)
    except KeyboardInterrupt:
        print("[VideoText] bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
