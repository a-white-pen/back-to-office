"""Load optional private employer integrations without publishing their targets.

Generic hiring-platform support remains in the public modules. The optional
private module supplies discovery, search and vacancy-reading hooks.
"""

import importlib.util
from functools import lru_cache

from bto import settings


@lru_cache(maxsize=1)
def _module():
    path = settings.private_dir() / "research_job_listing" / "adapters.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("bto_private_research_adapters", path)
    if spec is None or spec.loader is None:
        raise ValueError("Private research adapters cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("script_shell", "read_vacancy", "discover_boards", "search_board"):
        if not callable(getattr(module, name, None)):
            raise ValueError("Private research adapters have a missing hook")
    return module


def script_shell(url):
    module = _module()
    return bool(module and module.script_shell(url))


def read_vacancy(url):
    module = _module()
    return module.read_vacancy(url) if module else None


def discover_boards(markup, origin):
    module = _module()
    return module.discover_boards(markup, origin) if module else []


def search_board(board, listing, calls):
    module = _module()
    return module.search_board(board, listing, calls) if module else None
