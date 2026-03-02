"""Flask entrypoint for the quiz server."""

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


def run():
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    run()
