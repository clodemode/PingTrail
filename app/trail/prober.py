"""Host-side ICMP prober — parallel fire, one shot.

SIMULTANEITY IS THE PREMISE. Every rung is fired in the same instant so that all
of them experience the same network conditions; only then are the RTT
differences between rungs attributable to the segments between them. Probing
sequentially compares different moments and makes the subtraction meaningless.
So: open one socket per target, send every echo request in a tight loop, THEN
wait on all of them concurrently with select(). No staggering, no per-target
timeout serialisation.

PRIVILEGE. Unprivileged ICMP datagram sockets (SOCK_DGRAM / IPPROTO_ICMP) need
no root on macOS and are the preferred path. Shelling out to /sbin/ping is the
fallback only when the datagram socket cannot be opened — and the fallback still
fires every rung in parallel.

TWO macOS QUIRKS, both verified on Darwin 25.5.0 (2026-08-01) and both caught by
this module's own smoke test rather than by reading docs:

1. A SOCK_DGRAM ICMP read returns the packet WITH its 20-byte IPv4 header still
   attached (first byte 0x45); Linux strips it and hands back bare ICMP.
   `_icmp_offset` detects which shape arrived instead of assuming either.

2. EVERY open ICMP datagram socket in the process is handed a copy of EVERY
   echo reply — the kernel does not demultiplex them per socket, and it rewrites
   the ICMP id on SOCK_DGRAM so the id cannot demultiplex them either. Matching
   a reply to a rung by "which socket woke up" is therefore WRONG: the symptom
   is every rung reporting the loopback rung's RTT, monotonically decreasing in
   send order, with ttl=64 across the board — a plausible-looking table that is
   entirely fiction. Replies are matched by SOURCE ADDRESS, and each packet is
   timestamped as it is read rather than once per select() round.

THIS MODULE MUST NOT RUN IN THE CONTAINER. Inside Docker the default gateway is
the bridge (172.x.x.1), not the house router, and on macOS the Docker "host" is
a Linux VM behind its own NAT — a containerized prober measures a fiction.
"""
import os
import select
import socket
import struct
import subprocess
import time

ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
ICMP_DEST_UNREACH = 3
ICMP_TIME_EXCEEDED = 11

_ICMP_ERRORS = {
    ICMP_DEST_UNREACH: "destination unreachable",
    ICMP_TIME_EXCEEDED: "ttl exceeded in transit",
    5: "redirect",
}


class ProbeResult:
    """One rung's outcome for one tick. rtt_ms is None for a timeout."""

    __slots__ = ("host", "rtt_ms", "sent", "received", "loss_pct", "ttl", "error")

    def __init__(self, host, rtt_ms=None, sent=1, received=0, ttl=None, error=""):
        self.host = host
        self.rtt_ms = rtt_ms
        self.sent = sent
        self.received = received
        self.loss_pct = 100.0 * (sent - received) / sent if sent else 100.0
        self.ttl = ttl
        self.error = error

    def as_dict(self):
        return {
            "host": self.host,
            "rtt_ms": self.rtt_ms,
            "sent": self.sent,
            "received": self.received,
            "loss_pct": round(self.loss_pct, 2),
            "ttl": self.ttl,
            "error": self.error,
        }

    def __repr__(self):
        rtt = "timeout" if self.rtt_ms is None else f"{self.rtt_ms:.3f}ms"
        return f"<ProbeResult {self.host} {rtt}>"


def _checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _build_echo(ident, seq, payload=b"ping_trail"):
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, ident, seq)
    chk = _checksum(header + payload)
    return struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, chk, ident, seq) + payload


def _icmp_offset(data):
    """Return (icmp_start, ttl). macOS keeps the IP header, Linux strips it."""
    if len(data) >= 20 and (data[0] >> 4) == 4:
        ihl = (data[0] & 0x0F) * 4
        return ihl, data[8]
    return 0, None


def _error_original_destination(data, offset):
    """For an ICMP error, dig out the destination of the packet that caused it.

    An ICMP error carries [8-byte ICMP header][original IPv4 header][8 bytes of
    the original payload]. The original DESTINATION (bytes 16..20 of that inner
    header) is the rung the error is really about — the error's own source
    address is whichever router generated it, so source matching would misfile
    it. Returns a dotted-quad string, or None if the quotation is truncated.
    """
    inner = offset + 8
    if len(data) < inner + 20 or (data[inner] >> 4) != 4:
        return None
    return socket.inet_ntoa(data[inner + 16 : inner + 20])


