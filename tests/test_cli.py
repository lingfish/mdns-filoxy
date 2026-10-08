from __future__ import annotations

import sys
from typing import cast

import pytest
from click.testing import CliRunner

import mdns_filoxy.main as main_module
from mdns_filoxy._version import __version__
from mdns_filoxy.main import MyListener
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
