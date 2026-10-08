from __future__ import annotations

import sys

import pytest

import mdns_filoxy.main as main_module
from tests.helpers import CliHarness, LoopStopped


def test_entry_point_calls_main_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(main_module, 'main', lambda: calls.append(True))

    main_module.entry_point()

    assert calls == [True]


def test_entry_point_does_not_double_run_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the old body was ``asyncio.run(main())``, but ``main`` already runs
    # its own event loop and returns ``None`` -> ``ValueError``.
    monkeypatch.setattr(main_module, 'main', lambda: None)

    main_module.entry_point()


def test_entry_point_reaches_the_cli_body(monkeypatch: pytest.MonkeyPatch, cli_harness: CliHarness) -> None:
    monkeypatch.setattr(sys, 'argv', ['mdns-filoxy', '-s', 'lo', '-d', 'lo'])

    with pytest.raises(LoopStopped):
        main_module.entry_point()

    assert cli_harness.source_zeroconf.closed
    assert cli_harness.dest_zeroconf.closed
