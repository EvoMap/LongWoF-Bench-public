from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval"
POOL_ROOT = REPO_ROOT / "tasks_final"
MANIFEST_PATH = POOL_ROOT / "manifest.json"

if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def pool_root() -> Path:
    return POOL_ROOT


@pytest.fixture(scope="session")
def manifest_payload() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def manifest_rows(manifest_payload: dict) -> list[dict]:
    return manifest_payload["tasks"]
