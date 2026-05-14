"""Tests for wlfinder.checker.WhitelistChecker."""

from ipaddress import ip_network

from wlfinder.checker import WhitelistChecker


def _checker(*cidrs: str) -> WhitelistChecker:
    return WhitelistChecker([ip_network(c) for c in cidrs])


def test_inside_network() -> None:
    c = _checker("192.168.0.0/24", "10.0.0.0/8")
    assert c.is_whitelisted("192.168.0.5")
    assert c.is_whitelisted("10.1.2.3")


def test_outside_network() -> None:
    c = _checker("192.168.0.0/24")
    assert not c.is_whitelisted("192.168.1.0")
    assert not c.is_whitelisted("8.8.8.8")


def test_edges() -> None:
    c = _checker("192.168.0.0/24")
    assert c.is_whitelisted("192.168.0.0")  # network address
    assert c.is_whitelisted("192.168.0.255")  # broadcast address
    assert not c.is_whitelisted("192.168.1.0")  # one past the end
    assert not c.is_whitelisted("192.167.255.255")  # one before the start


def test_empty_checker() -> None:
    c = WhitelistChecker([])
    assert not c.is_whitelisted("1.2.3.4")
    assert c.network_count == 0


def test_host_route() -> None:
    c = _checker("203.0.113.7/32")
    assert c.is_whitelisted("203.0.113.7")
    assert not c.is_whitelisted("203.0.113.8")


def test_ipv6() -> None:
    c = _checker("2001:db8::/32")
    assert c.is_whitelisted("2001:db8::1")
    assert not c.is_whitelisted("2001:db9::1")


def test_overlapping_and_adjacent_collapse() -> None:
    # 0/24 + 0.128/25 overlap; 0/24 + 1/24 are adjacent -> all collapse to 0.0/23
    c = _checker("192.168.0.0/24", "192.168.0.128/25", "192.168.1.0/24")
    assert c.network_count == 1
    assert c.is_whitelisted("192.168.0.200")
    assert c.is_whitelisted("192.168.1.1")
    assert not c.is_whitelisted("192.168.2.1")


def test_whitespace_tolerated() -> None:
    c = _checker("10.0.0.0/8")
    assert c.is_whitelisted("  10.0.0.1  ")
