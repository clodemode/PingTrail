"""PING_TRAIL data model.

Vantage -> Trail -> Rung  (the ladder: what we probe, and how deep)
Trail   -> Sample -> Probe (the measurements: one tick, one result per rung)
Vantage -> ProberControl   (marching orders + heartbeat: the pull control plane)

Per spec-ping-trail-hop-ladder-latency-attribution:
  * every raw RTT is persisted per rung; deltas are computed at render, never
    stored pre-clamped;
  * Probe.rtt_ms NULL means TIMEOUT and is distinct from 0.0;
  * loss is first-class data (a Probe row with rtt_ms=NULL), never a missing row.

Per spec-ping-trail-control-plane-and-rfc1918-classification:
  * a PRIVATE address (RFC1918 + CGNAT) can never be an ISP rung — `home_router`
    exists so the DSL/cable box inside the house is named honestly;
  * `kind_pinned` / `label_pinned` let an operator correct discovery's guess and
    have the correction survive the next `discover_trail`;
  * ProberControl is PULL-only: the app never starts, stops or signals the host
    prober. It publishes orders; the prober asks for them and heartbeats back.
"""
from django.db import models
from django.utils import timezone


class RungKind(models.TextChoices):
    LOOPBACK = "loopback", "Loopback"
    GATEWAY = "gateway", "Gateway"
    # A private address beyond the default gateway — a second router still
    # inside the house (Kris: 192.168.11.1 inner -> 192.168.1.1 DSL box).
    # NEVER an ISP rung, no matter where traceroute found it.
    HOME_ROUTER = "home_router", "Home router"
    ISP_HOP = "isp_hop", "ISP hop"
    ISP_DNS = "isp_dns", "ISP resolver"
    PUBLIC_DNS = "public_dns", "Public DNS"
    ANCHOR = "anchor", "Distant anchor"


# Everything still inside the house. Used by the headline attribution so the
# answer reads "in-house" or "ISP" rather than leaving the operator to infer it
# from a label — the v1 defect was exactly that inference going wrong.
IN_HOUSE_KINDS = frozenset({RungKind.LOOPBACK, RungKind.GATEWAY, RungKind.HOME_ROUTER})


def is_in_house(kind):
    """True when this rung is equipment Kris owns, on Kris's side of the demarc."""
    return kind in IN_HOUSE_KINDS


# Display metadata per kind — icon + semantic colour for the surface.
# Kept beside the choices so the chart, the table and the report agree.
# Colour family carries the in-house/ISP split: cool = in the house, warm =
# out of the house.
KIND_META = {
    RungKind.LOOPBACK: {"icon": "fa-circle-dot", "colour": "#adb5bd", "blurb": "host scheduling noise floor"},
    RungKind.GATEWAY: {"icon": "fa-house-signal", "colour": "#0d6efd", "blurb": "LAN + default gateway responsiveness"},
    RungKind.HOME_ROUTER: {
        "icon": "fa-network-wired",
        "colour": "#0dcaf0",
        "blurb": "another router INSIDE the house (private address)",
    },
    RungKind.ISP_HOP: {"icon": "fa-tower-broadcast", "colour": "#fd7e14", "blurb": "last mile / ISP backhaul"},
    RungKind.ISP_DNS: {"icon": "fa-address-book", "colour": "#6f42c1", "blurb": "ISP infrastructure"},
    RungKind.PUBLIC_DNS: {"icon": "fa-globe", "colour": "#20c997", "blurb": "open-internet edge"},
    RungKind.ANCHOR: {"icon": "fa-anchor", "colour": "#d63384", "blurb": "long-haul transit"},
}


def kind_meta(kind):
    return KIND_META.get(kind, {"icon": "fa-question", "colour": "#6c757d", "blurb": ""})


