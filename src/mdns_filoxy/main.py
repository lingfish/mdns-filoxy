from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable

import click
from loguru import logger
from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from mdns_filoxy._version import __version__
from mdns_filoxy.ssdp import ZONE_PLAYER, SsdpProxy
from mdns_filoxy.utils import coro, find_address_by_name, find_ipv4_interfaces_by_name

logger.remove()
logger.add(sys.stderr, backtrace=True, diagnose=True)

#: How long after the destination sends a goodbye packet we treat a removal on the source
#: interface as our own goodbye echoing back, rather than a genuine device departure.
#:
#: zeroconf broadcasts each unregister as three goodbye packets 125 ms apart (``_UNREGISTER_TIME``),
#: so the whole train completes in ~250 ms. The echoed removal is delivered on the same browser
#: thread that is still busy in our callback, so it can be delayed further by blocking calls such as
#: ``get_service_info``; the window is sized generously for that. The window is measured from when
#: the registration change finished, not when the goodbye was sent.
ECHO_TTL_SECONDS = 2.0


def _same_service(a: ServiceInfo, b: ServiceInfo) -> bool:
    """Return whether two ``ServiceInfo`` objects publish the same discoverable data.

    TTLs are deliberately ignored: the records we copy from the source and the copy we
    announce carry different TTLs, so treating them as a change would make the proxy
    re-register on every re-announcement.
    """
    return (
        a.type == b.type
        and a.name == b.name
        and a.server == b.server
        and a.port == b.port
        and a.priority == b.priority
        and a.weight == b.weight
        and a.properties == b.properties
        and sorted(a.parsed_addresses()) == sorted(b.parsed_addresses())
    )


