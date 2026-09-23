"""Demo hosts have no `requests` and no signlab-client-monitor package, and a
missing dependency must never stop the scheduler (stijn: python-scheduler
crash-looped on `import requests` via python_client.py)."""
import importlib
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONCRON_STATE_DIR", str(tmp_path))
    root = logging.getLogger()
    saved = list(root.handlers)
    yield tmp_path
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
            h.close()


def reimport(monkeypatch, name, block):
    for mod in block:
        monkeypatch.setitem(sys.modules, mod, None)  # `import mod` raises ImportError
    monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module(name)


@pytest.mark.parametrize("block", [
    ("requests", "signlab_client_monitor"),                   # stijn
    ("requests", "signlab_client_monitor", "python_client"),  # stdlib fallback
])
def test_scheduler_starts_logging_without_dependencies(fresh, monkeypatch, block):
    monkeypatch.delitem(sys.modules, "python_client", raising=False)
    reimport(monkeypatch, "scheduler_v2", block)
    logging.getLogger("scheduler_v2").info("up")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "scheduler_v2 - INFO - up" in (fresh / "scheduler_v2.log").read_text()


def test_move_studiofiles_stdlib_fallback(fresh, monkeypatch):
    reimport(monkeypatch, "move_studiofiles",
             ("requests", "signlab_client_monitor", "python_client"))
    assert (fresh / "logs" / "move_videos.log").exists()


def test_vendored_client_imports_without_requests(monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", None)
    monkeypatch.delitem(sys.modules, "python_client", raising=False)
    import python_client
    assert python_client.requests is None
    assert callable(python_client.setup_rotating_logger)
