"""trail_report — the deterministic verifier.

Prints the per-tick attribution table: raw RTT per rung, computed segment
deltas, inversions flagged, loss column. This is the proof the maths is right,
and it runs before any browser check.

    manage.py trail_report --trail home --limit 10
    manage.py trail_report --trail home --tick 42      # one tick, full detail
"""
from django.core.management.base import BaseCommand, CommandError

from trail.attribution import attribute
from trail.models import Sample, Trail, is_in_house


def fmt_rtt(value):
    return "  timeout" if value is None else f"{value:8.3f}"


class Command(BaseCommand):
    help = "Per-tick latency attribution table for a Trail (deterministic verifier)."

    def add_arguments(self, parser):
        parser.add_argument("--trail", default="home", help="Trail slug (default: home)")
        parser.add_argument("--limit", type=int, default=10, help="Most recent N ticks (default: 10)")
        parser.add_argument("--tick", type=int, default=0, help="Report a single Sample id in full detail")
        parser.add_argument("--all", action="store_true", help="Every tick, oldest first")

    def handle(self, *args, **options):
        slug = options["trail"]
        try:
            trail = Trail.objects.get(slug=slug)
        except Trail.DoesNotExist:
            raise CommandError(f"No trail '{slug}'.")

        base = Sample.objects.filter(trail=trail).prefetch_related("probes__rung")
        if options["tick"]:
            samples = list(base.filter(pk=options["tick"]))
            if not samples:
                raise CommandError(f"No sample {options['tick']} on trail '{slug}'.")
        elif options["all"]:
            samples = list(base.order_by("fired_at"))
        else:
            samples = list(base.order_by("-fired_at")[: options["limit"]])[::-1]

        if not samples:
            self.stdout.write(
                self.style.WARNING(
                    f"Trail '{slug}' has no samples yet. Run: manage.py ping_sweep --trail {slug} --once"
                )
            )
            return

        ticks = [attribute(s) for s in samples]

        self.stdout.write(self.style.MIGRATE_HEADING(f"\nTRAIL: {trail.name} ({trail.slug})  vantage={trail.vantage.slug}"))
        self.stdout.write(f"{len(ticks)} tick(s)\n")

        for tick in ticks:
            self._render_tick(tick)

        self._render_rollup(ticks)

    def _render_tick(self, tick):
        stamp = tick.fired_at.strftime("%Y-%m-%d %H:%M:%S")
        header = f"── tick {tick.sample_id}  {stamp} "
        self.stdout.write(self.style.MIGRATE_LABEL(header + "─" * max(0, 74 - len(header))))

        if tick.void:
            self.stdout.write(
                self.style.ERROR(
                    "   VOID TICK — every rung timed out. No bar is drawn and no value is "
                    "interpolated across this gap."
                )
            )
            for level in tick.levels:
                self.stdout.write(
                    f"     depth {level.depth}  {level.label:<11} "
                    f"{'/'.join(level.hosts):<32}   timeout   loss {level.loss_pct:5.1f}%"
                )
            self.stdout.write("")
            return

        self.stdout.write(
            f"   {'depth':<6}{'rung':<12}{'host(s)':<34}{'raw RTT':>9}  {'segment':>10}   {'loss':>6}  note"
        )

        for level in tick.levels:
            segment = next((s for s in tick.segments if s.to_depth == level.depth), None)
            hosts = "/".join(level.hosts)
            if len(hosts) > 32:
                hosts = hosts[:31] + "…"

            if segment is None:
                seg_txt = "        —"
                note = "level lost — segment spans it" if level.lost else ""
            elif segment.from_depth is None:
                seg_txt = f"{segment.delta_ms:9.3f}"
                note = "baseline (noise floor)"
            else:
                seg_txt = f"{segment.delta_ms:+9.3f}"
                note = f"from depth {segment.from_depth}"
                if segment.spans_lost_levels:
                    note += f" — spans lost depth {','.join(str(d) for d in segment.spans_lost_levels)}"

            line = (
                f"   {level.depth:<6}{level.label:<12}{hosts:<34}"
                f"{fmt_rtt(level.rtt_ms):>9}  {seg_txt:>10}   {level.loss_pct:5.1f}%  {note}"
            )

            if segment is not None and segment.inverted:
                self.stdout.write(self.style.WARNING(line))
                self.stdout.write(
                    self.style.WARNING(
                        f"        ↑ INVERSION: depth {segment.to_depth} answered "
                        f"{abs(segment.delta_ms):.3f}ms FASTER than depth {segment.from_depth}. "
                        f"Real signal (slow-path ICMP to a router's control plane), not noise — "
                        f"kept negative, never clamped to 0."
                    )
                )
            elif level.lost:
                self.stdout.write(self.style.ERROR(line))
            else:
                self.stdout.write(line)

        total = f"{tick.total_ms:.3f}ms" if tick.total_ms is not None else "n/a"
        self.stdout.write(f"   {'':<6}{'TOTAL':<12}{'(sum of segments = deepest RTT)':<34}{total:>9}")

        dominant = tick.dominant_segment
        if dominant is not None:
            share = (100.0 * dominant.delta_ms / tick.total_ms) if tick.total_ms else 0.0
            self.stdout.write(
                self.style.SUCCESS(
                    f"   ATTRIBUTION → '{dominant.label}' (depth {dominant.to_depth}) "
                    f"injects {dominant.delta_ms:.3f}ms of {total} ({share:.1f}%)"
                )
            )
        else:
            self.stdout.write(self.style.WARNING("   ATTRIBUTION → no positive segment to attribute"))

        if tick.inversions:
            labels = ", ".join(f"{s.label}({s.delta_ms:+.3f}ms)" for s in tick.inversions)
            self.stdout.write(self.style.WARNING(f"   INVERSIONS  → {labels}"))
        if tick.lost_levels:
            labels = ", ".join(f"depth {lv.depth} {lv.label}" for lv in tick.lost_levels)
            self.stdout.write(self.style.ERROR(f"   LOSS        → {labels} (100% loss)"))
        self.stdout.write("")

    def _render_rollup(self, ticks):
        live = [t for t in ticks if not t.void]
        self.stdout.write(self.style.MIGRATE_HEADING("── rollup " + "─" * 65))
        if not live:
            self.stdout.write(self.style.ERROR("   Every tick in range was VOID."))
            return

        totals = {}
        counts = {}
        kinds = {}
        for tick in live:
            for segment in tick.segments:
                totals[segment.label] = totals.get(segment.label, 0.0) + segment.delta_ms
                counts[segment.label] = counts.get(segment.label, 0) + 1
                kinds[segment.label] = segment.kind

        means = sorted(
            ((label, totals[label] / counts[label]) for label in totals),
            key=lambda pair: pair[1],
            reverse=True,
        )
        grand = sum(mean for _, mean in means)
        self.stdout.write(f"   mean segment contribution across {len(live)} live tick(s):")
        for label, mean in means:
            share = (100.0 * mean / grand) if grand else 0.0
            flag = "  ← INVERSION (mean negative)" if mean < 0 else ""
            bar = "█" * int(max(0.0, share) / 2)
            side = "house" if is_in_house(kinds[label]) else " ISP "
            self.stdout.write(f"     {label:<12}[{side}]{mean:+9.3f}ms  {share:5.1f}%  {bar}{flag}")
        self.stdout.write(f"     {'TOTAL':<12}       {grand:9.3f}ms")

        void_count = len(ticks) - len(live)
        if void_count:
            self.stdout.write(self.style.ERROR(f"   {void_count} VOID tick(s) excluded from the rollup."))

        # WHOSE EQUIPMENT. The v1 defect was a bad noun, not a bad number: the
        # headline said "ISP" about a box in the house. Naming the side from the
        # rung KIND means the verdict cannot disagree with the classification.
        in_house = sum(mean for label, mean in means if is_in_house(kinds[label]))
        isp = sum(mean for label, mean in means if not is_in_house(kinds[label]))
        self.stdout.write("")
        self.stdout.write(
            f"   IN-HOUSE (loopback/gateway/home_router) {in_house:+9.3f}ms  "
            f"{(100.0 * in_house / grand) if grand else 0.0:5.1f}%"
        )
        self.stdout.write(
            f"   ISP SIDE (isp_hop/isp_dns/public/anchor) {isp:+9.3f}ms  "
            f"{(100.0 * isp / grand) if grand else 0.0:5.1f}%"
        )

        worst = max(means, key=lambda pair: pair[1])
        verdict = "IN YOUR HOUSE" if is_in_house(kinds[worst[0]]) else "OUTSIDE — ISP SIDE"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n   DOMINANT SEGMENT → '{worst[0]}' at {worst[1]:+.3f}ms mean — {verdict}"
            )
        )

        # Both sides get named. A dominant ISP hop does not excuse a sick LAN,
        # and the v1 sample window had exactly both at once.
        worst_house = next(((l, m) for l, m in means if is_in_house(kinds[l]) and m > 0), None)
        worst_isp = next(((l, m) for l, m in means if not is_in_house(kinds[l]) and m > 0), None)
        if worst_house and worst_isp:
            self.stdout.write(
                f"   worst in-house segment '{worst_house[0]}' {worst_house[1]:+.3f}ms · "
                f"worst ISP segment '{worst_isp[0]}' {worst_isp[1]:+.3f}ms — separate problems\n"
            )
