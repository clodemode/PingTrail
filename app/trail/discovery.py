"""Ladder discovery — build a Trail's rungs from the host's actual network.

Runs on the HOST (macOS), never in the container: `route -n get default` and
`traceroute` inside Docker describe the bridge network, not the house router.

Every step degrades gracefully. A rung that cannot be discovered is reported as
a skip with a plain-language reason and the rest of the ladder is still built —
a half-discovered trail is useful; a crashed command is not.

CLASSIFICATION IS BY ADDRESS SPACE, NOT BY POSITION
---------------------------------------------------
v1 labelled traceroute hop 2 `isp_hop1` because it was hop 2. On a double-NAT
house, hop 2 is a private address — the DSL router, still in the house — so the
headline attribution read "ISP" for a segment the operator owns. That is worse
than a missing label: the
measurement was right and the story was wrong.

A private address can NEVER be an ISP rung. RFC1918 (10/8, 172.16/12,
192.168/16) and CGNAT (100.64/10) are, by definition, not routable across the
public internet, so a reply from one came from equipment on this side of the
demarcation point. `classify_host` decides on address space alone and hop
position never enters into it.
"""
import ipaddress
import re
import subprocess

DEFAULT_TRACE_TARGET = "1.1.1.1"
DEFAULT_PUBLIC_DNS = ("1.1.1.1", "8.8.8.8")
# K-root (RIPE NCC). Stable, well-known, answers ICMP, and far enough off-net to
# represent long-haul transit. Override with --anchor.
DEFAULT_ANCHOR = "193.0.14.129"
LOOPBACK = "127.0.0.1"

_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class DiscoveryNote:
    """A single human-readable outcome line from discovery.

    `degraded` is not merely a louder `warn`: it means this pass could not SEE
    part of the path, so its silence about a rung is no evidence that the rung is
    gone. `discover_trail` reads it and declines to retire anything.
    """

    LEVELS = ("ok", "skip", "warn", "degraded")

    def __init__(self, level, message):
        self.level = level
        self.message = message

    def __str__(self):
        prefix = {
            "ok": "  ok  ",
            "skip": " skip ",
            "warn": " warn ",
            "degraded": " DEGR ",
        }.get(self.level, "      ")
        return f"[{prefix}] {self.message}"


def is_degraded(notes):
    """True when discovery could not see enough of the path to judge absences."""
    return any(note.level == "degraded" for note in notes)


def _run(cmd, timeout=20):
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(cmd)}: timed out after {timeout}s"


def is_ipv4(value):
    if not value or not _IPV4.match(value):
        return False
    return all(0 <= int(part) <= 255 for part in value.split("."))


# 100.64.0.0/10 — RFC6598 Carrier-Grade NAT. `ipaddress.is_private` reports
# True for it in modern Pythons, but it is checked explicitly here so the rule
# does not silently depend on a stdlib classification that has changed before.
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_private_address(host):
    """True for anything that cannot be a public internet device.

    Loopback, RFC1918, CGNAT, link-local. If this is True the address is NOT an
    ISP hop — full stop, whatever traceroute position it turned up at.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr in CGNAT


def classify_host(host, gateway=None):
    """Kind for an address, decided PURELY by address space.

    Order matters and encodes the rule:
      loopback      127/8
      gateway       the host's actual default gateway (identity, not a range)
      home_router   any other private/CGNAT address — still inside the house
      isp_hop       anything public

    `isp_dns` / `public_dns` / `anchor` are ROLES chosen by how the address was
    sourced (resolv.conf, the configured public resolvers, the anchor), not by
    its shape — so they are applied by the caller and then re-checked against
    this function: a private resolver is a home router doing DNS, never "ISP
    infrastructure".
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if addr.is_loopback:
        return "loopback"
    if gateway and host == gateway:
        return "gateway"
    if addr.is_private or addr in CGNAT:
        return "home_router"
    return "isp_hop"


