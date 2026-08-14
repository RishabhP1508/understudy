"""Settings and policy loading. No secrets in code; config comes from the environment and YAML."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class Settings(BaseModel):
    gemini_api_key: str | None = None
    policy_path: Path


def load_settings(policy_path: Path) -> Settings:
    """Build Settings from the environment and a policy path. Reads GEMINI_API_KEY only."""
    return Settings(gemini_api_key=os.environ.get("GEMINI_API_KEY"), policy_path=policy_path)


def load_policy(path: Path) -> dict[str, Any]:
    """Load a policy YAML file into a plain dict."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
