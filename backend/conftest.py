import sys
import pytest


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Any test that enters `with TestClient(app)` runs the lifespan shutdown hook, which sets the
    process-wide shutting-down flag; without clearing it every later _run_crawl exits immediately."""
    yield
    m = sys.modules.get("backend.main") or sys.modules.get("main")
    if m is not None:
        m._shutting_down.clear()
