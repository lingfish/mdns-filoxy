import asyncio
import sys

import click
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
from loguru import logger

from mdns_filoxy.utils import find_address_by_name, coro
from mdns_filoxy._version import __version__

logger.remove()
logger.add(sys.stderr, backtrace=True, diagnose=True)


class MyListener(ServiceListener):
    def __init__(self, dest_zc):
        self.dest_zc = dest_zc
        super().__init__()

    def update_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        logger.info(f'Service {name} updated')

    def remove_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        logger.info(f'Service {name} removed')

    def add_service(self, source_zc: Zeroconf, type_: str, name: str) -> None:
        info = source_zc.get_service_info(type_, name)
        logger.info(f'Service {name} added')
        self.dest_zc.register_service(info, cooperating_responders=True)
        logger.info('Announcement sent')


@logger.catch
@click.command()
@click.version_option(version=__version__)
@click.option('--source-interface', '-s', required=True, help='The interface to proxy from')
@click.option('--dest-interface', '-d', required=True, help='The interface to proxy to, and answer requests on')
@click.option(
    '--mdns-services', '-m', multiple=True, default=['_sonos._tcp.local.'], help='The mDNS services to listen for'
)
@coro
async def main(source_interface: str, dest_interface: str, mdns_services: list[str]) -> None:
    """This is mdns-filoxy, the mDNS filter proxy!"""
    zeroconf_source_address = find_address_by_name(source_interface)
    zeroconf_dest_address = find_address_by_name(dest_interface)
    logger.info(f'Listening on {source_interface} ({zeroconf_source_address}) for {mdns_services}')
    zeroconf_source = Zeroconf(interfaces=zeroconf_source_address)
    logger.info(f'Proxying {source_interface} to {dest_interface} ({zeroconf_dest_address})')
    zeroconf_dest = Zeroconf(interfaces=zeroconf_dest_address)

    for service in mdns_services:
        listener = MyListener(dest_zc=zeroconf_dest)
        browser = ServiceBrowser(zeroconf_source, service, listener)

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        logger.info('Shutdown')
        zeroconf_dest.close()
        zeroconf_source.close()


def entry_point() -> None:
    asyncio.run(main())


if __name__ == '__main__':
    entry_point()
