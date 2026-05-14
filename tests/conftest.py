"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_whitelist_lines() -> list[str]:
    """A whitelist file exercising every parsing edge case."""
    return [
        "# Russian mobile whitelist (sample)",
        "192.168.0.0/24",
        "10.0.0.0/8",
        "",
        "203.0.113.7        # a bare IP -> /32",
        "  2001:db8::/32  ",
        "garbage-not-an-ip",
        "192.168.0.128/25   # overlaps 192.168.0.0/24, should collapse",
    ]
