import asyncio
from functools import wraps

import ifaddr


def get_all_addresses_ipv4(adapters: list[ifaddr.Adapter]) -> list[str]:
    return list({addr.ip for iface in adapters for addr in iface.ips if addr.is_IPv4})


def get_all_addresses_ipv6(adapters: list[ifaddr.Adapter]) -> list[str]:
    return list({addr.ip[0] for iface in adapters for addr in iface.ips if addr.is_IPv6})


def find_address_by_name(name: str) -> list[str]:
    """Return a list of IP addresses based on interface name"""
    ips = []
    for adapter in ifaddr.get_adapters():
        if adapter.name == name:
            ips += get_all_addresses_ipv4([adapter]) + get_all_addresses_ipv6([adapter])
    return ips


def coro(f):
    """Make python click work with asyncio"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper
