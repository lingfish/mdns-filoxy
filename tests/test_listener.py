from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest
from loguru import logger
from zeroconf import ServiceInfo, Zeroconf

from mdns_filoxy.main import MyListener, ProxyRegistry
from tests.helpers import (
    NAME,
    TYPE,
    FakeDestZeroconf,
    FakeSourceZeroconf,
    make_info,
    make_listener,
    make_source,
)


class BoomRegisterDest(FakeDestZeroconf):
    def register_service(self, info: ServiceInfo, cooperating_responders: bool = False) -> None:
        raise RuntimeError('boom')


class BoomUnregisterDest(FakeDestZeroconf):
    def unregister_service(self, info: ServiceInfo) -> None:
        raise RuntimeError('boom')


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
    dest = BoomRegisterDest()
    listener = MyListener(ProxyRegistry(cast(Zeroconf, dest)))

    # A raising destination must not propagate; it would kill the browser thread.
    listener.add_service(make_source(make_info()), TYPE, NAME)


def test_update_errors_do_not_escape_listener() -> None:
    dest = BoomRegisterDest()
    listener = MyListener(ProxyRegistry(cast(Zeroconf, dest)))

    # ``update_service`` runs on a browser thread; an escaping exception kills it.
    listener.update_service(make_source(make_info()), TYPE, NAME)


def test_remove_errors_do_not_escape_listener() -> None:
    dest = BoomUnregisterDest()
    listener = MyListener(ProxyRegistry(cast(Zeroconf, dest)))
    listener.add_service(make_source(make_info()), TYPE, NAME)

    listener.remove_service(make_source(None), TYPE, NAME)


def test_register_uses_cooperating_responders() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)

    listener.add_service(make_source(make_info()), TYPE, NAME)

    assert dest.register_kwargs == [{'cooperating_responders': True}]


def test_re_register_unregisters_before_registering() -> None:
    dest = FakeDestZeroconf()
    registry = ProxyRegistry(cast(Zeroconf, dest))
    first = make_info(port=1400)
    second = make_info(port=1401)

    registry.add(NAME, first)
    dest.events.clear()
    registry.add(NAME, second)

    assert dest.events == [('unregister', first), ('register', second)]
    assert dest.registered == {NAME: second}


def test_registry_remove_is_symmetric() -> None:
    dest = FakeDestZeroconf()
    registry = ProxyRegistry(cast(Zeroconf, dest))
    info = make_info()

    registry.add(NAME, info)
    registry.remove(NAME)

    assert dest.events == [('register', info), ('unregister', info)]
    assert dest.registered == {}
    assert registry._registered == {}


def test_failed_register_leaves_no_tracking() -> None:
    dest = BoomRegisterDest()
    registry = ProxyRegistry(cast(Zeroconf, dest))

    with pytest.raises(RuntimeError):
        registry.add(NAME, make_info())

    assert registry._registered == {}


def test_add_noops_on_identical_info() -> None:
    dest = FakeDestZeroconf()
    registry = ProxyRegistry(cast(Zeroconf, dest))
    info = make_info()

    assert registry.add(NAME, info) is True
    # An equivalent ``ServiceInfo`` (same name/port/addresses/properties, different TTLs)
    # must not trigger a re-registration.
    assert registry.add(NAME, make_info()) is False

    assert dest.register_calls == [info]
    assert dest.unregister_calls == []
    assert dest.registered == {NAME: info}


def test_add_reregisters_on_changed_info() -> None:
    dest = FakeDestZeroconf()
    registry = ProxyRegistry(cast(Zeroconf, dest))
    first = make_info(port=1400)
    second = make_info(port=1401)

    assert registry.add(NAME, first) is True
    assert registry.add(NAME, second) is True

    assert dest.registered == {NAME: second}
    assert len(dest.register_calls) == 2
    assert dest.unregister_calls == [first]


def test_remove_ignores_own_goodbye_echo_within_ttl() -> None:
    dest = FakeDestZeroconf()
    now = [0.0]
    registry = ProxyRegistry(cast(Zeroconf, dest), echo_ttl=1.0, clock=lambda: now[0])
    info_a = make_info(port=1400)
    info_b = make_info(port=1401)

    assert registry.add(NAME, info_a) is True
    # Re-registering sends a goodbye for ``info_a``; that goodbye is still echoing back.
    assert registry.add(NAME, info_b) is True

    now[0] = 0.5  # still within the echo window
    assert registry.remove(NAME) is False

    assert dest.registered == {NAME: info_b}
    assert dest.unregister_calls == [info_a]


