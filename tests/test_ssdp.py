from __future__ import annotations

import io
import ipaddress
import socket
import struct
import threading
import time
from collections.abc import Iterator
from typing import cast

import pytest
from loguru import logger

from mdns_filoxy.ssdp import (
    SSDP_ALL,
    SSDP_GROUP,
    SSDP_PORT,
    ZONE_PLAYER,
    SearchResponder,
    SsdpProxy,
    SsdpReplyCache,
    build_msearch,
    parse_msearch,
)

SOURCE_IP = '192.168.1.10'
DEST_IP = '10.0.0.1'
SOURCE_NET = ipaddress.IPv4Network('192.168.1.0/24')
SEARCHER = ('10.0.0.50', 50000)
OTHER_ST = 'urn:schemas-upnp-org:device:MediaRenderer:1'

REPLY_A = b'HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\nLOCATION: http://192.168.1.20/\r\n\r\n'
REPLY_B = b'HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\nLOCATION: http://192.168.1.21/\r\n\r\n'


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSocket:
    """Records socket calls; ``recvfrom`` pops scripted results, then raises ``socket.timeout``.

    Each ``recvfrom`` advances ``clock`` (if given) so loops bounded by the clock terminate.
    With ``idle_sleep`` an exhausted script sleeps briefly first, so threaded loops don't spin.
    """

    def __init__(
        self,
        script: list[tuple[bytes, tuple[str, int]] | BaseException] | None = None,
        clock: FakeClock | None = None,
        tick: float = 1.0,
        bind_error: BaseException | None = None,
        idle_sleep: float = 0.0,
    ) -> None:
        self.script = list(script or [])
        self.clock = clock
        self.tick = tick
        self.bind_error = bind_error
        self.idle_sleep = idle_sleep
        self.bound: tuple[str, int] | None = None
        self.sockopts: list[tuple[int, int, int | bytes]] = []
        self.timeout: float | None = None
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.recv_calls = 0
        self.closed = False

    def bind(self, address: tuple[str, int]) -> None:
        if self.bind_error is not None:
            raise self.bind_error
        self.bound = address

    def setsockopt(self, level: int, option: int, value: int | bytes) -> None:
        self.sockopts.append((level, option, value))

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        self.sent.append((data, address))
        return len(data)

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        self.recv_calls += 1
        if self.clock is not None:
            self.clock.advance(self.tick)
        if not self.script:
            if self.idle_sleep:
                time.sleep(self.idle_sleep)
            raise socket.timeout
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class SocketFactory:
    """``socket.socket`` replacement handing out queued fakes (or fresh idle ones when empty)."""

    def __init__(self, *sockets: FakeSocket, idle_sleep: float = 0.0) -> None:
        self.queue = list(sockets)
        self.idle_sleep = idle_sleep
        self.created: list[FakeSocket] = []
        self.calls: list[tuple[int, int]] = []
        self._lock = threading.Lock()

    def __call__(self, family: int, type_: int) -> socket.socket:
        with self._lock:
            self.calls.append((family, type_))
            sock = self.queue.pop(0) if self.queue else FakeSocket(idle_sleep=self.idle_sleep)
            self.created.append(sock)
        return cast(socket.socket, sock)


def make_proxy(
    factory: SocketFactory,
    clock: FakeClock | None = None,
    search_targets: tuple[str, ...] = (ZONE_PLAYER,),
    refresh_seconds: float = 120.0,
    collect_seconds: float = 3.0,
) -> SsdpProxy:
    return SsdpProxy(
        source_ip=SOURCE_IP,
        dest_ip=DEST_IP,
        source_networks=[SOURCE_NET],
        search_targets=search_targets,
        refresh_seconds=refresh_seconds,
        collect_seconds=collect_seconds,
        socket_factory=factory,
        clock=clock if clock is not None else FakeClock(),
    )


def msearch(search_target: str, man: str = '"ssdp:discover"') -> bytes:
    return (
        f'M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_GROUP}:{SSDP_PORT}\r\nMAN: {man}\r\nMX: 1\r\nST: {search_target}\r\n\r\n'
    ).encode()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    sink_id = logger.add(stream, level='DEBUG', format='{level.name}: {message}')
    yield stream
    logger.remove(sink_id)


