"""Flask entrypoint for the quiz server."""

import socket
import sys
from pathlib import Path

# Ensure project root on sys.path for direct execution
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app import create_app  # type: ignore

HOST = "0.0.0.0"
PORT = 8000

app = create_app()


def _collect_server_urls() -> list[str]:
    urls: list[str] = [f"http://127.0.0.1:{PORT}"]
    seen: set[str] = {"127.0.0.1"}
    hostnames: set[str] = {socket.gethostname(), socket.getfqdn()}
    for hostname in hostnames:
        if not hostname:
            continue
        try:
            infos = socket.getaddrinfo(hostname, PORT, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except OSError:
            continue
        for info in infos:
            ip = str(info[4][0] or "")
            if not ip or ip.startswith("127.") or ip in seen:
                continue
            seen.add(ip)
            urls.append(f"http://{ip}:{PORT}")
    return urls


def _print_server_urls(server_name: str):
    print(f"{server_name} server is starting...")
    print("Available URLs:")
    for url in _collect_server_urls():
        print(f"  {url}")
    print("")


def run():
    try:
        from waitress import serve
    except ImportError:
        _print_server_urls("Flask")
        app.run(host=HOST, port=PORT, threaded=True)
        return
    _print_server_urls("Waitress")
    serve(app, host=HOST, port=PORT, threads=16)


if __name__ == "__main__":
    run()
