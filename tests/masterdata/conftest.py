from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.masterdata import inspect, load_recon_lines, load_snapshot

FIXTURE_DIR = ROOT / "fixtures" / "masterdata"


@pytest.fixture(scope="session")
def clean_snapshot():
    return load_snapshot(FIXTURE_DIR / "clean.json")


@pytest.fixture(scope="session")
def dirty_snapshot():
    return load_snapshot(FIXTURE_DIR / "dirty.json")


@pytest.fixture(scope="session")
def clean_report(clean_snapshot):
    return inspect(clean_snapshot)


@pytest.fixture(scope="session")
def dirty_report(dirty_snapshot):
    return inspect(dirty_snapshot)


@pytest.fixture(scope="session")
def recon_lines():
    return load_recon_lines(FIXTURE_DIR / "recon_lines_sample.json")