# --- build_msearch / parse_msearch ---------------------------------------------------


@pytest.mark.parametrize('search_target', [ZONE_PLAYER, SSDP_ALL, OTHER_ST])
def test_build_and_parse_msearch_round_trip(search_target: str) -> None:
    assert parse_msearch(build_msearch(search_target)) == search_target


def test_build_msearch_contents() -> None:
    data = build_msearch(ZONE_PLAYER, mx=3)

    assert data.startswith(b'M-SEARCH * HTTP/1.1\r\n')
    assert f'HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n'.encode() in data
    assert b'MAN: "ssdp:discover"\r\n' in data
    assert b'MX: 3\r\n' in data
    assert data.endswith(f'ST: {ZONE_PLAYER}\r\n\r\n'.encode())


def test_parse_msearch_header_names_are_case_insensitive() -> None:
    data = f'm-search * HTTP/1.1\r\nman: "ssdp:discover"\r\nSt:  {ZONE_PLAYER} \r\n\r\n'.encode()

    assert parse_msearch(data) == ZONE_PLAYER


@pytest.mark.parametrize('man', ['"ssdp:discover"', 'ssdp:discover'])
def test_parse_msearch_accepts_quoted_or_bare_man(man: str) -> None:
    assert parse_msearch(msearch(ZONE_PLAYER, man=man)) == ZONE_PLAYER


@pytest.mark.parametrize('man', ['"ssdp:alive"', ''])
def test_parse_msearch_rejects_other_man(man: str) -> None:
    assert parse_msearch(msearch(ZONE_PLAYER, man=man)) is None


def test_parse_msearch_rejects_missing_man() -> None:
    assert parse_msearch(f'M-SEARCH * HTTP/1.1\r\nST: {ZONE_PLAYER}\r\n\r\n'.encode()) is None


@pytest.mark.parametrize(
    'data',
    [
        b'NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\nNTS: ssdp:alive\r\n\r\n',
        REPLY_A,
        b'',
    ],
)
def test_parse_msearch_ignores_non_msearch(data: bytes) -> None:
    assert parse_msearch(data) is None


@pytest.mark.parametrize('st_line', ['', 'ST:\r\n', 'ST:   \r\n'])
def test_parse_msearch_missing_or_empty_st_is_none(st_line: str) -> None:
    data = f'M-SEARCH * HTTP/1.1\r\nMAN: "ssdp:discover"\r\n{st_line}\r\n'.encode()

    assert parse_msearch(data) is None


@pytest.mark.parametrize('data', [b'\xff\xfe\x00\x81garbage', b'M-SEARCH \xff\r\n\xff:\xfe\r\n', b'\r\n\r\n:::'])
def test_parse_msearch_garbage_does_not_raise(data: bytes) -> None:
    assert parse_msearch(data) is None


# --- SsdpReplyCache ------------------------------------------------------------------


def test_cache_update_reports_change_only_when_device_set_changes() -> None:
    cache = SsdpReplyCache()

    assert cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A}) is True
    assert cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_B}) is False  # same device, new body
    assert cache.replies_for(ZONE_PLAYER) == [REPLY_B]
    assert cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_B, '192.168.1.21': REPLY_A}) is True
    assert cache.update(ZONE_PLAYER, {'192.168.1.21': REPLY_A}) is True


def test_cache_empty_update_is_ignored_and_keeps_previous() -> None:
    cache = SsdpReplyCache()
    cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})

    assert cache.update(ZONE_PLAYER, {}) is False
    assert cache.replies_for(ZONE_PLAYER) == [REPLY_A]


def test_cache_update_copies_replies() -> None:
    cache = SsdpReplyCache()
    replies = {'192.168.1.20': REPLY_A}
    cache.update(ZONE_PLAYER, replies)

    replies['192.168.1.21'] = REPLY_B

    assert cache.replies_for(ZONE_PLAYER) == [REPLY_A]


