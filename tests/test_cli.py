from __future__ import annotations

import io
import ipaddress
import sys
from collections.abc import Iterator
from typing import cast

import pytest
from click.testing import CliRunner
from loguru import logger

import mdns_filoxy.main as main_module
from mdns_filoxy._version import __version__
from mdns_filoxy.main import MyListener
from mdns_filoxy.ssdp import ZONE_PLAYER, SsdpProxy, build_msearch
from tests.helpers import CliHarness, LoopStopped

SONOS = '_sonos._tcp.local.'
SPOTIFY = '_spotify-connect._tcp.local.'


def test_version_option_prints_version(cli_command) -> None:
    result = CliRunner().invoke(cli_command, ['--version'])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_missing_source_interface_exits_2(cli_command) -> None:
    result = CliRunner().invoke(cli_command, ['-d', 'lo'])

    assert result.exit_code == 2
    assert '--source-interface' in result.output


def test_missing_dest_interface_exits_2(cli_command) -> None:
    result = CliRunner().invoke(cli_command, ['-s', 'lo'])

    assert result.exit_code == 2
    assert '--dest-interface' in result.output


def test_default_browses_sonos_and_spotify(cli_harness: CliHarness) -> None:
    result = CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo'])

    assert isinstance(result.exception, LoopStopped)
    assert cli_harness.browsed_services == [SONOS, SPOTIFY]


def test_no_spotify_connect_excludes_spotify(cli_harness: CliHarness) -> None:
    CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo', '--no-spotify-connect'])

    assert cli_harness.browsed_services == [SONOS]


def test_no_spotify_alias_excludes_spotify(cli_harness: CliHarness) -> None:
    CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo', '--no-spotify'])

    assert cli_harness.browsed_services == [SONOS]


def test_multiple_services_replace_default_and_keep_spotify(cli_harness: CliHarness) -> None:
    CliRunner().invoke(
        cli_harness.command,
        ['-s', 'lo', '-d', 'lo', '-m', '_a._tcp.local.', '-m', '_b._tcp.local.'],
    )

    assert cli_harness.browsed_services == ['_a._tcp.local.', '_b._tcp.local.', SPOTIFY]


def test_each_service_gets_its_own_browser_sharing_one_registry(cli_harness: CliHarness) -> None:
    CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo', '-m', '_a._tcp.local.'])

    assert len(cli_harness.browsers) == 2  # the explicit service + spotify
    listeners = [browser.listener for browser in cli_harness.browsers]
    assert all(isinstance(listener, MyListener) for listener in listeners)
    first = cast(MyListener, listeners[0])
    second = cast(MyListener, listeners[1])
    assert first.registry is second.registry


def test_source_and_dest_zeroconf_bound_to_resolved_addresses(cli_harness: CliHarness) -> None:
    cli_harness.addresses['src0'] = ['10.0.0.1']
    cli_harness.addresses['dst0'] = ['10.0.0.2', 'fd00::1']

    CliRunner().invoke(cli_harness.command, ['-s', 'src0', '-d', 'dst0'])

    assert cli_harness.source_zeroconf.interfaces == ['10.0.0.1']
    assert cli_harness.dest_zeroconf.interfaces == ['10.0.0.2', 'fd00::1']


def test_both_zeroconf_instances_closed_on_shutdown(cli_harness: CliHarness) -> None:
    CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo'])

    assert len(cli_harness.zeroconf_instances) == 2
    assert cli_harness.source_zeroconf.closed
    assert cli_harness.dest_zeroconf.closed


def test_sleeps_one_second_per_loop_iteration(cli_harness: CliHarness) -> None:
    cli_harness.sleep.max_loops = 3

    CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo'])

    assert cli_harness.sleep.calls == [1, 1, 1]


def test_cli_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@logger.catch(reraise=True)`` must let failures reach the caller."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError('nope')

    monkeypatch.setattr(main_module, 'find_address_by_name', lambda name: ['127.0.0.1'])
    monkeypatch.setattr(main_module, 'Zeroconf', boom)
    monkeypatch.setattr(sys, 'argv', ['mdns-filoxy', '-s', 'lo', '-d', 'lo'])

    with pytest.raises(RuntimeError, match='nope'):
        main_module.main()