def sweep_icmp(hosts, timeout=2.0, payload_size=32):
    """Fire ICMP echo at every host simultaneously via unprivileged datagram sockets.

    Raises OSError if the sockets cannot be opened at all — the caller then falls
    back to `sweep_subprocess`.
    """
    hosts = list(dict.fromkeys(hosts))  # de-dupe, order-preserving
    payload = (b"ping_trail" * 8)[:payload_size]
    ident = os.getpid() & 0xFFFF

    sockets = {}
    results = {}
    try:
        for index, host in enumerate(hosts):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
                sock.setblocking(False)
                sockets[sock] = host
            except OSError as exc:
                if not sockets:
                    raise
                results[host] = ProbeResult(host, error=f"socket: {exc.strerror or exc}")

        if not sockets:
            raise OSError("no ICMP datagram sockets could be opened")

        # ---- TIGHT SEND LOOP: this is the simultaneity guarantee ----
        sent_at = {}
        for index, (sock, host) in enumerate(sockets.items()):
            packet = _build_echo(ident, index + 1, payload)
            try:
                sent_at[sock] = time.perf_counter()
                sock.sendto(packet, (host, 0))
            except OSError as exc:
                results[host] = ProbeResult(host, error=f"send: {exc.strerror or exc}")

        # ---- CONCURRENT WAIT: every socket read in the same window ----
        # A reply is matched to a rung by SOURCE ADDRESS, never by which socket
        # woke up — macOS hands every socket a copy of every reply (see the
        # module docstring). Send times are tracked per HOST for the same reason.
        send_time_by_host = {sockets[s]: t for s, t in sent_at.items()}
        awaiting = {h for h in send_time_by_host if h not in results}
        watch = [s for s in sockets if s in sent_at]
        deadline = time.perf_counter() + timeout

        while awaiting:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            readable, _, _ = select.select(watch, [], [], remaining)
            if not readable:
                break
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    continue
                # Timestamp per packet, not per select() round.
                now = time.perf_counter()
                source = addr[0]
                offset, ttl = _icmp_offset(data)
                if len(data) < offset + 8:
                    continue  # runt
                icmp_type = data[offset]

                if icmp_type == ICMP_ECHO_REPLY:
                    host = source if source in awaiting else None
                    if host is None:
                        continue  # duplicate copy, or a rung already resolved
                    rtt = (now - send_time_by_host[host]) * 1000.0
                    results[host] = ProbeResult(host, rtt_ms=rtt, received=1, ttl=ttl)
                    awaiting.discard(host)
                elif icmp_type in _ICMP_ERRORS:
                    # Attribute to the ORIGINAL destination, not the router that
                    # sent the error.
                    host = _error_original_destination(data, offset)
                    if host is None or host not in awaiting:
                        continue
                    results[host] = ProbeResult(
                        host,
                        ttl=ttl,
                        error=f"{_ICMP_ERRORS[icmp_type]} (from {source})",
                    )
                    awaiting.discard(host)

        # Anything still awaiting timed out. Loss is DATA — emit the row.
        for host in awaiting:
            results.setdefault(host, ProbeResult(host, error="timeout"))
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    for host in hosts:
        results.setdefault(host, ProbeResult(host, error="timeout"))
    return [results[h] for h in hosts]


def sweep_subprocess(hosts, timeout=2.0):
    """Fallback: parallel `ping` subprocesses. Still fires every rung at once."""
    hosts = list(dict.fromkeys(hosts))
    wait = max(1, int(round(timeout)))

    procs = {}
    # Spawn every process first, THEN collect — spawning and waiting one at a
    # time would serialise the sweep and void the attribution.
    for host in hosts:
        try:
            procs[host] = subprocess.Popen(
                ["/sbin/ping", "-c", "1", "-W", str(int(timeout * 1000)), "-t", str(wait), host],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            procs[host] = exc

    results = []
    for host, proc in procs.items():
        if isinstance(proc, OSError):
            results.append(ProbeResult(host, error=f"spawn: {proc}"))
            continue
        try:
            out, _ = proc.communicate(timeout=timeout + 2)
        except subprocess.TimeoutExpired:
            proc.kill()
            results.append(ProbeResult(host, error="timeout"))
            continue
        rtt = None
        ttl = None
        for line in out.splitlines():
            if "time=" in line:
                try:
                    rtt = float(line.split("time=")[1].split()[0])
                except (IndexError, ValueError):
                    rtt = None
            if "ttl=" in line:
                try:
                    ttl = int(line.split("ttl=")[1].split()[0])
                except (IndexError, ValueError):
                    ttl = None
        if rtt is None:
            results.append(ProbeResult(host, ttl=ttl, error="timeout"))
        else:
            results.append(ProbeResult(host, rtt_ms=rtt, received=1, ttl=ttl))
    return results


def sweep(hosts, timeout=2.0):
    """Parallel sweep with automatic fallback. Returns (results, method)."""
    try:
        return sweep_icmp(hosts, timeout=timeout), "icmp_dgram"
    except OSError as exc:
        results = sweep_subprocess(hosts, timeout=timeout)
        for result in results:
            if not result.error:
                continue
        return results, f"subprocess (icmp_dgram unavailable: {exc})"
