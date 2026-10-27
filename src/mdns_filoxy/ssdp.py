"""Answer SSDP (UPnP) searches on the destination interface on behalf of source devices.

Sonos speakers only answer ``M-SEARCH`` requests that come from their own subnet, and the Sonos
Android app will not use the speakers it finds over mDNS until one also answers its SSDP search.
So, alongside the mDNS proxy, we periodically search the source interface ourselves, cache the
devices' replies, and replay them (unicast) to matching searches arriving from other networks.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable, Sequence

from loguru import logger

SSDP_GROUP = '239.255.255.250'
SSDP_PORT = 1900
SSDP_ALL = 'ssdp:all'
ZONE_PLAYER = 'urn:schemas-upnp-org:device:ZonePlayer:1'

#: How often to re-search the source interface, so the cached replies follow devices coming and going.
REFRESH_SECONDS = 120.0
#: How long to wait for devices to answer each of our searches (they reply within ``MX`` = 1 s).
COLLECT_SECONDS = 3.0
#: Clients send each search several times (multicast and broadcast); answer a given one only once in this window.
DEDUPE_SECONDS = 1.0

SocketFactory = Callable[[int, int], socket.socket]


def build_msearch(search_target: str, mx: int = 1) -> bytes:
    return (
        'M-SEARCH * HTTP/1.1\r\n'
        f'HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n'
        'MAN: "ssdp:discover"\r\n'
        f'MX: {mx}\r\n'
        f'ST: {search_target}\r\n'
        '\r\n'
    ).encode()


def parse_msearch(data: bytes) -> str | None:
    """Return the search target of an SSDP ``M-SEARCH`` request, or ``None`` if ``data`` isn't one."""
    lines = data.decode('utf-8', errors='replace').split('\r\n')
    if not lines[0].upper().startswith('M-SEARCH '):
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(':')
        if sep:
            headers[key.strip().lower()] = value.strip()
    if headers.get('man', '').strip('"') != 'ssdp:discover':
        return None
    return headers.get('st') or None


class SsdpReplyCache:
    """The latest SSDP reply from each source device, per search target. Shared between threads."""

    def __init__(self) -> None:
        self._replies: dict[str, dict[str, bytes]] = {}
        self._lock = threading.Lock()

    def update(self, search_target: str, replies: dict[str, bytes]) -> bool:
        """Replace the replies for ``search_target``; return whether the set of devices changed.

        An empty result is ignored rather than wiping the cache, so a single missed search
        (devices busy, packet loss) doesn't make the devices disappear until the next refresh.
        """
        if not replies:
            return False
        with self._lock:
            previous = self._replies.get(search_target, {})
            self._replies[search_target] = dict(replies)
            return set(previous) != set(replies)

    def replies_for(self, search_target: str) -> list[bytes]:
        with self._lock:
            if search_target == SSDP_ALL:
                return [reply for replies in self._replies.values() for reply in replies.values()]
            return list(self._replies.get(search_target, {}).values())


