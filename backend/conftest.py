import os
import sys
import tempfile
import pytest

# One fresh index directory per test session: both test modules used to share /tmp/sairo-test across
# runs, so every crawl test had to unlink its own DB files first and results depended on run order.
os.environ["DB_DIR"] = tempfile.mkdtemp(prefix="sairo-test-")


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Any test that enters `with TestClient(app)` runs the lifespan shutdown hook, which sets the
    process-wide shutting-down flag; without clearing it every later _run_crawl exits immediately."""
    yield
    m = sys.modules.get("backend.main") or sys.modules.get("main")
    if m is not None:
        m._shutting_down.clear()