def default_gateway():
    """Read the real default gateway. macOS: `route -n get default`."""
    code, out, err = _run(["/sbin/route", "-n", "get", "default"], timeout=10)
    if code != 0:
        return None, f"route -n get default failed: {err.strip() or code}"
    for line in out.splitlines():
        if "gateway:" in line:
            candidate = line.split("gateway:", 1)[1].strip()
            if is_ipv4(candidate):
                return candidate, None
            return None, f"gateway is not IPv4: {candidate!r}"
    return None, "no gateway line in `route -n get default` (offline, or VPN-only route?)"


def traceroute_hops(target=DEFAULT_TRACE_TARGET, max_hops=6, timeout=90):
    """Return [(hop_number, ip)] toward `target`. Unresponsive hops are omitted.

    Deliberately patient (-q 2 -w 2): discovery runs rarely, and a hop that
    merely missed a 1-second window would silently shorten the ladder — losing
    the isp_hop2 rung that usually carries the interesting delta.
    """
    code, out, err = _run(
        ["/usr/sbin/traceroute", "-n", "-q", "2", "-w", "2", "-m", str(max_hops), target],
        timeout=timeout,
    )
    if code not in (0, 1) and not out:
        return [], f"traceroute failed: {err.strip() or code}"
    hops = []
    for line in out.splitlines():
        line = line.strip()
        match = re.match(r"^(\d+)\s+(\S+)", line)
        if not match:
            continue
        hop_no, addr = int(match.group(1)), match.group(2)
        if addr == "*" or not is_ipv4(addr):
            continue  # hop hides from traceroute; that is not an error
        hops.append((hop_no, addr))
    if not hops:
        return [], f"traceroute to {target} returned no addressable hops"
    return hops, None