class SearchResponder:
    """Decide which cached replies (if any) answer an incoming ``M-SEARCH``."""

    def __init__(
        self,
        cache: SsdpReplyCache,
        search_targets: Iterable[str],
        source_networks: Iterable[ipaddress.IPv4Network],
        clock: Callable[[], float] = time.monotonic,
        dedupe_seconds: float = DEDUPE_SECONDS,
    ) -> None:
        self._cache = cache
        self._search_targets = set(search_targets)
        self._source_networks = list(source_networks)
        self._clock = clock
        self._dedupe_seconds = dedupe_seconds
        self._recent: dict[tuple[str, int, str], float] = {}

    def answer(self, data: bytes, addr: tuple[str, int]) -> list[bytes]:
        search_target = parse_msearch(data)
        if search_target is None:
            return []
        if search_target != SSDP_ALL and search_target not in self._search_targets:
            return []
        if self._from_source_network(addr[0]):
            # Devices on the source network can reach the real devices themselves.
            return []
        if self._is_duplicate((addr[0], addr[1], search_target)):
            return []
        return self._cache.replies_for(search_target)

    def _from_source_network(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not isinstance(ip, ipaddress.IPv4Address) or any(ip in net for net in self._source_networks)

    def _is_duplicate(self, key: tuple[str, int, str]) -> bool:
        now = self._clock()
        self._recent = {k: seen for k, seen in self._recent.items() if now - seen < self._dedupe_seconds}
        if key in self._recent:
            return True
        self._recent[key] = now
        return False


class SsdpProxy:
    """Search the source interface for SSDP devices and answer searches for them on the destination.

    Runs two daemon threads: one refreshing the reply cache, one serving searches. Like the
    zeroconf browser callbacks, each loop iteration is guarded so an unexpected error is logged
    rather than silently killing the thread.
    """

    def __init__(
        self,
        source_ip: str,
        dest_ip: str,
        source_networks: Sequence[ipaddress.IPv4Network],
        search_targets: Sequence[str] = (ZONE_PLAYER,),
        refresh_seconds: float = REFRESH_SECONDS,
        collect_seconds: float = COLLECT_SECONDS,
        socket_factory: SocketFactory = socket.socket,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source_ip = source_ip
        self.dest_ip = dest_ip
        self.search_targets = list(search_targets)
        self.cache = SsdpReplyCache()
        self.responder = SearchResponder(self.cache, self.search_targets, source_networks, clock=clock)
        self._refresh_seconds = refresh_seconds
        self._collect_seconds = collect_seconds
        self._socket_factory = socket_factory
        self._clock = clock
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for target, name in ((self._refresh_loop, 'refresh'), (self._serve_loop, 'serve')):
            thread = threading.Thread(target=target, name=f'mdns-filoxy-ssdp-{name}', daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=self._collect_seconds + 2)

    def collect(self, search_target: str) -> dict[str, bytes]:
        """Search the source interface for ``search_target`` and return each device's reply."""
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.source_ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.source_ip))
            sock.settimeout(0.5)
            sock.sendto(build_msearch(search_target), (SSDP_GROUP, SSDP_PORT))
            replies: dict[str, bytes] = {}
            deadline = self._clock() + self._collect_seconds
            while self._clock() < deadline and not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                if data.upper().startswith(b'HTTP/1.1 200'):
                    replies[addr[0]] = data
            return replies
        finally:
            sock.close()

    def refresh(self) -> None:
        for search_target in self.search_targets:
            replies = self.collect(search_target)
            if self.cache.update(search_target, replies):
                logger.info(f'SSDP: {search_target} answered by {sorted(replies)}')
            elif not replies:
                logger.debug(f'SSDP: no replies for {search_target}, keeping previous ones')

    def open_listener(self) -> socket.socket:
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(('', SSDP_PORT))
        membership = struct.pack('4s4s', socket.inet_aton(SSDP_GROUP), socket.inet_aton(self.dest_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(1)
        return sock

    def handle_one(self, sock: socket.socket) -> None:
        """Wait (up to the socket timeout) for one datagram and answer it if appropriate."""
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            return
        replies = self.responder.answer(data, addr)
        for reply in replies:
            sock.sendto(reply, addr)
        if replies:
            logger.debug(f'SSDP: answered {addr[0]}:{addr[1]} with {len(replies)} replies')

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                logger.exception('SSDP: refreshing replies failed')
            self._stop.wait(self._refresh_seconds)

    def _serve_loop(self) -> None:
        try:
            sock = self.open_listener()
        except OSError:
            logger.exception(f'SSDP: cannot listen on {self.dest_ip}:{SSDP_PORT}, SSDP answering disabled')
            return
        logger.info(f'SSDP: answering {self.search_targets} searches on {self.dest_ip}')
        try:
            while not self._stop.is_set():
                try:
                    self.handle_one(sock)
                except Exception:
                    logger.exception('SSDP: answering a search failed')
                    self._stop.wait(1)  # don't spin if the socket itself is broken
        finally:
            sock.close()
