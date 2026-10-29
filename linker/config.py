"""Loads config files (settings.yaml, categories.yaml, program_pages.csv) and environment
secrets into one object."""
from __future__ import annotations

import csv
import json
from urllib.parse import urlsplit, urlunsplit
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


def _split(cell: str) -> list[str]:
    return [x.strip() for x in (cell or "").split("|") if x.strip()]


def clean_url(url: str) -> str:
    """Drop tracking query strings/fragments (utm_*, gclid...) but keep the path exactly as given,
    so the link we insert matches the live page (with or without a trailing slash)."""
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", "")) if parts.netloc else ""


def load_programs(path: Path) -> list[Program]:
    """Program landing pages from a CSV anyone can edit (GitHub's web editor works fine).
    Columns: id, active, name, url, categories, anchors. Multiple values separated by '|'.
    Rows with active != yes are ignored, so a program can be paused without deleting it."""
    programs, seen = [], set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("id"):
                continue
            if row.get("active", "yes").lower() not in {"yes", "y", "true", "1"}:
                continue
            if row["id"] in seen:
                raise SystemExit(f"{path.name} line {i}: duplicate id '{row['id']}'")
            seen.add(row["id"])
            programs.append(Program(
                id=row["id"], name=row.get("name") or row["id"], url=clean_url(row.get("url", "")),
                categories=_split(row.get("categories", "")), keywords=_split(row.get("anchors", "")),
            ))
    return programs


def load_config(require_secrets: bool = True) -> Config:
    settings = _load_yaml("settings.yaml")
    prog = _load_yaml("categories.yaml")
    programs = load_programs(CONFIG_DIR / "program_pages.csv")

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
