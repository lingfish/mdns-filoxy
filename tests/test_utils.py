from __future__ import annotations

from collections.abc import Callable

from mdns_filoxy import utils
from tests.helpers import make_adapter, make_ip


def test_get_all_addresses_ipv4_returns_only_ipv4_strings() -> None:
    adapter = make_adapter('eth0', [make_ip('10.0.0.1'), make_ip(('fe80::1', 0, 3))])

    assert utils.get_all_addresses_ipv4([adapter]) == ['10.0.0.1']


def test_get_all_addresses_ipv4_dedups() -> None:
    adapter = make_adapter('eth0', [make_ip('10.0.0.1'), make_ip('10.0.0.1')])

    assert utils.get_all_addresses_ipv4([adapter]) == ['10.0.0.1']


def test_get_all_addresses_ipv4_across_adapters() -> None:
    adapters = [
        make_adapter('eth0', [make_ip('10.0.0.1')]),
        make_adapter('eth1', [make_ip('10.0.0.2')]),
    ]

    assert set(utils.get_all_addresses_ipv4(adapters)) == {'10.0.0.1', '10.0.0.2'}


def test_get_all_addresses_ipv6_extracts_first_tuple_element() -> None:
    adapter = make_adapter('eth0', [make_ip('10.0.0.1'), make_ip(('fe80::1', 0, 3))])

    assert utils.get_all_addresses_ipv6([adapter]) == ['fe80::1']


def test_get_all_addresses_ipv6_dedups() -> None:
    adapter = make_adapter('eth0', [make_ip(('fe80::1', 0, 3)), make_ip(('fe80::1', 0, 4))])

    assert utils.get_all_addresses_ipv6([adapter]) == ['fe80::1']


def test_find_address_by_name_combines_ipv4_and_ipv6(monkeypatch) -> None:
    adapter = make_adapter('eth0', [make_ip('10.0.0.1'), make_ip(('fe80::1', 0, 3))])
    monkeypatch.setattr(utils.ifaddr, 'get_adapters', lambda: [adapter])

    assert set(utils.find_address_by_name('eth0')) == {'10.0.0.1', 'fe80::1'}


def test_find_address_by_name_unknown_returns_empty(monkeypatch) -> None:
    adapter = make_adapter('eth0', [make_ip('10.0.0.1')])
    monkeypatch.setattr(utils.ifaddr, 'get_adapters', lambda: [adapter])

    assert utils.find_address_by_name('wlan0') == []


def test_find_address_by_name_combines_multiple_aliases(monkeypatch) -> None:
    adapters = [
        make_adapter('eth0', [make_ip('10.0.0.1')]),
        make_adapter('eth0', [make_ip('10.0.0.2')]),
        make_adapter('eth1', [make_ip('10.0.0.3')]),
    ]
    monkeypatch.setattr(utils.ifaddr, 'get_adapters', lambda: [adapters[0], adapters[1], adapters[2]])

    assert set(utils.find_address_by_name('eth0')) == {'10.0.0.1', '10.0.0.2'}


def test_find_address_by_name_calls_get_adapters_once(monkeypatch) -> None:
    calls = 0

    def counting_get_adapters() -> list[object]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(utils.ifaddr, 'get_adapters', counting_get_adapters)

    utils.find_address_by_name('eth0')

    assert calls == 1


def test_coro_runs_async_function() -> None:
    @utils.coro
    async def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3


def test_coro_preserves_metadata() -> None:
    @utils.coro
    async def documented() -> None:
        """A docstring."""

    assert documented.__name__ == 'documented'
    assert documented.__doc__ == 'A docstring.'


def test_coro_turns_coroutine_function_into_sync_callable() -> None:
    """The wrapper turns a coroutine function into a plain synchronous callable."""

    async def body() -> str:
        return 'ok'

    wrapped: Callable[[], str] = utils.coro(body)

    assert wrapped() == 'ok'
