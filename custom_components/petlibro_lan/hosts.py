"""Pure helper for IP addresses; no Home Assistant imports.

The broker only talks to devices on the local network: private (RFC 1918) or link-local IPv4.
Public, loopback, multicast, reserved and unspecified addresses are refused.
"""
from __future__ import annotations

import ipaddress

LAN_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))


def is_lan_ipv4(value: object) -> bool:
    """True for a private or link-local IPv4 address."""
    try:
        addr = ipaddress.IPv4Address(value) if isinstance(value, str) and value.count(".") == 3 else None
    except ValueError:
        return False
    if addr is None or addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_reserved:
        return False
    return any(addr in net for net in LAN_NETWORKS)