def resolvers(path="/etc/resolv.conf"):
    """Parse nameservers from resolv.conf.

    macOS note: resolv.conf is a generated summary and is not what most
    processes consult (scutil --dns is authoritative). It is still the right
    file per spec, and in practice it carries the active resolver.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return [], f"could not read {path}: {exc}"
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line.startswith("nameserver"):
            continue
        parts = line.split()
        if len(parts) >= 2 and is_ipv4(parts[1]) and parts[1] not in found:
            found.append(parts[1])
    if not found:
        return [], f"no IPv4 nameserver lines in {path}"
    return found, None


def _number_labels(kinds):
    """Label each onward hop by its KIND, suffixed only when it has company.

    One ISP hop is `isp_hop`, not `isp_hop1` — a bare number implies a sibling
    that does not exist. Two get `isp_hop1` / `isp_hop2`. Same for home routers.
    Labels feed the rollup's grouping and the headline, so they have to read as
    plain English on the dashboard.
    """
    counts = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    seen = {}
    labels = []
    for kind in kinds:
        seen[kind] = seen.get(kind, 0) + 1
        labels.append(kind if counts[kind] == 1 else f"{kind}{seen[kind]}")
    return labels


def build_ladder(
    trace_target=DEFAULT_TRACE_TARGET,
    public_dns=DEFAULT_PUBLIC_DNS,
    anchor=DEFAULT_ANCHOR,
):
    """Assemble the ordered ladder. Returns (rungs, notes).

    rungs: [{depth, kind, host, label, note}] — ordered, de-duplicated by host.
    notes: [DiscoveryNote] — one line per discovery decision, including skips.
    """
    notes = []
    rungs = []
    claimed = {}  # host -> label of the rung that already claimed it

    def add(depth, kind, host, label, note=""):
        if not is_ipv4(host):
            notes.append(DiscoveryNote("skip", f"{label}: {host!r} is not an IPv4 address"))
            return False
        if host in claimed:
            # A genuine finding, not an error: e.g. the configured resolver IS
            # 1.1.1.1, so there is no distinct ISP-resolver rung to measure.
            notes.append(
                DiscoveryNote(
                    "skip",
                    f"{label}: {host} already on the ladder as '{claimed[host]}' — "
                    f"no distinct rung to measure",
                )
            )
            return False
        claimed[host] = label
        rungs.append({"depth": depth, "kind": kind, "host": host, "label": label, "note": note})
        notes.append(DiscoveryNote("ok", f"depth {depth} {label:<10} {host}"))
        return True

    # depth 0 — host scheduling noise floor
    add(0, "loopback", LOOPBACK, "loopback", "host scheduling noise floor")

    # depth 1 — LAN + router
    gateway, gw_err = default_gateway()
    if gateway:
        add(1, "gateway", gateway, "gateway", "LAN + router responsiveness")
    else:
        notes.append(DiscoveryNote("skip", f"gateway: {gw_err}"))

    # depth 2/3 — the path onward from the gateway.
    #
    # NOT automatically "the ISP" — each hop is classified on its own address.
    # Behind a second router, hop 2 is the DSL box: a home_router, not a last mile.
    hops, trace_err = traceroute_hops(target=trace_target)
    if trace_err:
        # Traceroute told us nothing, so it cannot be evidence that a rung is
        # gone. Degraded, not merely skipped.
        notes.append(DiscoveryNote("degraded", f"onward hops: {trace_err}"))
    else:
        # Hop 1 is the gateway we already have; the path onward starts after it.
        onward = [(n, ip) for n, ip in hops if ip != gateway and n > 1]
        if not onward:
            notes.append(
                DiscoveryNote(
                    "degraded",
                    f"onward hops: traceroute to {trace_target} revealed no hop "
                    f"beyond the gateway (every upstream hop hid from traceroute)",
                )
            )
        chosen = onward[:2]
        kinds = [classify_host(addr, gateway=gateway) for _hop, addr in chosen]
        labels = _number_labels(kinds)
        for position, (hop_no, addr) in enumerate(chosen):
            kind = kinds[position]
            if kind == "home_router":
                blurb = "ANOTHER ROUTER IN THE HOUSE — private address, cannot be an ISP hop"
            else:
                blurb = (
                    "first public address — the last mile begins here"
                    if "isp_hop" not in kinds[:position]
                    else "ISP backhaul / regional aggregation"
                )
            add(2 + position, kind, addr, labels[position], f"traceroute hop {hop_no} — {blurb}")

        if chosen and "isp_hop" not in kinds:
            notes.append(
                DiscoveryNote(
                    "degraded",
                    f"no PUBLIC hop found within {len(chosen)} hop(s) of the gateway — every "
                    f"onward hop is a private address, so this pass cannot see the ISP at all. "
                    f"Existing rungs will be LEFT ALONE rather than retired on this evidence.",
                )
            )
        elif len(onward) < 2:
            notes.append(
                DiscoveryNote(
                    "warn",
                    f"only {len(onward)} hop(s) discoverable beyond the gateway toward "
                    f"{trace_target}; ladder is shorter than the full spec ladder",
                )
            )

    # depth 4 — the configured resolver.
    #
    # ROLE says "resolver"; ADDRESS SPACE says whose box it is. A private
    # resolver is the house router answering DNS, and calling that "ISP
    # infrastructure" would repeat the exact defect this spec exists to fix.
    resolver_list, resolver_err = resolvers()
    if resolver_err:
        notes.append(DiscoveryNote("skip", f"resolver rung: {resolver_err}"))
    else:
        first = resolver_list[0]
        if is_private_address(first):
            add(4, "home_router", first, "local_dns", "resolver from /etc/resolv.conf — private address, in-house")
        else:
            add(4, "isp_dns", first, "isp_dns", "resolver from /etc/resolv.conf")
        if len(resolver_list) > 1:
            notes.append(
                DiscoveryNote("warn", f"additional resolvers not laddered: {', '.join(resolver_list[1:])}")
            )

    # depth 5 — open-internet edge (BOTH public resolvers share this depth)
    for host in public_dns:
        add(5, "public_dns", host, "public_dns", "open-internet edge")

    # depth 6 — long-haul transit
    if anchor:
        add(6, "anchor", anchor, "anchor", "distant well-known host — long-haul transit")
    else:
        notes.append(DiscoveryNote("skip", "anchor: no anchor host configured"))

    if not rungs:
        notes.append(DiscoveryNote("warn", "no rungs discovered at all — is the host offline?"))

    return rungs, notes