def test_cache_replies_for_ssdp_all_spans_targets() -> None:
    cache = SsdpReplyCache()
    cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})
    cache.update(OTHER_ST, {'192.168.1.21': REPLY_B})

    assert sorted(cache.replies_for(SSDP_ALL)) == sorted([REPLY_A, REPLY_B])
    assert cache.replies_for(ZONE_PLAYER) == [REPLY_A]


def test_cache_unknown_target_is_empty() -> None:
    cache = SsdpReplyCache()
    cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})

    assert cache.replies_for(OTHER_ST) == []
    assert SsdpReplyCache().replies_for(SSDP_ALL) == []


# --- SearchResponder -----------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(100.0)


@pytest.fixture
def responder(clock: FakeClock) -> SearchResponder:
    cache = SsdpReplyCache()
    cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})
    cache.update(OTHER_ST, {'192.168.1.21': REPLY_B})
    return SearchResponder(cache, [ZONE_PLAYER], [SOURCE_NET], clock=clock, dedupe_seconds=1.0)


def test_responder_answers_configured_target(responder: SearchResponder) -> None:
    assert responder.answer(msearch(ZONE_PLAYER), SEARCHER) == [REPLY_A]


def test_responder_answers_ssdp_all_with_everything_cached(responder: SearchResponder) -> None:
    assert sorted(responder.answer(msearch(SSDP_ALL), SEARCHER)) == sorted([REPLY_A, REPLY_B])


def test_responder_ignores_unconfigured_target(responder: SearchResponder) -> None:
    # Cached (e.g. via another responder), but not one this responder was configured for.
    assert responder.answer(msearch(OTHER_ST), SEARCHER) == []


@pytest.mark.parametrize('host', ['192.168.1.99', SOURCE_IP])
def test_responder_ignores_senders_on_source_network(responder: SearchResponder, host: str) -> None:
    assert responder.answer(msearch(ZONE_PLAYER), (host, 50000)) == []


@pytest.mark.parametrize('host', ['fe80::1', '::ffff:10.0.0.50', 'not-an-ip', ''])
def test_responder_ignores_ipv6_and_unparsable_hosts(responder: SearchResponder, host: str) -> None:
    assert responder.answer(msearch(ZONE_PLAYER), (host, 50000)) == []


def test_responder_ignores_non_msearch(responder: SearchResponder) -> None:
    assert responder.answer(REPLY_A, SEARCHER) == []
    assert responder.answer(b'NOTIFY * HTTP/1.1\r\n\r\n', SEARCHER) == []


def test_responder_dedupes_within_window_then_answers_again(responder: SearchResponder, clock: FakeClock) -> None:
    assert responder.answer(msearch(ZONE_PLAYER), SEARCHER) == [REPLY_A]
    clock.advance(0.5)
    assert responder.answer(msearch(ZONE_PLAYER), SEARCHER) == []
    clock.advance(0.5)  # 1.0 s after the first answer: window has closed
    assert responder.answer(msearch(ZONE_PLAYER), SEARCHER) == [REPLY_A]


def test_responder_different_port_or_target_is_not_duplicate(responder: SearchResponder) -> None:
    assert responder.answer(msearch(ZONE_PLAYER), SEARCHER) == [REPLY_A]
    assert responder.answer(msearch(ZONE_PLAYER), (SEARCHER[0], SEARCHER[1] + 1)) == [REPLY_A]
    assert sorted(responder.answer(msearch(SSDP_ALL), SEARCHER)) == sorted([REPLY_A, REPLY_B])


def test_responder_ignored_search_does_not_start_dedupe_window(responder: SearchResponder) -> None:
    responder.answer(msearch(OTHER_ST), SEARCHER)

    assert responder.answer(msearch(ZONE_PLAYER), SEARCHER) == [REPLY_A]


# --- SsdpProxy.collect / refresh -----------------------------------------------------


