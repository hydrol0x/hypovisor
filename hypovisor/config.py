"""Runtime configuration, read from environment variables (and a local .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so we do not need python-dotenv. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("HYPOVISOR_DATABASE_URL", "sqlite:///./data/hypovisor.db")
    )
    timezone: str = field(default_factory=lambda: _env("HYPOVISOR_TIMEZONE", "America/New_York"))
    photo_dir: Path = field(default_factory=lambda: Path(_env("HYPOVISOR_PHOTO_DIR", "./data/photos")))
    target_low: float = field(default_factory=lambda: float(_env("HYPOVISOR_TARGET_LOW", "70")))
    target_high: float = field(default_factory=lambda: float(_env("HYPOVISOR_TARGET_HIGH", "180")))
    dia_minutes: int = field(default_factory=lambda: int(_env("HYPOVISOR_DIA_MINUTES", "300")))
    insulin_peak_minutes: int = field(
        default_factory=lambda: int(_env("HYPOVISOR_INSULIN_PEAK_MINUTES", "75"))
    )
    preferred_cgm_source: str = field(
        default_factory=lambda: _env("HYPOVISOR_PREFERRED_CGM_SOURCE", "dexcom_clarity")
    )
    anthropic_model: str = field(default_factory=lambda: _env("HYPOVISOR_ANTHROPIC_MODEL", "claude-opus-5-5"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    usda_api_key: str = field(default_factory=lambda: _env("USDA_FDC_API_KEY", ""))
    nightscout_url: str = field(default_factory=lambda: _env("NIGHTSCOUT_URL", ""))
    nightscout_token: str = field(default_factory=lambda: _env("NIGHTSCOUT_TOKEN", ""))
    dexcom_client_id: str = field(default_factory=lambda: _env("DEXCOM_CLIENT_ID", ""))
    dexcom_client_secret: str = field(default_factory=lambda: _env("DEXCOM_CLIENT_SECRET", ""))
    dexcom_redirect_uri: str = field(
        default_factory=lambda: _env("DEXCOM_REDIRECT_URI", "http://localhost:8000/api/sync/dexcom/callback")
    )
    dexcom_base_url: str = field(default_factory=lambda: _env("DEXCOM_BASE_URL", "https://api.dexcom.com"))


settings = Settings()
