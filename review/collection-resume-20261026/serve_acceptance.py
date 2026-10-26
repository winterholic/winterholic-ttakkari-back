"""Serve the real API against this Run's disposable DB and existing live receipts."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # applies only the existing integration-test configuration
import uvicorn

conftest.get_settings().cors_origins = ["http://127.0.0.1:5213"]
for middleware in conftest.app.user_middleware:
    if middleware.cls.__name__ == "CORSMiddleware":
        middleware.kwargs["allow_origins"] = ["http://127.0.0.1:5213"]
uvicorn.run(conftest.app, host="127.0.0.1", port=9231, log_level="warning")
