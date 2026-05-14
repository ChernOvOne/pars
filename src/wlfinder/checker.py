"""CIDR matching: decide whether an IP falls into the whitelist."""

from __future__ import annotations

import bisect
from ipaddress import IPv4Network, IPv6Network, collapse_addresses, ip_address

Network = IPv4Network | IPv6Network


def _collapse_v4(nets: list[IPv4Network]) -> list[IPv4Network]:
    return list(collapse_addresses(nets)) if nets else []


def _collapse_v6(nets: list[IPv6Network]) -> list[IPv6Network]:
    return list(collapse_addresses(nets)) if nets else []


class WhitelistChecker:
    """O(log n) membership test over a set of networks.

    The networks are collapsed (merged + sorted) once at construction time,
    so they are non-overlapping and ordered. Each lookup is then a binary
    search for the candidate network followed by a single containment check.
    IPv4 and IPv6 are kept in separate lists.
    """

    def __init__(self, networks: list[Network]) -> None:
        v4 = sorted(n for n in networks if isinstance(n, IPv4Network))
        v6 = sorted(n for n in networks if isinstance(n, IPv6Network))
        self._v4: list[IPv4Network] = _collapse_v4(v4)
        self._v6: list[IPv6Network] = _collapse_v6(v6)
        self._v4_starts: list[int] = [int(n.network_address) for n in self._v4]
        self._v6_starts: list[int] = [int(n.network_address) for n in self._v6]

    @property
    def network_count(self) -> int:
        """Number of (collapsed) networks held."""
        return len(self._v4) + len(self._v6)

    def is_whitelisted(self, ip: str) -> bool:
        """Return True if *ip* is contained in any whitelisted network."""
        addr = ip_address(ip.strip())
        if addr.version == 4:
            nets: list[IPv4Network] | list[IPv6Network] = self._v4
            starts = self._v4_starts
        else:
            nets = self._v6
            starts = self._v6_starts
        if not nets:
            return False
        # Rightmost network whose start address is <= addr.
        idx = bisect.bisect_right(starts, int(addr)) - 1
        if idx < 0:
            return False
        return addr in nets[idx]

    def count_overlap(self, network: IPv4Network | IPv6Network) -> int:
        """Count addresses of *network* that fall inside the whitelist.

        Used by ``asn-stats`` to estimate the hit probability for a hoster:
        ``count_overlap / network.num_addresses`` over its announced prefixes.
        """
        if network.version == 4:
            nets: list[IPv4Network] | list[IPv6Network] = self._v4
            starts = self._v4_starts
        else:
            nets = self._v6
            starts = self._v6_starts
        if not nets:
            return 0
        lo = int(network.network_address)
        hi = int(network.broadcast_address)
        total = 0
        # Walk back from the rightmost whitelist net that could reach into
        # [lo, hi]; stop once a net ends before lo (sorted + non-overlapping).
        i = bisect.bisect_right(starts, hi) - 1
        while i >= 0:
            wl = nets[i]
            wl_hi = int(wl.broadcast_address)
            if wl_hi < lo:
                break
            overlap = min(wl_hi, hi) - max(int(wl.network_address), lo) + 1
            if overlap > 0:
                total += overlap
            i -= 1
        return total
