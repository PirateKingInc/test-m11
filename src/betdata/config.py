"""Settings loaded from .env (secrets, DB URL) and config/sources.yaml (quotas, schedules)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_URL = f"sqlite:///{PROJECT_ROOT / 'data' / 'betdata.sqlite'}"


@dataclass(frozen=True)
class QuotaPolicy:
    """Free-tier limit for one API. `period` is "month" or "day"."""

    limit: int
    period: str = "month"
    reset_day: int = 1  # day of month the monthly quota resets
    safety_margin: float = 0.10  # fraction of `limit` we never spend


@dataclass(frozen=True)
class SourceConfig:
    name: str
    enabled: bool = True
    api_key_env: str | None = None
    quota: QuotaPolicy | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class Settings:
    db_url: str
    sources: dict[str, SourceConfig]

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError:
            raise KeyError(f"source {name!r} is not configured in sources.yaml") from None


def _parse_sources(raw: dict[str, Any]) -> dict[str, SourceConfig]:
    out = {}
    for name, body in (raw or {}).items():
        body = dict(body or {})
        quota = body.pop("quota", None)
        out[name] = SourceConfig(
            name=name,
            enabled=body.pop("enabled", True),
            api_key_env=body.pop("api_key_env", None),
            quota=QuotaPolicy(**quota) if quota else None,
            options=body,
        )
    return out


def load_settings(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or PROJECT_ROOT / ".env", override=False)
    path = config_path or Path(
        os.environ.get("BETDATA_CONFIG", PROJECT_ROOT / "config" / "sources.yaml")
    )
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    return Settings(
        db_url=os.environ.get("BETDATA_DB_URL", DEFAULT_DB_URL),
        sources=_parse_sources(raw.get("sources", {})),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