class Vantage(models.Model):
    """Where a prober runs.

    Exists as a model on day one so the same Trail can later be probed from a
    second point (home Mac + a Fly instance) and triangulated. Do not build the
    second vantage yet — the spec is explicit about that.
    """

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Trail(models.Model):
    """A named ladder of targets reaching progressively deeper into the internet."""

    vantage = models.ForeignKey(Vantage, on_delete=models.CASCADE, related_name="trails")
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    discovered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    @property
    def rung_count(self):
        return self.rungs.count()

    @property
    def sample_count(self):
        return self.samples.count()


class Rung(models.Model):
    """One ordered target on a Trail.

    `depth` is the ladder position, NOT a unique key — the spec puts both
    1.1.1.1 and 8.8.8.8 at the public_dns level. Attribution therefore works on
    depth LEVELS, aggregating the rungs that share a depth.
    """

    trail = models.ForeignKey(Trail, on_delete=models.CASCADE, related_name="rungs")
    depth = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=RungKind.choices)
    host = models.GenericIPAddressField(protocol="IPv4")
    label = models.CharField(max_length=64)
    note = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    discovered_at = models.DateTimeField(default=timezone.now)

    # Discovery GUESSES; the operator CORRECTS. A pinned field is never
    # overwritten by a later `discover_trail` — otherwise the correction would
    # silently evaporate on the next re-discovery and the operator would have
    # no way to tell the tool it was wrong.
    kind_pinned = models.BooleanField(default=False)
    label_pinned = models.BooleanField(default=False)

    class Meta:
        ordering = ("depth", "host")
        constraints = [
            # Idempotency for `discover_trail`: re-running when the ISP path
            # changes UPDATES the ladder rather than duplicating it.
            models.UniqueConstraint(fields=["trail", "host"], name="uniq_rung_per_trail_host"),
        ]
        indexes = [models.Index(fields=["trail", "depth"])]

    def __str__(self):
        return f"{self.depth} {self.label} ({self.host})"

    @property
    def meta(self):
        return kind_meta(self.kind)

    @property
    def in_house(self):
        return is_in_house(self.kind)

    @property
    def pinned(self):
        """The operator has corrected at least one of kind/label on this rung."""
        return self.kind_pinned or self.label_pinned


class Sample(models.Model):
    """One tick — every rung of the trail fired simultaneously, in one shot."""

    trail = models.ForeignKey(Trail, on_delete=models.CASCADE, related_name="samples")
    fired_at = models.DateTimeField()
    vantage = models.ForeignKey(
        Vantage, on_delete=models.SET_NULL, null=True, blank=True, related_name="samples"
    )
    prober = models.CharField(max_length=64, blank=True, default="")
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("fired_at",)
        constraints = [
            models.UniqueConstraint(fields=["trail", "fired_at"], name="uniq_sample_per_trail_tick"),
        ]
        indexes = [models.Index(fields=["trail", "fired_at"])]

    def __str__(self):
        return f"{self.trail.slug} @ {self.fired_at.isoformat()}"


class Probe(models.Model):
    """One rung's result within one Sample.

    rtt_ms NULL == the echo never came back (timeout). That is emphatically NOT
    the same as 0.0, which would be an impossibly fast reply. Loss is recorded
    as a row with rtt_ms=NULL and loss_pct=100.0 — never as an absent row.
    """

    sample = models.ForeignKey(Sample, on_delete=models.CASCADE, related_name="probes")
    rung = models.ForeignKey(Rung, on_delete=models.CASCADE, related_name="probes")
    rtt_ms = models.FloatField(null=True, blank=True)
    sent = models.PositiveIntegerField(default=1)
    received = models.PositiveIntegerField(default=0)
    loss_pct = models.FloatField(default=0.0)
    ttl = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ("rung__depth", "rung__host")
        constraints = [
            models.UniqueConstraint(fields=["sample", "rung"], name="uniq_probe_per_sample_rung"),
        ]

    def __str__(self):
        rtt = "timeout" if self.rtt_ms is None else f"{self.rtt_ms:.3f}ms"
        return f"{self.rung.label}: {rtt}"

    @property
    def timed_out(self):
        return self.rtt_ms is None