def test_collect_searches_source_interface_and_keeps_200_replies() -> None:
    clock = FakeClock()
    sock = FakeSocket(
        [
            (REPLY_A, ('192.168.1.20', 1400)),
            (b'NOTIFY * HTTP/1.1\r\n\r\n', ('192.168.1.30', 1900)),
            (b'HTTP/1.1 404 Not Found\r\n\r\n', ('192.168.1.31', 1400)),
            (REPLY_B, ('192.168.1.21', 1400)),
        ],
        clock=clock,
    )
    factory = SocketFactory(sock)
    proxy = make_proxy(factory, clock=clock, collect_seconds=10.0)

    replies = proxy.collect(ZONE_PLAYER)

    assert replies == {'192.168.1.20': REPLY_A, '192.168.1.21': REPLY_B}
    assert factory.calls == [(socket.AF_INET, socket.SOCK_DGRAM)]
    assert sock.bound == (SOURCE_IP, 0)
    assert (socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(SOURCE_IP)) in sock.sockopts
    assert sock.timeout == 0.5
    assert sock.sent == [(build_msearch(ZONE_PLAYER), (SSDP_GROUP, SSDP_PORT))]
    assert sock.closed


def test_collect_keys_replies_by_sender_ip_latest_wins() -> None:
    clock = FakeClock()
    sock = FakeSocket([(REPLY_A, ('192.168.1.20', 1400)), (REPLY_B, ('192.168.1.20', 1401))], clock=clock)
    proxy = make_proxy(SocketFactory(sock), clock=clock)

    assert proxy.collect(ZONE_PLAYER) == {'192.168.1.20': REPLY_B}


def test_collect_stops_at_deadline() -> None:
    clock = FakeClock()
    sock = FakeSocket(clock=clock, tick=1.0)  # every recv times out
    proxy = make_proxy(SocketFactory(sock), clock=clock, collect_seconds=3.0)

    assert proxy.collect(ZONE_PLAYER) == {}
    assert sock.recv_calls == 3
    assert sock.closed


def test_collect_ignores_replies_after_deadline() -> None:
    clock = FakeClock()
    sock = FakeSocket([socket.timeout(), socket.timeout(), (REPLY_A, ('192.168.1.20', 1400))], clock=clock)
    proxy = make_proxy(SocketFactory(sock), clock=clock, collect_seconds=2.0)

    assert proxy.collect(ZONE_PLAYER) == {}


def test_collect_stops_early_once_closed() -> None:
    clock = FakeClock()
    sock = FakeSocket(clock=clock)
    proxy = make_proxy(SocketFactory(sock), clock=clock)
    proxy.close()  # sets the stop flag; no threads were started

    assert proxy.collect(ZONE_PLAYER) == {}
    assert sock.recv_calls == 0
    assert sock.closed


def test_collect_closes_socket_on_error() -> None:
    sock = FakeSocket(bind_error=OSError('cannot bind'))
    proxy = make_proxy(SocketFactory(sock))

    with pytest.raises(OSError, match='cannot bind'):
        proxy.collect(ZONE_PLAYER)

    assert sock.closed


def test_collect_closes_socket_on_recv_error() -> None:
    clock = FakeClock()
    sock = FakeSocket([OSError('network down')], clock=clock)
    proxy = make_proxy(SocketFactory(sock), clock=clock)

    with pytest.raises(OSError, match='network down'):
        proxy.collect(ZONE_PLAYER)

    assert sock.closed


def test_refresh_updates_cache_and_logs_info_only_on_change(log_stream: io.StringIO) -> None:
    clock = FakeClock()
    first = FakeSocket([(REPLY_A, ('192.168.1.20', 1400))], clock=clock)
    same = FakeSocket([(REPLY_A, ('192.168.1.20', 1400))], clock=clock)
    proxy = make_proxy(SocketFactory(first, same), clock=clock)

    proxy.refresh()
    proxy.refresh()

    assert proxy.cache.replies_for(ZONE_PLAYER) == [REPLY_A]
    output = log_stream.getvalue()
    assert output.count('INFO: ') == 1
    assert f"INFO: SSDP: {ZONE_PLAYER} answered by ['192.168.1.20']" in output


def test_refresh_without_replies_keeps_cache_and_logs_debug(log_stream: io.StringIO) -> None:
    clock = FakeClock()
    first = FakeSocket([(REPLY_A, ('192.168.1.20', 1400))], clock=clock)
    empty = FakeSocket(clock=clock)
    proxy = make_proxy(SocketFactory(first, empty), clock=clock)

    proxy.refresh()
    proxy.refresh()

    assert proxy.cache.replies_for(ZONE_PLAYER) == [REPLY_A]
    assert f'DEBUG: SSDP: no replies for {ZONE_PLAYER}, keeping previous ones' in log_stream.getvalue()


