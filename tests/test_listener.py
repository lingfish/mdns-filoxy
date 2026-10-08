from __future__ import annotations

import socket
from typing import cast

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

    def register_service(self, info: ServiceInfo, cooperating_responders: bool = False) -> None:
        self.register_calls.append(info)
        if info.name in self.registered:
            raise ServiceNameAlreadyRegistered
        self.registered[info.name] = info

    def unregister_service(self, info: ServiceInfo) -> None:
        self.unregister_calls.append(info)
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


def test_add_service_registers_on_destination() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)
    info = make_info()

    listener.add_service(make_source(info), TYPE, NAME)

    assert dest.registered == {NAME: info}
    assert dest.register_calls == [info]
    assert dest.unregister_calls == []


def test_add_service_is_idempotent() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)
    listener.add_service(make_source(make_info()), TYPE, NAME)

    replacement = make_info(port=1401)
    listener.add_service(make_source(replacement), TYPE, NAME)

    assert dest.registered == {NAME: replacement}
    assert len(dest.register_calls) == 2
    assert len(dest.unregister_calls) == 1


def test_remove_then_add_does_not_raise_already_registered() -> None:
    """Regression: the source browser re-announces by firing remove then add."""
    dest = FakeDestZeroconf()
    listener = make_listener(dest)

    listener.add_service(make_source(make_info()), TYPE, NAME)
    listener.remove_service(make_source(None), TYPE, NAME)
    assert dest.registered == {}

    listener.add_service(make_source(make_info()), TYPE, NAME)
    assert NAME in dest.registered


def test_remove_unknown_service_is_noop() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)

    listener.remove_service(make_source(None), TYPE, NAME)

    assert dest.unregister_calls == []
    assert dest.register_calls == []


def test_update_service_replaces_existing_registration() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)
    listener.add_service(make_source(make_info()), TYPE, NAME)

    updated = make_info(port=1401)
    listener.update_service(make_source(updated), TYPE, NAME)

    assert dest.registered[NAME] is updated
    assert len(dest.unregister_calls) == 1


def test_missing_info_is_ignored() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)

    listener.add_service(make_source(None), TYPE, NAME)
    listener.update_service(make_source(None), TYPE, NAME)

    assert dest.register_calls == []


def test_registration_errors_do_not_escape_listener() -> None:
    class BoomDestZeroconf(FakeDestZeroconf):
        def register_service(self, info: ServiceInfo, cooperating_responders: bool = False) -> None:
            raise RuntimeError('boom')

    dest = BoomDestZeroconf()
    listener = MyListener(ProxyRegistry(cast(Zeroconf, dest)))

    # A raising destination must not propagate; it would kill the browser thread.
    listener.add_service(make_source(make_info()), TYPE, NAME)
