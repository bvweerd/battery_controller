"""Conftest for Battery Controller tests."""

from __future__ import annotations

import pytest


# Enable loading of custom integrations for all tests
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Automatically enable custom integration."""
    return enable_custom_integrations
