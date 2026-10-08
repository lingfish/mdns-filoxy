from __future__ import annotations

import asyncio
import sys
import threading

import click
from loguru import logger
from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from mdns_filoxy._version import __version__
from mdns_filoxy.utils import coro, find_address_by_name

logger.remove()
logger.add(sys.stderr, backtrace=True, diagnose=True)


class ProxyRegistry:
    """Keep the services registered on the destination interface in sync with the source.

    The destination ``Zeroconf`` instance refuses to register the same service name twice
    (``ServiceNameAlreadyRegistered``), so whenever a service is re-announced or removed on
    the source interface we must first unregister whatever we previously registered for that
    name.

    Listeners run on their own browser threads, so access to the bookkeeping is locked.
    """

    def __init__(self, dest_zc: Zeroconf):
        self._dest_zc = dest_zc
        self._registered: dict[str, ServiceInfo] = {}
        self._lock = threading.Lock()

    def add(self, name: str, info: ServiceInfo) -> None:
        """Register (or re-register) a service on the destination interface."""
        with self._lock:
            old_info = self._registered.pop(name, None)
            if old_info is not None:
                self._dest_zc.unregister_service(old_info)
            self._dest_zc.register_service(info, cooperating_responders=True)
            self._registered[name] = info

    def remove(self, name: str) -> None:
        """Unregister a service from the destination interface, if we registered it."""
        with self._lock:
            info = self._registered.pop(name, None)
            if info is not None:
                self._dest_zc.unregister_service(info)


class MyListener(ServiceListener):
    def __init__(self, registry: ProxyRegistry):
        self.registry = registry
        super().__init__()

    def update_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        info = source_zc.get_service_info(type_, name)
        if info is None:
            logger.warning(f'Service {name} updated without info')
            return
        try:
            self.registry.add(name, info)
        except Exception:
            logger.exception(f'Failed to update service {name}')
            return
        logger.info(f'Service {name} updated')

    def remove_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        try:
            self.registry.remove(name)
        except Exception:
            logger.exception(f'Failed to remove service {name}')
            return
        logger.info(f'Service {name} removed')

    def add_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        info = source_zc.get_service_info(type_, name)
        if info is None:
            logger.warning(f'Service {name} added without info')
            return
        try:
            self.registry.add(name, info)
        except Exception:
            logger.exception(f'Failed to add service {name}')
            return
        logger.info(f'Service {name} added')
        logger.info('Announcement sent')


@logger.catch(reraise=True)
@click.command()
@click.version_option(version=__version__)
@click.option('--source-interface', '-s', required=True, help='The interface to proxy from')
@click.option('--dest-interface', '-d', required=True, help='The interface to proxy to, and answer requests on')
@click.option(
    '--mdns-services', '-m', multiple=True, default=['_sonos._tcp.local.'], help='The mDNS services to listen for'
)
@click.option(
    '--spotify-connect/--no-spotify-connect',
    '--spotify/--no-spotify',
    default=True,
    help='Add the suitable Spotify connect mDNS entry',
)
@coro
async def main(
    source_interface: str, dest_interface: str, mdns_services: tuple[str, ...], spotify_connect: bool
) -> None:
    """This is mdns-filoxy, the mDNS filter proxy!"""

    if spotify_connect:
        mdns_services += ('_spotify-connect._tcp.local.',)

    zeroconf_source_address = find_address_by_name(source_interface)
    zeroconf_dest_address = find_address_by_name(dest_interface)

    logger.info(f'Listening on {source_interface} ({zeroconf_source_address}) for {mdns_services}')
    zeroconf_source = Zeroconf(interfaces=zeroconf_source_address)
    logger.info(f'Proxying {source_interface} to {dest_interface} ({zeroconf_dest_address})')
    zeroconf_dest = Zeroconf(interfaces=zeroconf_dest_address)

    registry = ProxyRegistry(zeroconf_dest)

    browsers = []
    for service in mdns_services:
        listener = MyListener(registry)
        browsers.append(ServiceBrowser(zeroconf_source, service, listener))

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        logger.info('Shutdown')
        zeroconf_dest.close()
        zeroconf_source.close()


def entry_point() -> None:
    # ``main`` is already the ``@coro``-wrapped click command: it runs its own
    # ``asyncio.run`` internally, so calling it directly is all that's needed.
    # Wrapping it in another ``asyncio.run`` would try to await its ``None`` return.
    main()


if __name__ == '__main__':
    entry_point()
