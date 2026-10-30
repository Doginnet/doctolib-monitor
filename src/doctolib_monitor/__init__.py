"""Entry point bridge for `uv run doctolib-monitor` CLI and `uv run doctolib-web` FastAPI UI."""

from src.main import main
from src.web.api import run_server as run_web

__all__ = ["main", "run_web"]
