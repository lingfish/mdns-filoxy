from __future__ import annotations

import types
from collections.abc import Iterator
from typing import cast

import click
import pytest
from loguru import logger

import mdns_filoxy.main as main_module
from tests.helpers import (
    CliHarness,
    FakeServiceBrowser,
    FakeZeroconf,
    SleepStopper,
)


@pytest.fixture(autouse=True)
def _quiet_loguru() -> Iterator[None]:
    """Detach every loguru sink so tests don't spew tracebacks to stderr.

    Importing ``mdns_filoxy.main`` already calls ``logger.remove()`` and adds a
    stderr sink; the except-handlers under test log through it.
    """
    logger.remove()
    yield
    logger.remove()


@pytest.fixture
def cli_command() -> click.Command:
    """The real click command hidden behind the ``@logger.catch`` wrapper."""
    return cast(click.Command, main_module.main.__wrapped__)  # type: ignore[attr-defined]


@pytest.fixture
def cli_harness(monkeypatch: pytest.MonkeyPatch) -> CliHarness:
    """Wire the CLI command to fakes and a deterministic loop-stopper."""
    harness = CliHarness(
        command=cast(click.Command, main_module.main.__wrapped__),  # type: ignore[attr-defined]
        sleep=SleepStopper(),
    )

    def fake_find_address(name: str) -> list[str]:
        return harness.addresses.get(name, ['127.0.0.1'])

    def make_zeroconf(interfaces: list[str] | None = None) -> FakeZeroconf:
        instance = FakeZeroconf(interfaces)
        harness.zeroconf_instances.append(instance)
        return instance

    def make_browser(zc: object, service_type: str, listener: object) -> FakeServiceBrowser:
        browser = FakeServiceBrowser(zc, service_type, listener)
        harness.browsers.append(browser)
        return browser

    monkeypatch.setattr(main_module, 'find_address_by_name', fake_find_address)
    monkeypatch.setattr(main_module, 'Zeroconf', make_zeroconf)
    monkeypatch.setattr(main_module, 'ServiceBrowser', make_browser)
    # ``main`` only reaches for ``asyncio.sleep``; swap the module for a minimal stub
    # so the event loop still runs (``utils.coro`` holds its own asyncio reference).
    monkeypatch.setattr(main_module, 'asyncio', types.SimpleNamespace(sleep=harness.sleep))
    return harness
