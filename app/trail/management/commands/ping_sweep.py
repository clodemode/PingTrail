"""ping_sweep — the host-side prober.

MUST RUN ON THE HOST, never in the container (spec VANTAGE): inside Docker the
default gateway is the bridge, and on macOS the Docker "host" is a Linux VM
behind its own NAT, so a containerized prober measures a fiction.

    .venv/bin/python app/manage.py ping_sweep --vantage home-mac --once --force
    .venv/bin/python app/manage.py ping_sweep --vantage home-mac --loop

HOLDS NO DATABASE. This is the point of v2 and an acceptance criterion of
spec-ping-trail-control-plane-and-rfc1918-classification. v1 read the ladder
through the ORM, so starting the prober on the host required DJANGO_DB_PATH
pointed at the container's SQLite file. That only worked because host and
container happen to share one file on this machine, and it silently broke the
decoupling the v1 spec asserts — the prober is supposed to be able to run
anywhere, including a Fly instance with no access to this database at all.

So: the ladder and the marching orders arrive over HTTP from
`GET /control/<vantage>/`, results leave over HTTP to `POST /ingest/`, and
nothing in this file imports a model or opens a connection. If it ever needs
DJANGO_DB_PATH to start again, that is the regression.

CONTROL IS PULL. The container cannot spawn or signal a host process, so it
never tries. This loop asks for its orders every cycle and obeys them:

  * `enabled=false`  -> keep polling, fire nothing. The daemon stays up so the
                        website toggle works within one interval without anyone
                        touching a terminal.
  * `interval`       -> takes effect on the NEXT cycle, never needs a restart.
  * every poll       -> a heartbeat, which is what the status badge reads. It
                        has to be every poll and not only every ingest: a paused
                        prober ingests nothing, and a badge that went STALE the
                        moment sweeping was switched off would be reporting on
                        entirely the wrong thing.

SHIPS DISABLED. There is no autostart and no beat schedule; an operator starts
the loop, and sweeping itself begins disabled
(leaning-beat-schedule-must-ship-disabled).

Every rung is fired SIMULTANEOUSLY in one shot. That is the premise the whole
attribution rests on; see trail/prober.py.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from trail import prober


class ControlUnreachable(Exception):
    """The control endpoint could not be read this cycle."""


class Command(BaseCommand):
    help = "Fire every rung of a Trail in parallel and record the tick (host-side, no DB)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--vantage",
            default=os.environ.get("PING_TRAIL_VANTAGE", "") or "home-mac",
            help="Vantage slug whose orders this prober obeys (default: home-mac).",
        )
        parser.add_argument("--once", action="store_true", help="Fire a single sweep and exit.")
        parser.add_argument("--loop", action="store_true", help="Poll and sweep until interrupted.")
        parser.add_argument(
            "--control-url",
            default=os.environ.get("PING_TRAIL_CONTROL_URL", "") or settings.CONTROL_URL,
            help="Base URL of the control plane; the vantage slug is appended.",
        )
        parser.add_argument(
            "--ingest-url",
            default=os.environ.get("PING_TRAIL_INGEST_URL", "") or settings.INGEST_URL,
            help="POST results here (decouples the prober from the app).",
        )
        parser.add_argument(
            "--token",
            default=os.environ.get("PING_TRAIL_INGEST_TOKEN", "") or settings.INGEST_TOKEN,
            help="Shared secret sent as X-Ping-Trail-Token on both control and ingest.",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=30.0,
            help="FALLBACK seconds between polls, used only while the control plane is "
            "unreachable. The control plane is authoritative when it answers.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=2.0,
            help="FALLBACK per-sweep ICMP timeout, used only while the control plane is "
            "unreachable.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Sweep even when the control plane says sweeping is disabled "
            "(manual one-shot; does not apply to --loop).",
        )
        parser.add_argument("--quiet", action="store_true", help="Only print errors.")

    # ------------------------------------------------------------------ run --

    def handle(self, *args, **options):
        if not options["once"] and not options["loop"]:
            raise CommandError(
                "Pass --once for a single sweep or --loop to poll continuously. "
                "The prober ships disabled: it never starts on its own."
            )

        self.vantage = options["vantage"]
        self.control_url = options["control_url"].rstrip("/") + "/" + self.vantage + "/"
        self.token = options["token"]
        self.pid = os.getpid()
        self.hostname = socket.gethostname()

        if not options["quiet"]:
            self.stdout.write(
                self.style.MIGRATE_HEADING(f"PING_TRAIL prober — vantage '{self.vantage}'")
            )
            self.stdout.write(f"control  {self.control_url}")
            self.stdout.write(f"ingest   {options['ingest_url']}")
            self.stdout.write("no database configuration — ladder and orders arrive over HTTP")

        if options["once"]:
            self._cycle(options, single=True)
            return None

        ticks = 0
        try:
            while True:
                started = time.perf_counter()
                interval = self._cycle(options, single=False)
                ticks += 1
                drift = time.perf_counter() - started
                time.sleep(max(0.0, interval - drift))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING(f"\nStopped after {ticks} cycle(s)."))
        return None

    def _cycle(self, options, single):
        """One poll (+ sweep, if the orders say so). Returns the interval to wait."""
        try:
            orders = self._fetch_control()
        except ControlUnreachable as exc:
            # Keep the daemon alive on a fallback cadence. A prober that exits
            # when the site restarts would need a human to notice and restart it,
            # which defeats the whole point of a remote toggle.
            self.stderr.write(self.style.ERROR(f"  control unreachable: {exc}"))
            if single:
                raise CommandError(
                    f"Cannot reach the control plane at {self.control_url} — "
                    f"the prober has no ladder to fire at and will not guess one."
                )
            return options["interval"]

        interval = float(orders.get("interval_seconds") or options["interval"])
        timeout = float(orders.get("timeout_seconds") or options["timeout"])
        ladder = orders.get("ladder") or []
        enabled = bool(orders.get("enabled"))
        sweeping = enabled or (single and options["force"])

        if not sweeping:
            if not options["quiet"]:
                self.stdout.write(
                    f"{timezone.now().strftime('%H:%M:%S')}  sweeping disabled — polled, "
                    f"fired nothing (next poll in {interval:.0f}s)"
                )
            return interval

        if not ladder:
            self.stderr.write(
                self.style.ERROR(
                    f"  control returned an EMPTY ladder for trail "
                    f"'{orders.get('trail')}' — nothing to fire at. "
                    f"Run: manage.py discover_trail --trail {orders.get('trail')}"
                )
            )
            return interval

        self._sweep_once(orders, ladder, timeout, options)
        return interval

    # -------------------------------------------------------------- control --

    def _fetch_control(self):
        """GET the marching orders + ladder. Also this prober's heartbeat."""
        request = urllib.request.Request(
            self.control_url,
            headers={
                "X-Ping-Trail-Token": self.token,
                "X-Ping-Trail-Pid": str(self.pid),
                "X-Ping-Trail-Host": self.hostname,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise ControlUnreachable(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, socket.timeout, OSError, json.JSONDecodeError) as exc:
            raise ControlUnreachable(str(exc)) from exc

    # ---------------------------------------------------------------- sweep --

    def _sweep_once(self, orders, ladder, timeout, options):
        hosts = [rung["host"] for rung in ladder]
        fired_at = timezone.now()
        results, method = prober.sweep(hosts, timeout=timeout)

        payload = {
            "trail": orders.get("trail"),
            "vantage": self.vantage,
            "prober": method[:64],
            "fired_at": fired_at.isoformat(),
            "probes": [r.as_dict() for r in results],
            # Heartbeat rides along with the measurement.
            "pid": self.pid,
            "hostname": self.hostname,
        }
        outcome = self._post(payload, options)

        if not options["quiet"]:
            answered = sum(1 for r in results if r.rtt_ms is not None)
            deepest = max((r.rtt_ms for r in results if r.rtt_ms is not None), default=None)
            deepest_txt = "all timed out (VOID tick)" if deepest is None else f"deepest {deepest:.2f}ms"
            self.stdout.write(
                f"{fired_at.strftime('%H:%M:%S')}  {answered}/{len(results)} answered  "
                f"{deepest_txt}  [{method}]  {outcome}"
            )

    def _post(self, payload, options):
        url = options["ingest_url"]
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Ping-Trail-Token": self.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                if body.get("skipped_hosts"):
                    self.stderr.write(
                        self.style.WARNING(f"  hosts not on ladder: {', '.join(body['skipped_hosts'])}")
                    )
                return body.get("status", "ok")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            self.stderr.write(self.style.ERROR(f"  ingest {exc.code}: {detail}"))
            return f"ingest failed ({exc.code})"
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            self.stderr.write(self.style.ERROR(f"  ingest unreachable at {url}: {exc}"))
            return "ingest unreachable"