class ProberControl(models.Model):
    """Marching orders for one Vantage's prober, plus that prober's heartbeat.

    PULL ONLY. The container cannot spawn or signal a host process — that split
    is the whole reason the measurement is honest — so the app never starts or
    stops anything. It publishes intent here; the prober fetches it every cycle
    and obeys it, and writes its heartbeat back on the way past.

    The consequence worth stating plainly: this row controls SWEEPING, not the
    daemon. `enabled=False` means the daemon keeps running and keeps polling,
    it just stops firing ICMP — which is precisely what makes the UI toggle work
    without anyone touching a terminal.
    """

    vantage = models.OneToOneField(Vantage, on_delete=models.CASCADE, related_name="control")
    trail = models.ForeignKey(
        Trail, on_delete=models.SET_NULL, null=True, blank=True, related_name="controls"
    )

    # ---- orders (written by the UI, read by the prober) ----
    # Ships DISABLED (leaning-beat-schedule-must-ship-disabled): a new vantage
    # never begins sweeping on its own.
    enabled = models.BooleanField(default=False)
    interval_seconds = models.PositiveIntegerField(default=30)
    timeout_seconds = models.FloatField(default=2.0)
    updated_at = models.DateTimeField(auto_now=True)

    # ---- heartbeat (written by the prober, never guessed by the app) ----
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_sweep_at = models.DateTimeField(null=True, blank=True)
    pid = models.PositiveIntegerField(null=True, blank=True)
    hostname = models.CharField(max_length=128, blank=True, default="")
    last_error = models.CharField(max_length=300, blank=True, default="")
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("vantage__name",)

    def __str__(self):
        return f"{self.vantage.slug}: {'sweeping' if self.enabled else 'paused'}"

    # ---- status ----------------------------------------------------------
    # STALE_INTERVALS: a prober that has missed two whole polling windows is not
    # merely late. One missed window is ordinary jitter (a slow sweep, a
    # rescheduled sleep); two is a process that is gone.
    STALE_INTERVALS = 2

    @property
    def stale_after_seconds(self):
        return max(1, self.interval_seconds) * self.STALE_INTERVALS

    @property
    def seconds_since_seen(self):
        """Age of the heartbeat in seconds, or None if no prober ever checked in."""
        if self.last_seen_at is None:
            return None
        return (timezone.now() - self.last_seen_at).total_seconds()

    @property
    def status(self):
        """ALIVE / STOPPED / STALE — read from the heartbeat, never inferred.

        The three states are deliberately about two DIFFERENT things, because
        conflating them is what would let the badge imply the site can kill a
        host process:

          STALE   — nothing has checked in for 2+ intervals. The DAEMON is not
                    running (or cannot reach us). Only a terminal fixes this.
          STOPPED — the daemon is polling, but sweeping is switched off HERE.
          ALIVE   — the daemon is polling and sweeping.
        """
        age = self.seconds_since_seen
        if age is None or age > self.stale_after_seconds:
            return {
                "code": "STALE",
                "label": "STALE",
                "css": "text-bg-danger",
                "dot": "#dc3545",
                "detail": (
                    "no prober has ever checked in"
                    if age is None
                    else f"no check-in for {int(age)}s (> {self.stale_after_seconds}s)"
                ),
                "meaning": "the host daemon is not running — this page cannot start it",
                "sweeping": False,
            }
        if self.enabled:
            return {
                "code": "ALIVE",
                "label": "ALIVE",
                "css": "text-bg-success",
                "dot": "#198754",
                "detail": f"checked in {int(age)}s ago",
                "meaning": "daemon polling and sweeping",
                "sweeping": True,
            }
        return {
            "code": "STOPPED",
            "label": "STOPPED",
            "css": "text-bg-secondary",
            "dot": "#6c757d",
            "detail": f"checked in {int(age)}s ago",
            "meaning": "daemon still polling — sweeping paused from this page",
            "sweeping": False,
        }