def test_refresh_searches_every_target() -> None:
    clock = FakeClock()
    zp = FakeSocket([(REPLY_A, ('192.168.1.20', 1400))], clock=clock)
    other = FakeSocket([(REPLY_B, ('192.168.1.21', 1400))], clock=clock)
    factory = SocketFactory(zp, other)
    proxy = make_proxy(factory, clock=clock, search_targets=(ZONE_PLAYER, OTHER_ST))

    proxy.refresh()

    assert zp.sent[0][0] == build_msearch(ZONE_PLAYER)
    assert other.sent[0][0] == build_msearch(OTHER_ST)
    assert proxy.cache.replies_for(OTHER_ST) == [REPLY_B]


# --- SsdpProxy.open_listener / handle_one --------------------------------------------


def test_open_listener_binds_ssdp_port_and_joins_group_on_dest() -> None:
    sock = FakeSocket()
    proxy = make_proxy(SocketFactory(sock))

    assert proxy.open_listener() is cast(socket.socket, sock)

    assert sock.bound == ('', SSDP_PORT)
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in sock.sockopts
    if hasattr(socket, 'SO_REUSEPORT'):
        assert (socket.SOL_SOCKET, socket.SO_REUSEPORT, 1) in sock.sockopts
    membership = struct.pack('4s4s', socket.inet_aton(SSDP_GROUP), socket.inet_aton(DEST_IP))
    assert (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership) in sock.sockopts
    assert sock.timeout == 1


def test_open_listener_without_so_reuseport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(socket, 'SO_REUSEPORT', raising=False)
    sock = FakeSocket()
    proxy = make_proxy(SocketFactory(sock))

    proxy.open_listener()

    assert [opt[:2] for opt in sock.sockopts] == [
        (socket.SOL_SOCKET, socket.SO_REUSEADDR),
        (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP),
    ]


def test_handle_one_sends_each_cached_reply_unicast(log_stream: io.StringIO) -> None:
    proxy = make_proxy(SocketFactory())
    proxy.cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A, '192.168.1.21': REPLY_B})
    sock = FakeSocket([(msearch(ZONE_PLAYER), SEARCHER)])

    proxy.handle_one(cast(socket.socket, sock))

    assert sorted(sock.sent) == sorted([(REPLY_A, SEARCHER), (REPLY_B, SEARCHER)])
    assert f'DEBUG: SSDP: answered {SEARCHER[0]}:{SEARCHER[1]} with 2 replies' in log_stream.getvalue()


def test_handle_one_timeout_does_nothing() -> None:
    proxy = make_proxy(SocketFactory())
    proxy.cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})
    sock = FakeSocket()

    proxy.handle_one(cast(socket.socket, sock))

    assert sock.sent == []


@pytest.mark.parametrize(
    ('data', 'addr'),
    [
        (msearch(OTHER_ST), SEARCHER),
        (msearch(ZONE_PLAYER), ('192.168.1.99', 50000)),
        (REPLY_A, SEARCHER),
    ],
)
def test_handle_one_ignored_search_sends_nothing(log_stream: io.StringIO, data: bytes, addr: tuple[str, int]) -> None:
    proxy = make_proxy(SocketFactory())
    proxy.cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})
    sock = FakeSocket([(data, addr)])

    proxy.handle_one(cast(socket.socket, sock))

    assert sock.sent == []
    assert 'answered' not in log_stream.getvalue()


def test_handle_one_with_empty_cache_sends_nothing() -> None:
    proxy = make_proxy(SocketFactory())
    sock = FakeSocket([(msearch(ZONE_PLAYER), SEARCHER)])

    proxy.handle_one(cast(socket.socket, sock))

    assert sock.sent == []


# --- SsdpProxy loops and threads -----------------------------------------------------


