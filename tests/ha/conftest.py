"""Fixtures for the Home Assistant based tests.

These need ``pytest-homeassistant-custom-component``; without it they are
skipped, so ``pytest tests/`` keeps working without a Home Assistant install.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load integrations from custom_components/."""
    return
