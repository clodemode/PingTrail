"""Deterministic proof that the attribution maths is right.

These run BEFORE any browser check. Each test pins one clause of
spec-ping-trail-hop-ladder-latency-attribution that is easy to regress and
impossible to eyeball on a chart.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from trail.attribution import attribute, segment_series
from trail.models import Probe, Rung, Sample, Trail, Vantage

# The ladder used by every fixture below.
LADDER = [
    (0, "loopback", "127.0.0.1", "loopback"),
    (1, "gateway", "192.168.11.1", "gateway"),
    (2, "isp_hop", "192.168.1.1", "isp_hop1"),
    (3, "isp_hop", "192.88.99.2", "isp_hop2"),
    (4, "isp_dns", "192.88.99.53", "isp_dns"),
    (5, "public_dns", "1.1.1.1", "public_dns"),
    (6, "anchor", "193.0.14.129", "anchor"),
]


class LadderFixtureMixin:
    def build_trail(self, slug="fixture", ladder=LADDER):
        vantage = Vantage.objects.create(slug=f"{slug}-vantage", name="Fixture Vantage")
        trail = Trail.objects.create(slug=slug, name="Fixture Trail", vantage=vantage)
        self.rungs = {}
        for depth, kind, host, label in ladder:
            self.rungs[label] = Rung.objects.create(
                trail=trail, depth=depth, kind=kind, host=host, label=label
            )
        return trail

    def add_tick(self, trail, rtt_by_label, fired_at=None):
        """rtt_by_label: {label: rtt_ms or None}. None == timeout."""
        sample = Sample.objects.create(
            trail=trail, fired_at=fired_at or timezone.now(), prober="fixture"
        )
        for label, rtt in rtt_by_label.items():
            Probe.objects.create(
                sample=sample,
                rung=self.rungs[label],
                rtt_ms=rtt,
                sent=1,
                received=0 if rtt is None else 1,
                loss_pct=100.0 if rtt is None else 0.0,
                error="timeout" if rtt is None else "",
            )
        return sample


class InjectedDelayAttributionTest(LadderFixtureMixin, TestCase):
    """VERIFICATION 1 — a known delay at a known rung must be named."""

    # 40ms is injected between isp_hop1 (depth 2) and isp_hop2 (depth 3).
    # Every other step is ~1ms, so 'isp_hop2' is unambiguously the answer.
    RTTS = {
        "loopback": 0.5,
        "gateway": 1.5,
        "isp_hop1": 2.5,
        "isp_hop2": 42.5,   # <-- +40.0ms injected HERE
        "isp_dns": 43.5,
        "public_dns": 44.5,
        "anchor": 45.5,
    }

    def setUp(self):
        self.trail = self.build_trail()
        self.sample = self.add_tick(self.trail, self.RTTS)

    def test_dominant_segment_is_the_injected_rung(self):
        tick = attribute(self.sample)
        self.assertFalse(tick.void)
        dominant = tick.dominant_segment
        self.assertIsNotNone(dominant, "expected a dominant segment")
        self.assertEqual(
            dominant.label,
            "isp_hop2",
            f"attribution blamed '{dominant.label}' but the delay was injected at 'isp_hop2'",
        )
        self.assertAlmostEqual(dominant.delta_ms, 40.0, places=3)
        self.assertEqual(dominant.from_depth, 2)
        self.assertEqual(dominant.to_depth, 3)

    def test_segments_telescope_to_the_deepest_rtt(self):
        """Sum of segments must equal RTT to the deepest answering rung."""
        tick = attribute(self.sample)
        self.assertAlmostEqual(tick.total_ms, self.RTTS["anchor"], places=3)
        self.assertAlmostEqual(
            sum(s.delta_ms for s in tick.segments), self.RTTS["anchor"], places=3
        )

    def test_every_other_segment_is_small(self):
        tick = attribute(self.sample)
        others = [s for s in tick.segments if s.label != "isp_hop2"]
        self.assertTrue(others)
        for segment in others:
            self.assertLess(
                abs(segment.delta_ms), 2.0, f"segment {segment.label} unexpectedly large"
            )

    def test_trail_report_names_the_injected_rung(self):
        """The CLI verifier itself must print the right attribution."""
        out = StringIO()
        call_command("trail_report", "--trail", "fixture", stdout=out, no_color=True)
        report = out.getvalue()
        self.assertIn("ATTRIBUTION", report)
        self.assertRegex(
            report,
            r"ATTRIBUTION → 'isp_hop2' \(depth 3\) injects 40\.000ms",
            f"trail_report did not name isp_hop2. Report was:\n{report}",
        )
        self.assertIn("DOMINANT SEGMENT → 'isp_hop2'", report)


class InversionNotClampedTest(LadderFixtureMixin, TestCase):
    """VERIFICATION 2 — a negative delta survives to the report, unclamped."""

    # isp_dns (depth 4) answers 5ms FASTER than isp_hop2 (depth 3): a router
    # punting ICMP addressed to its own control plane onto a slow path.
    RTTS = {
        "loopback": 0.5,
        "gateway": 1.5,
        "isp_hop1": 2.5,
        "isp_hop2": 30.0,
        "isp_dns": 25.0,    # <-- INVERSION: -5.0ms
        "public_dns": 26.0,
        "anchor": 27.0,
    }

    def setUp(self):
        self.trail = self.build_trail()
        self.sample = self.add_tick(self.trail, self.RTTS)

    def test_negative_delta_is_preserved_with_its_sign(self):
        tick = attribute(self.sample)
        inverted = [s for s in tick.segments if s.label == "isp_dns"][0]
        self.assertAlmostEqual(inverted.delta_ms, -5.0, places=3)
        self.assertTrue(inverted.inverted)
        self.assertNotEqual(inverted.delta_ms, 0.0, "negative delta was clamped to zero")
        self.assertLess(inverted.delta_ms, 0.0)

    def test_inversion_is_reported_as_a_distinct_signal(self):
        tick = attribute(self.sample)
        self.assertEqual(len(tick.inversions), 1)
        self.assertEqual(tick.inversions[0].label, "isp_dns")

    def test_inversion_never_becomes_the_dominant_segment(self):
        """An inversion is a routing artefact — it cannot be 'the segment to blame'."""
        tick = attribute(self.sample)
        self.assertEqual(tick.dominant_segment.label, "isp_hop2")

    def test_total_still_telescopes_through_the_inversion(self):
        tick = attribute(self.sample)
        self.assertAlmostEqual(tick.total_ms, self.RTTS["anchor"], places=3)

    def test_chart_payload_carries_the_negative_value(self):
        """The series data handed to ECharts must keep the negative, not a 0."""
        tick = attribute(self.sample)
        _cats, series, totals, inversions, _voids = segment_series([tick])
        isp_dns_series = [s for s in series if s["label"] == "isp_dns"][0]
        self.assertAlmostEqual(isp_dns_series["data"][0], -5.0, places=3)
        self.assertEqual(len(inversions), 1)
        self.assertEqual(inversions[0][0], 0)          # tick index
        self.assertAlmostEqual(inversions[0][1], -5.0, places=3)
        self.assertEqual(inversions[0][2], "isp_dns")
        self.assertAlmostEqual(totals[0], self.RTTS["anchor"], places=3)

    def test_report_flags_the_inversion_and_shows_the_sign(self):
        out = StringIO()
        call_command("trail_report", "--trail", "fixture", stdout=out, no_color=True)
        report = out.getvalue()
        self.assertIn("INVERSION", report)
        self.assertIn("isp_dns(-5.000ms)", report)
        self.assertNotIn("isp_dns(0.000ms)", report)


class VoidTickTest(LadderFixtureMixin, TestCase):
    """VERIFICATION 3 — a fully-timed-out tick is void, never bridged."""

    def setUp(self):
        self.trail = self.build_trail()
        base = timezone.now()
        # Neighbour ticks on either side of the void — if anything bridges, it
        # will bridge between these two.
        self.before = self.add_tick(
            self.trail,
            {"loopback": 0.5, "gateway": 1.5, "isp_hop1": 2.5, "isp_hop2": 30.0,
             "isp_dns": 31.0, "public_dns": 32.0, "anchor": 33.0},
            fired_at=base,
        )
        self.void = self.add_tick(
            self.trail,
            {label: None for label, *_ in [(l,) for _, _, _, l in LADDER]},
            fired_at=base + timezone.timedelta(seconds=30),
        )
        self.after = self.add_tick(
            self.trail,
            {"loopback": 0.5, "gateway": 1.5, "isp_hop1": 2.5, "isp_hop2": 30.0,
             "isp_dns": 31.0, "public_dns": 32.0, "anchor": 33.0},
            fired_at=base + timezone.timedelta(seconds=60),
        )

    def test_void_tick_has_no_segments_and_no_total(self):
        tick = attribute(self.void)
        self.assertTrue(tick.void)
        self.assertEqual(tick.segments, ())
        self.assertIsNone(tick.total_ms)
        self.assertIsNone(tick.dominant_segment)

    def test_loss_is_recorded_as_rows_not_as_absence(self):
        """Every rung still has a Probe row — loss is data, not a missing row."""
        self.assertEqual(self.void.probes.count(), len(LADDER))
        for probe in self.void.probes.all():
            self.assertIsNone(probe.rtt_ms)
            self.assertEqual(probe.loss_pct, 100.0)

    def test_null_rtt_is_distinct_from_zero(self):
        probe = self.void.probes.first()
        self.assertIsNone(probe.rtt_ms)
        self.assertNotEqual(probe.rtt_ms, 0.0)
        self.assertTrue(probe.timed_out)

    def test_chart_payload_emits_null_never_an_interpolated_value(self):
        ticks = [attribute(s) for s in (self.before, self.void, self.after)]
        categories, series, totals, _inversions, voids = segment_series(ticks)

        self.assertEqual(len(categories), 3)
        self.assertEqual(voids, [1], "the middle tick must be flagged void")
        self.assertIsNone(totals[1], "a void tick must have no total")

        for entry in series:
            value = entry["data"][1]
            self.assertIsNone(
                value,
                f"series '{entry['label']}' put {value!r} in the void slot — "
                f"a void tick must be null (a gap), never 0 and never a bridged value",
            )
            # The neighbours must be untouched and NOT averaged into the gap.
            self.assertIsNotNone(entry["data"][0])
            self.assertIsNotNone(entry["data"][2])

    def test_report_marks_the_tick_void(self):
        out = StringIO()
        call_command("trail_report", "--trail", "fixture", stdout=out, no_color=True)
        report = out.getvalue()
        self.assertIn("VOID TICK", report)
        self.assertIn("No bar is drawn", report)
        self.assertIn("1 VOID tick(s) excluded from the rollup.", report)


class PartialLossTest(LadderFixtureMixin, TestCase):
    """A lost level whose DEEPER neighbour still answers — the strongest signal."""

    def setUp(self):
        self.trail = self.build_trail()
        self.sample = self.add_tick(
            self.trail,
            {
                "loopback": 0.5,
                "gateway": 1.5,
                "isp_hop1": None,   # this box deprioritizes ICMP...
                "isp_hop2": 30.0,   # ...but traffic THROUGH it is fine
                "isp_dns": 31.0,
                "public_dns": 32.0,
                "anchor": 33.0,
            },
        )

    def test_tick_is_not_void_when_only_one_level_is_lost(self):
        tick = attribute(self.sample)
        self.assertFalse(tick.void)
        self.assertEqual(len(tick.lost_levels), 1)
        self.assertEqual(tick.lost_levels[0].label, "isp_hop1")

    def test_segment_spans_the_lost_level_instead_of_hiding_it(self):
        tick = attribute(self.sample)
        spanning = [s for s in tick.segments if s.to_depth == 3][0]
        self.assertEqual(spanning.from_depth, 1, "should bridge from the last answering level")
        self.assertEqual(spanning.spans_lost_levels, (2,))
        self.assertAlmostEqual(spanning.delta_ms, 28.5, places=3)

    def test_total_still_equals_deepest_rtt(self):
        tick = attribute(self.sample)
        self.assertAlmostEqual(tick.total_ms, 33.0, places=3)


class LevelAggregationTest(LadderFixtureMixin, TestCase):
    """Two rungs sharing a depth (1.1.1.1 + 8.8.8.8) collapse to one level."""

    def setUp(self):
        ladder = [
            (0, "loopback", "127.0.0.1", "loopback"),
            (1, "gateway", "192.168.11.1", "gateway"),
            (5, "public_dns", "1.1.1.1", "public_dns"),
            (5, "public_dns", "8.8.8.8", "public_dns"),
        ]
        self.trail = self.build_trail(slug="shared-depth", ladder=ladder)
        # build_trail keys by label, so re-fetch the two depth-5 rungs by host.
        self.sample = Sample.objects.create(trail=self.trail, fired_at=timezone.now())
        rtts = {"127.0.0.1": 0.5, "192.168.11.1": 1.5, "1.1.1.1": 20.0, "8.8.8.8": 25.0}
        for rung in self.trail.rungs.all():
            rtt = rtts[rung.host]
            Probe.objects.create(
                sample=self.sample, rung=rung, rtt_ms=rtt, sent=1, received=1, loss_pct=0.0
            )

    def test_level_rtt_is_the_minimum_across_the_shared_depth(self):
        tick = attribute(self.sample)
        level = [lv for lv in tick.levels if lv.depth == 5][0]
        self.assertEqual(level.total, 2)
        self.assertEqual(level.answered, 2)
        self.assertAlmostEqual(level.rtt_ms, 20.0, places=3)
        self.assertEqual(set(level.hosts), {"1.1.1.1", "8.8.8.8"})

    def test_one_host_down_does_not_lose_the_level(self):
        Probe.objects.filter(sample=self.sample, rung__host="1.1.1.1").update(
            rtt_ms=None, received=0, loss_pct=100.0
        )
        tick = attribute(self.sample)
        level = [lv for lv in tick.levels if lv.depth == 5][0]
        self.assertFalse(level.lost)
        self.assertAlmostEqual(level.rtt_ms, 25.0, places=3)
        self.assertAlmostEqual(level.loss_pct, 50.0, places=1)