def test_serve_loop_logs_and_exits_when_listener_cannot_open(log_stream: io.StringIO) -> None:
    sock = FakeSocket(bind_error=OSError('address in use'))
    proxy = make_proxy(SocketFactory(sock))

    proxy._serve_loop()

    output = log_stream.getvalue()
    assert f'ERROR: SSDP: cannot listen on {DEST_IP}:{SSDP_PORT}, SSDP answering disabled' in output
    assert 'answering [' not in output


def test_serve_loop_answers_until_stopped_and_closes_socket(log_stream: io.StringIO) -> None:
    proxy = make_proxy(SocketFactory())
    proxy.cache.update(ZONE_PLAYER, {'192.168.1.20': REPLY_A})
    listener = FakeSocket([(msearch(ZONE_PLAYER), SEARCHER)])
    proxy.open_listener = lambda: cast(socket.socket, listener)  # type: ignore[method-assign]
    real_handle_one = proxy.handle_one

    def handle_then_stop(sock: socket.socket) -> None:
        real_handle_one(sock)
        proxy._stop.set()

    proxy.handle_one = handle_then_stop  # type: ignore[method-assign]

    proxy._serve_loop()

    assert listener.sent == [(REPLY_A, SEARCHER)]
    assert listener.closed
    assert f"INFO: SSDP: answering ['{ZONE_PLAYER}'] searches on {DEST_IP}" in log_stream.getvalue()


def test_serve_loop_survives_handle_errors(log_stream: io.StringIO) -> None:
    proxy = make_proxy(SocketFactory())
    listener = FakeSocket()
    proxy.open_listener = lambda: cast(socket.socket, listener)  # type: ignore[method-assign]
    calls: list[int] = []

    def flaky(sock: socket.socket) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('boom')
        proxy._stop.set()

    proxy.handle_one = flaky  # type: ignore[method-assign]
    # The loop backs off with ``_stop.wait(1)`` after an error; don't really wait.
    proxy._stop.wait = lambda timeout=None: False  # type: ignore[method-assign]

    proxy._serve_loop()

    assert len(calls) == 2
    assert listener.closed
    assert 'ERROR: SSDP: answering a search failed' in log_stream.getvalue()


def test_serve_loop_closes_socket_on_unexpected_base_exception() -> None:
    proxy = make_proxy(SocketFactory())
    listener = FakeSocket([KeyboardInterrupt()])
    proxy.open_listener = lambda: cast(socket.socket, listener)  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        proxy._serve_loop()

    assert listener.closed


def test_refresh_loop_survives_refresh_errors(log_stream: io.StringIO) -> None:
    proxy = make_proxy(SocketFactory(), refresh_seconds=0)
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('boom')
        proxy._stop.set()

    proxy.refresh = flaky  # type: ignore[method-assign]

    proxy._refresh_loop()

    assert len(calls) == 2
    assert 'ERROR: SSDP: refreshing replies failed' in log_stream.getvalue()


def test_start_and_close_run_and_stop_both_threads_promptly() -> None:
    factory = SocketFactory(idle_sleep=0.001)
    proxy = SsdpProxy(
        source_ip=SOURCE_IP,
        dest_ip=DEST_IP,
        source_networks=[SOURCE_NET],
        refresh_seconds=0.01,
        collect_seconds=0.01,
        socket_factory=factory,
    )

    proxy.start()
    try:
        threads = list(proxy._threads)
        assert sorted(thread.name for thread in threads) == ['mdns-filoxy-ssdp-refresh', 'mdns-filoxy-ssdp-serve']
        assert all(thread.daemon for thread in threads)
        deadline = time.monotonic() + 2
        while not any(sock.bound == ('', SSDP_PORT) for sock in factory.created) and time.monotonic() < deadline:
            time.sleep(0.001)
    finally:
        started = time.monotonic()
        proxy.close()

    assert time.monotonic() - started < 1.5
    assert not any(thread.is_alive() for thread in threads)
    assert any(sock.bound == ('', SSDP_PORT) for sock in factory.created)  # serve loop opened its listener
    assert any(sock.bound == (SOURCE_IP, 0) for sock in factory.created)  # refresh loop searched
    assert all(sock.closed for sock in factory.created)
