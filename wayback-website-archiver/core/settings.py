"""Settings management — load/save from JSON, env-var fallbacks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

if os.environ.get("VERCEL"):
    SETTINGS_FILE = Path("/tmp/data/settings.json")
else:
    SETTINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"


@dataclass
class Settings:
    # Internet Archive credentials
    access_key: str = ""
    secret_key: str = ""

    # Discovery
    max_pages: int = 500
    max_depth: int = 3
    check_sitemap: bool = True
    check_navigation: bool = True
    follow_internal_links: bool = True

    # Archiving
    archive_delay: int = 10
    request_timeout: int = 60
    retries: int = 3
    check_existing_snapshots: bool = True
    skip_existing_days: int = 30

    # Filtering
    include_external: bool = False

    def has_credentials(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Never expose secret key fully
        return d

    def to_safe_dict(self) -> dict:
        """Return settings with masked secret key."""
        d = asdict(self)
        if d.get("secret_key"):
            d["secret_key"] = d["secret_key"][:4] + "****" + d["secret_key"][-4:]
        return d


def load_settings() -> Settings:
    """Load settings from JSON file, with env-var overrides."""
    settings = Settings()

    # Load from file
    if SETTINGS_FILE.is_file():
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for key, val in data.items():
                if hasattr(settings, key):
                    setattr(settings, key, val)
        except Exception:
            pass

    # Env-var overrides for credentials
    env_access = os.environ.get("SAVEPAGENOW_ACCESS_KEY") or os.environ.get("IA_ACCESS_KEY")
    env_secret = os.environ.get("SAVEPAGENOW_SECRET_KEY") or os.environ.get("IA_SECRET_KEY")
    if env_access:
        settings.access_key = env_access
    if env_secret:
        settings.secret_key = env_secret

    return settings


def save_settings(settings: Settings) -> None:
    """Persist settings to JSON file."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    with SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
