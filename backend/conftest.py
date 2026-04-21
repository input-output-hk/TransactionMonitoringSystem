import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Tests boot the FastAPI app via TestClient with no API keys configured.
# Production refuses to start in that configuration; the test environment
# opts into dev mode explicitly.
os.environ.setdefault("TMS_ALLOW_DEV_MODE", "1")
