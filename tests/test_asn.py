"""Tests for ASN prefix fetching and whitelist-overlap computation."""

from ipaddress import ip_network

import httpx
import respx

from wlfinder.asn import (
    DEFAULT_ASNS,
    AsnStore,
    compute_overlap,
    parse_asn_prefixes,
    resolve_asns,
)
from wlfinder.checker import WhitelistChecker

IPVERSE = "https://raw.githubusercontent.com/ipverse/asn-ip/master/as/{}/ipv4-aggregated.txt"


def test_parse_asn_prefixes_strips_comments_and_garbage() -> None:
    text = "# AS9123 (TIMEWEB-AS)\n# JSC TIMEWEB\n#\n2.59.40.0/22\nnonsense\n5.42.96.0/20\n"
    nets = parse_asn_prefixes(text)
    assert nets == [ip_network("2.59.40.0/22"), ip_network("5.42.96.0/20")]


def test_resolve_asns_default_and_override() -> None:
    assert resolve_asns("timeweb", {}) == DEFAULT_ASNS["timeweb"]
    assert resolve_asns("timeweb", {"asns": [111, 222]}) == [111, 222]
    assert resolve_asns("unknown-type", {}) == []


def test_compute_overlap_collapses_and_measures() -> None:
    checker = WhitelistChecker(
        [ip_network("10.0.0.0/25"), ip_network("192.168.0.0/24")]
    )
    prefixes = [
        ip_network("10.0.0.0/24"),
        ip_network("10.0.1.0/24"),  # adjacent -> collapses with the above into /23
        ip_network("192.168.0.0/24"),
    ]
    overlap = compute_overlap("timeweb-spb", [9123], prefixes, checker)

    assert overlap.total_prefixes == 2  # 10.0.0.0/23 + 192.168.0.0/24
    assert overlap.announced_addresses == 512 + 256
    assert overlap.whitelisted_addresses == 128 + 256  # /25 inside the /23, full /24
    assert overlap.percent == 50.0
    assert len(overlap.matched_prefixes) == 2


def test_compute_overlap_no_match() -> None:
    checker = WhitelistChecker([ip_network("203.0.113.0/24")])
    overlap = compute_overlap("h", [1], [ip_network("10.0.0.0/24")], checker)
    assert overlap.whitelisted_addresses == 0
    assert overlap.percent == 0.0
    assert overlap.matched_prefixes == []


@respx.mock
async def test_asn_store_fetches_and_caches(tmp_path) -> None:
    route = respx.get(IPVERSE.format(9123)).mock(
        return_value=httpx.Response(200, text="# AS9123\n2.59.40.0/22\n5.42.96.0/20\n")
    )
    async with httpx.AsyncClient() as client:
        store = AsnStore(tmp_path, client)
        first = await store.fetch_prefixes(9123)
        assert route.call_count == 1
        assert first == [ip_network("2.59.40.0/22"), ip_network("5.42.96.0/20")]
        # second call within TTL is served from the on-disk cache
        second = await store.fetch_prefixes(9123)
        assert route.call_count == 1
        assert second == first
        assert (tmp_path / "asn" / "9123.txt").exists()
