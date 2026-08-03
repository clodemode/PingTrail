"""Latency attribution — the subtraction that makes PING_TRAIL worth building.

Each rung's path CONTAINS the shallower rung's path, so the incremental latency
of a segment is the delta between adjacent rungs:

    segment[i] = rtt[level i] - rtt[level i-1]

Stack the segments and the bar's total height equals the RTT to the deepest
answering rung (the sum telescopes). The segment whose band swells is the one
injecting the latency. That band is the answer.

Three rules from spec-ping-trail-hop-ladder-latency-attribution that this module
exists to enforce, and that the test-suite pins:

1. NEGATIVE DELTAS ARE NEVER CLAMPED. A router punts ICMP addressed TO its own
   control plane onto a slow path while forwarding ICMP THROUGH it on the fast
   path, so a deeper rung legitimately answers FASTER than a shallower one.
   Clamping to zero hides a real signal and makes the chart lie. A negative
   delta is surfaced as an `inverted` segment, sign intact.

2. A FULLY-TIMED-OUT TICK IS VOID, NEVER BRIDGED. `void=True`, no segments, no
   total. The renderer draws a gap. It must never be interpolated across.

3. LOSS IS DATA, NOT ABSENCE. A level where every probe timed out is reported
   with rtt_ms=None and loss_pct=100, and the segment spanning it names the
   depths it jumped (`spans_lost_levels`) instead of silently swallowing them.

Levels, not rungs: the spec puts BOTH 1.1.1.1 and 8.8.8.8 at the public_dns
depth, so attribution aggregates rungs that share a `depth`. A level's RTT is
the MINIMUM across its answering probes — the fastest route to that tier is the
honest representative of the tier's reachable latency; a max would blame a whole
segment for one sulking peer.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LevelResult:
    """One depth level of the ladder within one tick."""

    depth: int
    kind: str
    label: str
    hosts: tuple
    rtt_ms: float | None  # None == every probe at this level timed out
    answered: int
    total: int
    loss_pct: float
    per_host: tuple = field(default_factory=tuple)  # ((host, rtt_ms|None, error), ...)

    @property
    def lost(self):
        return self.rtt_ms is None


@dataclass(frozen=True)
class Segment:
    """Incremental latency attributed to the span between two answering levels."""

    to_depth: int
    from_depth: int | None  # None == the base segment (floor -> shallowest level)
    kind: str
    label: str
    delta_ms: float
    spans_lost_levels: tuple = ()

    @property
    def inverted(self):
        """A deeper rung answered FASTER than the shallower one. Real, not noise."""
        return self.delta_ms < 0


@dataclass(frozen=True)
class TickAttribution:
    """A single sample tick, fully attributed."""

    sample_id: int
    fired_at: object
    levels: tuple
    segments: tuple
    total_ms: float | None
    void: bool

    @property
    def inversions(self):
        return tuple(s for s in self.segments if s.inverted)

    @property
    def lost_levels(self):
        return tuple(lv for lv in self.levels if lv.lost)

    @property
    def dominant_segment(self):
        """The segment injecting the most latency — the tool's actual answer.

        Chosen among POSITIVE segments: an inversion is a routing artefact, not
        a latency contribution, so it can never be 'the segment to blame'.
        """
        positives = [s for s in self.segments if s.delta_ms > 0]
        if not positives:
            return None
        return max(positives, key=lambda s: s.delta_ms)

    @property
    def has_partial_loss(self):
        return any(lv.loss_pct > 0 for lv in self.levels)


def _level_label(kind, rungs):
    """Prefer the shared rung label; fall back to the kind's display name."""
    labels = {r.label for r in rungs}
    if len(labels) == 1:
        return labels.pop()
    return kind


def build_levels(sample, probes=None):
    """Collapse a Sample's probes into ordered depth levels."""
    probes = list(sample.probes.all() if probes is None else probes)

    by_depth = {}
    for probe in probes:
        by_depth.setdefault(probe.rung.depth, []).append(probe)

    levels = []
    for depth in sorted(by_depth):
        group = by_depth[depth]
        rungs = [p.rung for p in group]
        answered = [p for p in group if p.rtt_ms is not None]
        # MIN across answering probes — see module docstring.
        rtt = min(p.rtt_ms for p in answered) if answered else None
        total = len(group)
        sent = sum(p.sent for p in group) or total
        received = sum(p.received for p in group)
        loss_pct = 100.0 * (sent - received) / sent if sent else 100.0
        levels.append(
            LevelResult(
                depth=depth,
                kind=rungs[0].kind,
                label=_level_label(rungs[0].kind, rungs),
                hosts=tuple(r.host for r in rungs),
                rtt_ms=rtt,
                answered=len(answered),
                total=total,
                loss_pct=round(loss_pct, 2),
                per_host=tuple(
                    (p.rung.host, p.rtt_ms, p.error) for p in sorted(group, key=lambda x: x.rung.host)
                ),
            )
        )
    return tuple(levels)