class ProxyRegistry:
    """Keep the services registered on the destination interface in sync with the source.

    The destination ``Zeroconf`` instance refuses to register the same service name twice
    (``ServiceNameAlreadyRegistered``), so whenever a service is re-announced or removed on
    the source interface we must first unregister whatever we previously registered for that
    name.

    On Linux the destination's announcements and goodbye packets are looped back into the
    source ``Zeroconf`` (its listen socket is bound to ``0.0.0.0`` and ``IP_MULTICAST_ALL`` is
    left enabled). Without care the proxy would see its own output as fresh source events and
    re-register the same services forever. Two guards stop that:

    * ``add`` skips re-registration when the incoming ``ServiceInfo`` is identical to the one
      already registered, so our echoed announcements are a no-op.
    * ``remove`` ignores removals that arrive while a goodbye we just sent is still in flight;
      those are our own goodbye echoing back through the source browser.

    Listeners run on their own browser threads, so access to the bookkeeping is locked.
    """

    def __init__(
        self,
        dest_zc: Zeroconf,
        echo_ttl: float = ECHO_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._dest_zc = dest_zc
        self._echo_ttl = echo_ttl
        self._clock = clock
        self._registered: dict[str, ServiceInfo] = {}
        #: Names whose goodbye packet we just sent, mapped to when the echo window closes.
        self._pending_goodbyes: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, name: str, info: ServiceInfo) -> bool:
        """Register (or re-register) a service on the destination interface.

        Returns ``True`` when the destination registration changed, ``False`` when it was
        already current (so callers can avoid logging no-ops).
        """
        with self._lock:
            current = self._registered.get(name)
            if current is not None and _same_service(current, info):
                return False
            self._registered.pop(name, None)
            if current is None:
                self._dest_zc.register_service(info, cooperating_responders=True)
            else:
                self._dest_zc.unregister_service(current)
                try:
                    self._dest_zc.register_service(info, cooperating_responders=True)
                finally:
                    # Start the echo window only once the (blocking) register has returned: the
                    # echoed removal is queued behind this call on the same browser thread.
                    self._pending_goodbyes[name] = self._clock() + self._echo_ttl
            self._registered[name] = info
            return True

    def remove(self, name: str) -> bool:
        """Unregister a service from the destination interface, if we registered it.

        Returns ``True`` when the destination registration changed. A removal that arrives
        while a goodbye we sent for this name is still in flight is our own echo and is
        ignored; acting on it would unregister the registration we just made and restart the
        announce/goodbye loop.
        """
        with self._lock:
            if name not in self._registered:
                return False
            if self._echo_pending(name):
                return False
            info = self._registered.pop(name)
            self._dest_zc.unregister_service(info)
            self._pending_goodbyes[name] = self._clock() + self._echo_ttl
            return True

    def _echo_pending(self, name: str) -> bool:
        """Return whether a goodbye we sent for ``name`` may still be echoing back."""
        deadline = self._pending_goodbyes.get(name)
        if deadline is None:
            return False
        if self._clock() >= deadline:
            del self._pending_goodbyes[name]
            return False
        return True


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
            changed = self.registry.add(name, info)
        except Exception:
            logger.exception(f'Failed to update service {name}')
            return
        if changed:
            logger.info(f'Service {name} updated')
        else:
            logger.debug(f'Service {name} unchanged, skipping re-registration')

    def remove_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        try:
            removed = self.registry.remove(name)
        except Exception:
            logger.exception(f'Failed to remove service {name}')
            return
        if removed:
            logger.info(f'Service {name} removed')
        else:
            logger.debug(f'Service {name} removal ignored (not registered or own goodbye echo)')

    def add_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        info = source_zc.get_service_info(type_, name)
        if info is None:
            logger.warning(f'Service {name} added without info')
            return
        try:
            added = self.registry.add(name, info)
        except Exception:
            logger.exception(f'Failed to add service {name}')
            return
        if added:
            logger.info(f'Service {name} added')
            logger.info('Announcement sent')
        else:
            logger.debug(f'Service {name} already registered, skipping re-registration')


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
@click.option(
    '--ssdp/--no-ssdp',
    default=True,
    help='Also answer SSDP (UPnP) searches on the destination interface with replies from source devices',
)
@click.option(
    '--ssdp-search-target',
    multiple=True,
    default=[ZONE_PLAYER],
    show_default=True,
    help='The SSDP search targets (ST) to proxy',
)
@coro
async def main(
    source_interface: str,
    dest_interface: str,
    mdns_services: tuple[str, ...],
    spotify_connect: bool,
    ssdp: bool,
    ssdp_search_target: tuple[str, ...],
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

    ssdp_proxy = make_ssdp_proxy(source_interface, dest_interface, ssdp_search_target) if ssdp else None
    if ssdp_proxy is not None:
        ssdp_proxy.start()

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        logger.info('Shutdown')
        if ssdp_proxy is not None:
            ssdp_proxy.close()
        zeroconf_dest.close()
        zeroconf_source.close()


def make_ssdp_proxy(source_interface: str, dest_interface: str, search_targets: tuple[str, ...]) -> SsdpProxy | None:
    """Build the SSDP proxy, or return ``None`` (with a warning) if an interface has no IPv4 address."""
    source = find_ipv4_interfaces_by_name(source_interface)
    dest = find_ipv4_interfaces_by_name(dest_interface)
    if not source or not dest:
        missing = source_interface if not source else dest_interface
        logger.warning(f'SSDP disabled: {missing} has no IPv4 address')
        return None
    logger.info(f'SSDP: proxying {list(search_targets)} from {source_interface} to {dest_interface}')
    return SsdpProxy(
        source_ip=str(source[0].ip),
        dest_ip=str(dest[0].ip),
        source_networks=[interface.network for interface in source],
        search_targets=search_targets,
    )


def entry_point() -> None:
    # ``main`` is already the ``@coro``-wrapped click command: it runs its own
    # ``asyncio.run`` internally, so calling it directly is all that's needed.
    # Wrapping it in another ``asyncio.run`` would try to await its ``None`` return.
    main()


if __name__ == '__main__':
    entry_point()