def test_echo_window_starts_after_blocking_register_returns() -> None:
    now = [0.0]

    class SlowRegisterDest(FakeDestZeroconf):
        def register_service(self, info: ServiceInfo, cooperating_responders: bool = False) -> None:
            super().register_service(info, cooperating_responders)
            now[0] += 0.9  # zeroconf blocks until the announcement train has gone out

    dest = SlowRegisterDest()
    registry = ProxyRegistry(cast(Zeroconf, dest), echo_ttl=1.0, clock=lambda: now[0])
    info_a = make_info(port=1400)
    info_b = make_info(port=1401)

    registry.add(NAME, info_a)
    registry.add(NAME, info_b)  # finishes at t=1.8; the echo window must run until t=2.8

    now[0] = 2.5  # past "goodbye sent + ttl" but inside "register finished + ttl"
    assert registry.remove(NAME) is False
    assert dest.registered == {NAME: info_b}


def test_remove_works_after_echo_window_closes() -> None:
    dest = FakeDestZeroconf()
    now = [0.0]
    registry = ProxyRegistry(cast(Zeroconf, dest), echo_ttl=1.0, clock=lambda: now[0])
    info_a = make_info(port=1400)
    info_b = make_info(port=1401)

    registry.add(NAME, info_a)
    registry.add(NAME, info_b)  # sends a goodbye for info_a, deadline = 1.0

    now[0] = 1.5  # past the echo window; the pending goodbye must be expired and dropped
    assert registry.remove(NAME) is True

    assert dest.registered == {}
    assert dest.unregister_calls == [info_a, info_b]


def test_remove_unknown_name_returns_false() -> None:
    dest = FakeDestZeroconf()
    registry = ProxyRegistry(cast(Zeroconf, dest))

    assert registry.remove(NAME) is False
    assert dest.unregister_calls == []
    assert dest.register_calls == []


def test_listener_logs_noop_at_debug_and_change_at_info() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)
    stream = io.StringIO()
    sink_id = logger.add(stream, level='DEBUG', format='{level.name}: {message}')

    try:
        listener.add_service(make_source(make_info()), TYPE, NAME)  # genuine change -> INFO
        listener.add_service(make_source(make_info()), TYPE, NAME)  # identical -> DEBUG
        listener.update_service(make_source(make_info()), TYPE, NAME)  # identical -> DEBUG
    finally:
        logger.remove(sink_id)

    output = stream.getvalue()
    assert f'INFO: Service {NAME} added' in output
    assert f'DEBUG: Service {NAME} already registered, skipping re-registration' in output
    assert f'DEBUG: Service {NAME} unchanged, skipping re-registration' in output
    assert f'INFO: Service {NAME} already registered' not in output
    assert f'INFO: Service {NAME} unchanged' not in output


def test_concurrent_registry_never_raises_already_registered() -> None:
    dest = FakeDestZeroconf()
    # ``echo_ttl=0.0`` disables goodbye-echo suppression, so the churn below keeps the
    # original symmetric add/remove semantics this test is guarding (no suppressed removes).
    registry = ProxyRegistry(cast(Zeroconf, dest), echo_ttl=0.0)
    infos = [make_info(port=1400 + index) for index in range(4)]

    def churn(worker: int) -> None:
        for step in range(200):
            registry.add(NAME, infos[(worker + step) % len(infos)])
            registry.remove(NAME)

    with ThreadPoolExecutor(max_workers=8) as pool:
        # ``map`` re-raises the first exception a worker hits; the shared lock must
        # keep the destination's duplicate-name guard from ever firing.
        list(pool.map(churn, range(8)))

    assert dest.registered == {}
    assert len(dest.register_calls) == len(dest.unregister_calls)


def test_concurrent_shared_registry_keeps_single_entry_per_name() -> None:
    """Two listeners sharing one registry (as the CLI wires them) stay consistent."""
    dest = FakeDestZeroconf()
    registry = ProxyRegistry(cast(Zeroconf, dest))
    listener_a = MyListener(registry)
    listener_b = MyListener(registry)

    def add_via(listener: MyListener, port: int) -> None:
        for _ in range(100):
            listener.add_service(make_source(make_info(port=port)), TYPE, NAME)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: add_via(*args), [(listener_a, 1400), (listener_b, 1401)]))

    assert len(dest.registered) == 1
    assert NAME in dest.registered


def test_source_zeroconf_requested_with_browse_type() -> None:
    dest = FakeDestZeroconf()
    listener = make_listener(dest)
    source = FakeSourceZeroconf(make_info())

    listener.add_service(cast(Zeroconf, source), TYPE, NAME)

    assert source.requested == [(TYPE, NAME)]
