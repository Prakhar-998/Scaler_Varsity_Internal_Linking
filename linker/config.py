"""Loads YAML config and environment secrets into one object."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass
class Program:
    id: str
    name: str
    url: str
    categories: list[str]
    keywords: list[str]


@dataclass
class Config:
    settings: dict
    programs: list[Program]
    adjacent: dict[str, list[str]]
    pillar_urls: set[str]
    keyword_overrides: dict[str, list[str]]
    secrets: dict = field(default_factory=dict)

    def get(self, *path, default=None):
        node = self.settings
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def dry_run(self) -> bool:
        return self.secrets.get("DRY_RUN", "true").lower() != "false"

    def program_for_category(self, category: str) -> Program | None:
        for p in self.programs:
            if category in p.categories:
                return p
        return None


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(require_secrets: bool = True) -> Config:
    settings = _load_yaml("settings.yaml")
    prog = _load_yaml("programs.yaml")

    programs = [
        Program(
            id=p["id"], name=p["name"], url=(p.get("url") or "").strip(),
            categories=p.get("categories", []), keywords=p.get("keywords", []),
        )
        for p in prog.get("programs", [])
    ]

    names = [
        "GOOGLE_SERVICE_ACCOUNT_JSON", "MASTERSHEET_ID", "CONTROL_SHEET_ID",
        "WP_USERNAME", "WP_APP_PASSWORD", "GEMINI_API_KEY", "GEMINI_MODEL", "DRY_RUN",
    ]
    secrets = {n: os.environ[n] for n in names if os.environ.get(n)}
    if require_secrets:
        required = names[:6]  # GEMINI_MODEL and DRY_RUN are optional
        missing = [n for n in required if n not in secrets]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
        json.loads(secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])  # fail fast on a malformed key

    return Config(
        settings=settings,
        programs=programs,
        adjacent=prog.get("adjacent_categories", {}) or {},
        pillar_urls={u.rstrip("/") + "/" for u in (prog.get("pillar_urls") or [])},
        keyword_overrides={k.rstrip("/") + "/": v for k, v in (prog.get("keyword_overrides") or {}).items()},
        secrets=secrets,
    )
