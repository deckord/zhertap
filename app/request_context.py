from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from fastapi import Request

from app.config import settings


def _trusted_proxy_networks() -> tuple[IPv4Network | IPv6Network, ...]:
    networks: list[IPv4Network | IPv6Network] = []
    for raw_network in settings.trusted_proxy_networks.split(","):
        value = raw_network.strip()
        if not value:
            continue
        try:
            networks.append(ip_network(value, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def client_ip(request: Request) -> str:
    """Return the client address without trusting spoofable forwarding headers."""
    peer = request.client.host if request.client else ""
    if not peer:
        return ""

    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer[:64]

    if not any(peer_address in network for network in _trusted_proxy_networks()):
        return str(peer_address)

    forwarded = request.headers.get("x-forwarded-for", "")
    for raw_address in forwarded.split(","):
        value = raw_address.strip()
        try:
            return str(ip_address(value))
        except ValueError:
            continue
    return str(peer_address)
