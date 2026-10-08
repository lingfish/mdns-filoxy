from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import cast

import click
import ifaddr
from zeroconf import ServiceInfo, ServiceNameAlreadyRegistered, Zeroconf

from mdns_filoxy.main import MyListener, ProxyRegistry

TYPE = '_sonos._tcp.local.'
NAME = 'RINCON_1234@Office._sonos._tcp.local.'


def make_info(name: str = NAME, port: int = 1400) -> ServiceInfo:
    return ServiceInfo(
        TYPE,
        name,
        port=port,
        addresses=[socket.inet_aton('192.0.2.1')],
    )


class FakeDestZeroconf:
    """Stand-in for the destination ``Zeroconf``, including its duplicate-name guard."""

    def __init__(self) -> None:
        self.registered: dict[str, ServiceInfo] = {}
        self.register_calls: list[ServiceInfo] = []
        self.unregister_calls: list[ServiceInfo] = []
        self.register_kwargs: list[dict[str, bool]] = []
        # Combined, ordered log of both operations, for asserting call order.
        self.events: list[tuple[str, ServiceInfo]] = []

    def register_service(self, info: ServiceInfo, cooperating_responders: bool = False) -> None:
        self.register_kwargs.append({'cooperating_responders': cooperating_responders})
        self.register_calls.append(info)
        self.events.append(('register', info))
        if info.name in self.registered:
            raise ServiceNameAlreadyRegistered
        self.registered[info.name] = info

    def unregister_service(self, info: ServiceInfo) -> None:
        self.unregister_calls.append(info)
        self.events.append(('unregister', info))
        self.registered.pop(info.name, None)


class FakeSourceZeroconf:
    """Stand-in for the source ``Zeroconf``; returns whatever info it was given."""

    def __init__(self, info: ServiceInfo | None) -> None:
        self.info = info
        self.requested: list[tuple[str, str]] = []

    def get_service_info(self, type_: str, name: str) -> ServiceInfo | None:
        self.requested.append((type_, name))
        return self.info


def make_listener(dest: FakeDestZeroconf) -> MyListener:
    return MyListener(ProxyRegistry(cast(Zeroconf, dest)))


def make_source(info: ServiceInfo | None) -> Zeroconf:
    return cast(Zeroconf, FakeSourceZeroconf(info))


def make_ip(ip: str | tuple[str, int, int], network_prefix: int = 24) -> ifaddr.IP:
    return ifaddr.IP(ip, network_prefix, '')


def make_adapter(name: str, ips: list[ifaddr.IP], nice_name: str | None = None) -> ifaddr.Adapter:
    return ifaddr.Adapter(name, nice_name if nice_name is not None else name, ips)


# --- CLI harness -------------------------------------------------------------------


class FakeZeroconf:
    """Records the ``interfaces`` it was constructed with and whether it was closed."""

    def __init__(self, interfaces: list[str] | None = None) -> None:
        self.interfaces = interfaces
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeServiceBrowser:
    def __init__(self, zc: object, service_type: str, listener: object) -> None:
        self.zc = zc
        self.service_type = service_type
        self.listener = listener


class LoopStopped(Exception):
    """Sentinel raised by the fake ``asyncio.sleep`` to break the CLI's infinite loop."""


class SleepStopper:
    """``asyncio.sleep`` replacement that records calls and stops the loop deterministically."""

    def __init__(self, max_loops: int = 1) -> None:
        self.max_loops = max_loops
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= self.max_loops:
            raise LoopStopped


@dataclass
class CliHarness:
    command: click.Command
    sleep: SleepStopper
    addresses: dict[str, list[str]] = field(default_factory=dict)
    zeroconf_instances: list[FakeZeroconf] = field(default_factory=list)
    browsers: list[FakeServiceBrowser] = field(default_factory=list)

    @property
    def browsed_services(self) -> list[str]:
        return [browser.service_type for browser in self.browsers]

    @property
    def source_zeroconf(self) -> FakeZeroconf:
        # ``main`` constructs the source Zeroconf before the destination one.
        return self.zeroconf_instances[0]

    @property
    def dest_zeroconf(self) -> FakeZeroconf:
        return self.zeroconf_instances[1]