def build_segments(levels):
    """Delta between adjacent ANSWERING levels. Signs preserved — never clamped."""
    answering = [lv for lv in levels if not lv.lost]
    if not answering:
        return ()

    segments = []
    previous = None
    for level in answering:
        if previous is None:
            # Base segment: the shallowest answering level's own RTT. On a full
            # ladder that is the loopback rung — the host scheduling noise floor.
            delta = level.rtt_ms
            from_depth = None
            skipped = tuple(lv.depth for lv in levels if lv.lost and lv.depth < level.depth)
        else:
            delta = level.rtt_ms - previous.rtt_ms
            from_depth = previous.depth
            skipped = tuple(
                lv.depth for lv in levels if lv.lost and previous.depth < lv.depth < level.depth
            )
        segments.append(
            Segment(
                to_depth=level.depth,
                from_depth=from_depth,
                kind=level.kind,
                label=level.label,
                delta_ms=round(delta, 4),
                spans_lost_levels=skipped,
            )
        )
        previous = level
    return tuple(segments)


def attribute(sample, probes=None):
    """Full attribution for one tick."""
    levels = build_levels(sample, probes=probes)
    segments = build_segments(levels)
    # Void: nothing answered anywhere on the ladder. No total, no segments — the
    # renderer must draw a gap, never a bridge to the neighbouring tick.
    void = not segments
    total = None if void else round(sum(s.delta_ms for s in segments), 4)
    return TickAttribution(
        sample_id=sample.pk,
        fired_at=sample.fired_at,
        levels=levels,
        segments=segments,
        total_ms=total,
        void=void,
    )


def attribute_many(samples):
    """Attribute a queryset/iterable of samples, one pass, ordered by fired_at."""
    return [attribute(s) for s in samples]


def segment_series(ticks):
    """Reshape ticks into per-segment series for a stacked bar chart.

    Returns (categories, series, totals, inversion_points, void_indices) where:
      * categories       — one label per tick (CATEGORY axis; never a time axis,
                           so evenly-spaced bars can't imply continuity across a
                           collection gap);
      * series           — [{key,label,kind,colour,data:[...]}] with `None` where
                           a tick has no value for that segment (void or absent);
      * totals           — pre-computed bucket totals for the stacked label
                           (`echarts-stacked-label-total`: p.value in a stacked
                           formatter gives ONE segment, not the bar total);
      * inversion_points — [[tickIndex, delta_ms, label], ...] for the marker series;
      * void_indices     — ticks where the whole ladder timed out.
    """
    from trail.models import kind_meta

    categories = []
    totals = []
    void_indices = []
    inversion_points = []

    # Stable series order = ladder order. Keyed by (to_depth, label) so a
    # segment keeps its identity across ticks.
    seen = {}
    order = []
    for tick in ticks:
        for seg in tick.segments:
            key = (seg.to_depth, seg.label)
            if key not in seen:
                seen[key] = {"kind": seg.kind, "label": seg.label, "to_depth": seg.to_depth}
                order.append(key)
    order.sort()

    rows = {key: [] for key in order}

    for index, tick in enumerate(ticks):
        categories.append(tick.fired_at.strftime("%H:%M:%S"))
        totals.append(tick.total_ms)
        if tick.void:
            void_indices.append(index)
        present = {(s.to_depth, s.label): s for s in tick.segments}
        for key in order:
            seg = present.get(key)
            # None (not 0.0) for an absent segment — ECharts leaves a true gap
            # rather than drawing a zero-height band that implies a measurement.
            rows[key].append(None if seg is None else seg.delta_ms)
        for seg in tick.inversions:
            inversion_points.append([index, seg.delta_ms, seg.label])

    series = []
    for key in order:
        meta = seen[key]
        series.append(
            {
                "key": f"d{meta['to_depth']}-{meta['label']}",
                "label": meta["label"],
                "kind": meta["kind"],
                "depth": meta["to_depth"],
                "colour": kind_meta(meta["kind"])["colour"],
                "data": rows[key],
            }
        )

    return categories, series, totals, inversion_points, void_indices
