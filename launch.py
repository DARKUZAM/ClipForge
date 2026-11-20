from __future__ import annotations
import socket
import threading
import time
import urllib.request

import uvicorn

from app import app


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    port = available_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, log_config=None)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="clipforge-api")
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=0.5).close()
            break
        except Exception:
            time.sleep(0.15)
    try:
        import webview
    except ImportError as exc:
        server.should_exit = True
        raise RuntimeError("ClipForge desktop window is unavailable. Start the app with Run ClipForge.bat to install its desktop shell.") from exc
    try:
        webview.create_window("ClipForge · Local AI Studio", url, width=1440, height=940, min_size=(960, 680), background_color="#0d1013")
        webview.start(debug=False)
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
