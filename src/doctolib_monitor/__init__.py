"""Entry point bridges for `doctolib-bot`, `doctolib-cli`, and `doctolib-web`."""

from src.cli import run_cli
from src.main import main
from src.web.api import run_server as run_web

__all__ = ["main", "run_cli", "run_web"]
