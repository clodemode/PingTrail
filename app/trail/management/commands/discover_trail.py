"""discover_trail — build a Trail's ladder from the host's actual network.

MUST RUN ON THE HOST, never in the container: inside Docker the default gateway
is the bridge (172.x.x.1) and traceroute describes the bridge network, so a
containerized discovery produces a confident, wrong ladder.

    .venv/bin/python app/manage.py discover_trail --trail home

Idempotent. Re-run it whenever the ISP path changes: rungs are matched by host,
depths and labels are updated in place, and rungs that fall off the path are
DEACTIVATED rather than deleted so their measurement history survives.
"""
import os

from django.core.management.base import BaseCommand, CommandError

from trail import discovery
from trail.models import Trail
from trail.services import apply_ladder, get_or_create_trail, get_or_create_vantage


class Command(BaseCommand):
    help = "Discover the hop ladder for a Trail (runs on the host, not in Docker)."

    def add_arguments(self, parser):
        parser.add_argument("--trail", default="home", help="Trail slug (default: home)")
        parser.add_argument("--name", default="", help="Human name when creating the trail")
        parser.add_argument(
            "--vantage",
            default=os.environ.get("PING_TRAIL_VANTAGE", "home-mac"),
            help="Vantage slug this ladder is discovered from (default: home-mac)",
        )
        parser.add_argument(
            "--target",
            default=discovery.DEFAULT_TRACE_TARGET,
            help=f"Traceroute target used to find the ISP hops (default: {discovery.DEFAULT_TRACE_TARGET})",
        )
        parser.add_argument(
            "--anchor",
            default=discovery.DEFAULT_ANCHOR,
            help=f"Distant anchor host (default: {discovery.DEFAULT_ANCHOR})",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show the discovered ladder; write nothing."
        )
        parser.add_argument(
            "--keep-missing",
            action="store_true",
            help="Do not deactivate rungs that no longer appear on the path.",
        )

    def handle(self, *args, **options):
        slug = options["trail"]
        self.stdout.write(self.style.MIGRATE_HEADING(f"Discovering ladder for trail '{slug}'"))

        rungs, notes = discovery.build_ladder(
            trace_target=options["target"], anchor=options["anchor"]
        )

        for note in notes:
            style = {
                "ok": self.style.SUCCESS,
                "warn": self.style.WARNING,
                "skip": self.style.NOTICE,
                "degraded": self.style.ERROR,
            }.get(note.level, self.style.HTTP_INFO)
            self.stdout.write(style(str(note)))

        if not rungs:
            raise CommandError(
                "No rungs discovered — refusing to write an empty ladder. "
                "Check that the host is online and that /sbin/route and "
                "/usr/sbin/traceroute are available."
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\n--dry-run: nothing written."))
            return

        vantage = get_or_create_vantage(options["vantage"])
        trail = get_or_create_trail(
            slug,
            vantage,
            name=options["name"] or slug.replace("-", " ").title(),
            description=f"Auto-discovered toward {options['target']}",
        )

        # A DEGRADED pass could not see part of the path, so its silence about a
        # rung is not evidence that the rung is gone. Retiring on that evidence
        # is how a lossy traceroute quietly deletes the only ISP rung on the
        # ladder and takes the ISP attribution with it — observed live on
        # 2026-08-01 during v2 verification, with the ISP hop at 100% loss.
        degraded = discovery.is_degraded(notes)
        deactivate = not options["keep_missing"] and not degraded
        if degraded and not options["keep_missing"]:
            self.stdout.write(
                self.style.ERROR(
                    "\nDEGRADED PASS — this discovery could not see the whole path, so no rung "
                    "will be retired. A transient traceroute failure must never be allowed to "
                    "silently shorten the ladder. Re-run when the path is answering, or pass "
                    "--keep-missing explicitly to make this the intent."
                )
            )

        created, updated, deactivated, reactivated = apply_ladder(
            trail, rungs, deactivate_missing=deactivate
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Ladder for '{trail.slug}' (vantage: {vantage.slug})"))
        for rung in Trail.objects.get(pk=trail.pk).rungs.filter(is_active=True):
            self.stdout.write(f"  depth {rung.depth}  {rung.kind:<11} {rung.host:<16} {rung.label}")

        summary = [
            f"{len(created)} created",
            f"{len(updated)} updated",
            f"{len(reactivated)} reactivated",
            f"{len(deactivated)} deactivated",
        ]
        self.stdout.write(self.style.SUCCESS("\n" + ", ".join(summary)))
        if deactivated:
            self.stdout.write(
                self.style.WARNING(
                    f"  deactivated (history preserved): {', '.join(deactivated)}"
                )
            )