def test_default_starts_ssdp_proxy_and_closes_it_on_shutdown(cli_harness: CliHarness) -> None:
    CliRunner().invoke(cli_harness.command, ['-s', 'src0', '-d', 'dst0'])

    assert cli_harness.ssdp_requests == [('src0', 'dst0', (ZONE_PLAYER,))]
    [proxy] = cli_harness.ssdp_proxies
    assert proxy.started
    assert proxy.closed


def test_no_ssdp_does_not_build_proxy(cli_harness: CliHarness) -> None:
    CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo', '--no-ssdp'])

    assert cli_harness.ssdp_requests == []
    assert cli_harness.ssdp_proxies == []


def test_multiple_ssdp_search_targets_are_passed_through(cli_harness: CliHarness) -> None:
    CliRunner().invoke(
        cli_harness.command,
        ['-s', 'lo', '-d', 'lo', '--ssdp-search-target', 'urn:x:1', '--ssdp-search-target', 'urn:y:1'],
    )

    assert cli_harness.ssdp_requests == [('lo', 'lo', ('urn:x:1', 'urn:y:1'))]


def test_cli_still_runs_when_ssdp_proxy_unavailable(cli_harness: CliHarness) -> None:
    cli_harness.ssdp_available = False

    result = CliRunner().invoke(cli_harness.command, ['-s', 'lo', '-d', 'lo'])

    assert isinstance(result.exception, LoopStopped)
    assert len(cli_harness.ssdp_requests) == 1
    assert cli_harness.browsed_services == [SONOS, SPOTIFY]
    assert cli_harness.source_zeroconf.closed
    assert cli_harness.dest_zeroconf.closed


# --- make_ssdp_proxy ---------------------------------------------------------------


@pytest.fixture
def interfaces(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[ipaddress.IPv4Interface]]:
    table: dict[str, list[ipaddress.IPv4Interface]] = {}
    monkeypatch.setattr(main_module, 'find_ipv4_interfaces_by_name', lambda name: table.get(name, []))
    return table


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    sink_id = logger.add(stream, level='DEBUG', format='{level.name}: {message}')
    yield stream
    logger.remove(sink_id)


@pytest.mark.parametrize('missing', ['src0', 'dst0'])
def test_make_ssdp_proxy_returns_none_and_warns_without_ipv4(
    interfaces: dict[str, list[ipaddress.IPv4Interface]], log_stream: io.StringIO, missing: str
) -> None:
    interfaces['src0'] = [ipaddress.IPv4Interface('192.168.1.10/24')]
    interfaces['dst0'] = [ipaddress.IPv4Interface('10.0.0.1/24')]
    del interfaces[missing]

    proxy = main_module.make_ssdp_proxy('src0', 'dst0', (ZONE_PLAYER,))

    assert proxy is None
    assert f'WARNING: SSDP disabled: {missing} has no IPv4 address' in log_stream.getvalue()


def test_make_ssdp_proxy_uses_first_ipv4_and_all_source_networks(
    interfaces: dict[str, list[ipaddress.IPv4Interface]],
) -> None:
    interfaces['src0'] = [ipaddress.IPv4Interface('192.168.1.10/24'), ipaddress.IPv4Interface('172.16.0.5/16')]
    interfaces['dst0'] = [ipaddress.IPv4Interface('10.0.0.1/24'), ipaddress.IPv4Interface('10.0.1.1/24')]

    proxy = main_module.make_ssdp_proxy('src0', 'dst0', ('urn:x:1', 'urn:y:1'))

    # Constructed but never started: no threads, no sockets.
    assert isinstance(proxy, SsdpProxy)
    assert proxy.source_ip == '192.168.1.10'
    assert proxy.dest_ip == '10.0.0.1'
    assert proxy.search_targets == ['urn:x:1', 'urn:y:1']
    # Searches from either source network are ignored; the destination network is answered.
    proxy.cache.update('urn:x:1', {'192.168.1.20': b'reply'})
    assert proxy.responder.answer(build_msearch('urn:x:1'), ('192.168.1.99', 5000)) == []
    assert proxy.responder.answer(build_msearch('urn:x:1'), ('172.16.9.9', 5000)) == []
    assert proxy.responder.answer(build_msearch('urn:x:1'), ('10.0.0.50', 5000)) == [b'reply']
