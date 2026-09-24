"""Vercel entrypoint for Wayback Website Archiver."""

import sys
from pathlib import Path

# Add the web application folder to Python path
app_dir = Path(__file__).resolve().parent / "wayback-website-archiver"
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

from app import app
