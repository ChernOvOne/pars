"""Tests for whitelist parsing, merging and caching."""

from ipaddress import ip_network

import httpx
import respx

from wlfinder.config import WhitelistConfig
from wlfinder.whitelist.store import WhitelistStore, parse_lines


def test_parse_cidr_bare_ip_and_ipv6(sample_whitelist_lines: list[str]) -> None:
    nets = parse_lines(sample_whitelist_lines)
    assert ip_network("192.168.0.0/24") in nets
    assert ip_network("203.0.113.7/32") in nets  # bare IP -> /32
    assert ip_network("2001:db8::/32") in nets  # surrounding whitespace tolerated


def test_parse_skips_comments_and_blanks() -> None:
    nets = parse_lines(["# comment", "   ", "", "8.8.8.0/24"])
    assert nets == [ip_network("8.8.8.0/24")]


def test_parse_skips_garbage() -> None:
    nets = parse_lines(["not-an-ip", "999.999.999.999", "1.2.3.0/24"])
    assert nets == [ip_network("1.2.3.0/24")]


def test_inline_comment_stripped() -> None:
    nets = parse_lines(["1.2.3.0/24  # mts"])
    assert nets == [ip_network("1.2.3.0/24")]


def _cfg(*sources: dict[str, str]) -> WhitelistConfig:
    return WhitelistConfig.model_validate({"sources": list(sources), "refresh_ttl_hours": 24})


@respx.mock
async def test_store_merges_and_dedups(tmp_path) -> None:
    respx.get("https://example.test/a.txt").mock(
        return_value=httpx.Response(200, text="1.0.0.0/24\n2.0.0.0/24\n")
    )
    respx.get("https://example.test/b.txt").mock(
        return_value=httpx.Response(200, text="2.0.0.0/24\n3.0.0.0/24\n# dup of above\n")
    )
    cfg = _cfg(
        {"type": "github", "name": "a", "url": "https://example.test/a.txt"},
        {"type": "github", "name": "b", "url": "https://example.test/b.txt"},
    )
    async with httpx.AsyncClient() as client:
        store = WhitelistStore(cfg, tmp_path, client)
        checker = await store.get_checker(force=True)

    assert checker.is_whitelisted("1.0.0.1")
    assert checker.is_whitelisted("2.0.0.1")
    assert checker.is_whitelisted("3.0.0.1")
    assert not checker.is_whitelisted("4.0.0.1")
    # 1/24 + 2/24 + 3/24 collapse to a single 1.0.0.0/23 + 3.0.0.0/24 (<= 2 nets)
    assert checker.network_count < 4
    assert store.cache_path.exists()


@respx.mock
async def test_store_serves_from_cache_within_ttl(tmp_path) -> None:
    route = respx.get("https://example.test/a.txt").mock(
        return_value=httpx.Response(200, text="9.0.0.0/24\n")
    )
    cfg = _cfg({"type": "github", "name": "a", "url": "https://example.test/a.txt"})
    async with httpx.AsyncClient() as client:
        store = WhitelistStore(cfg, tmp_path, client)
        await store.get_checker(force=True)
        assert route.call_count == 1
        # second call within TTL must be served from the pickle cache
        checker = await store.get_checker()
        assert route.call_count == 1
        assert checker.is_whitelisted("9.0.0.1")


@respx.mock
async def test_twl_subnets_source_filters_by_percent(tmp_path) -> None:
    respx.get("https://example.test/subnets.json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"cidr": "10.0.0.0/24", "percent": 80.0, "ips": []},
                {"cidr": "10.0.1.0/24", "percent": 20.0, "ips": []},
                {"cidr": "10.0.2.0/24", "percent": 55.0, "ips": []},
                {"count": 1, "percent": 99.0},  # malformed entry — no cidr, skipped
            ],
        )
    )
    cfg = _cfg(
        {
            "type": "twl_subnets",
            "name": "twl",
            "url": "https://example.test/subnets.json",
            "min_percent": 50,
        }
    )
    async with httpx.AsyncClient() as client:
        store = WhitelistStore(cfg, tmp_path, client)
        checker = await store.get_checker(force=True)

    assert checker.is_whitelisted("10.0.0.5")  # 80% >= 50
    assert checker.is_whitelisted("10.0.2.5")  # 55% >= 50
    assert not checker.is_whitelisted("10.0.1.5")  # 20% < 50
