from __future__ import annotations

import os
from pathlib import Path

from .apps import create_public_app
from .web_serving import WebServingApp

app = WebServingApp(
    create_public_app(),
    Path(os.environ.get("CONTEXT_GATEWAY_WEB_DIR", "/app/web-dist")),
)
