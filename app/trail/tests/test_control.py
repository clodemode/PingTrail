"""The pull control plane: orders out, heartbeat in, and a prober with no DB."""
import io
import json
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from trail.models import ProberControl, Rung, Sample, Trail, Vantage
from trail.services import get_or_create_control, record_check_in

TOKEN = "test-token"


@override_settings(INGEST_TOKEN=TOKEN)
class ControlEndpointTest(TestCase):
    def setUp(self):
        self.vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=self.vantage)
        Rung.objects.create(trail=self.trail, depth=0, kind="loopback", host="127.0.0.1", label="loopback")
        Rung.objects.create(trail=self.trail, depth=1, kind="gateway", host="192.168.11.1", label="gateway")
        Rung.objects.create(trail=self.trail, depth=2, kind="home_router", host="192.168.1.1", label="home_router")
        self.retired = Rung.objects.create(
            trail=self.trail, depth=3, kind="isp_hop", host="10.9.9.9", label="gone", is_active=False
        )
        self.url = reverse("trail:control", kwargs={"vantage_slug": "home-mac"})

    def get(self, token=TOKEN, **headers):
        kwargs = {"HTTP_X_PING_TRAIL_TOKEN": token} if token is not None else {}
        kwargs.update(headers)
        return self.client.get(self.url, **kwargs)

    def test_returns_orders_and_the_full_ladder(self):
        control = get_or_create_control(self.vantage, trail=self.trail)
        control.enabled = True
        control.interval_seconds = 45
        control.timeout_seconds = 3.5
        control.save()

        body = self.get().json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["enabled"])
        self.assertEqual(body["interval_seconds"], 45)
        self.assertEqual(body["timeout_seconds"], 3.5)
        self.assertEqual(body["trail"], "home")
        self.assertEqual(
            [r["host"] for r in body["ladder"]], ["127.0.0.1", "192.168.11.1", "192.168.1.1"]
        )

    def test_retired_rungs_are_not_handed_to_the_prober(self):
        """History keeps them; the sweep must not keep firing at them."""
        hosts = [r["host"] for r in self.get().json()["ladder"]]
        self.assertNotIn("10.9.9.9", hosts)

    def test_rejects_a_missing_or_wrong_token(self):
        self.assertEqual(self.get(token=None).status_code, 401)
        self.assertEqual(self.get(token="nope").status_code, 401)

    def test_unknown_vantage_is_named_not_invented(self):
        response = self.client.get(
            reverse("trail:control", kwargs={"vantage_slug": "nowhere"}),
            HTTP_X_PING_TRAIL_TOKEN=TOKEN,
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("unknown vantage", response.json()["detail"])
        self.assertEqual(Vantage.objects.count(), 1)

    def test_the_poll_itself_is_the_heartbeat(self):
        """Essential: a paused prober ingests nothing, so ingest cannot be the
        only heartbeat or the badge would call a healthy daemon STALE."""
        self.get(HTTP_X_PING_TRAIL_PID="4242", HTTP_X_PING_TRAIL_HOST="krismac")
        control = ProberControl.objects.get(vantage=self.vantage)
        self.assertIsNotNone(control.last_seen_at)
        self.assertEqual(control.pid, 4242)
        self.assertEqual(control.hostname, "krismac")

    def test_polling_while_disabled_still_heartbeats(self):
        control = get_or_create_control(self.vantage, trail=self.trail)
        control.enabled = False
        control.save()
        self.get()
        control.refresh_from_db()
        self.assertIsNotNone(control.last_seen_at)
        self.assertEqual(control.status["code"], "STOPPED")


class ProberStatusBadgeTest(TestCase):
    """The badge reads the heartbeat. It never guesses, and it never conflates
    'sweeping is off' with 'the daemon is gone'."""

    def setUp(self):
        self.vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=self.vantage)
        self.control = get_or_create_control(self.vantage, trail=self.trail)
        self.control.interval_seconds = 30
        self.control.save()

    def _seen(self, seconds_ago):
        self.control.last_seen_at = timezone.now() - timedelta(seconds=seconds_ago)
        self.control.save(update_fields=["last_seen_at"])

    def test_never_seen_is_stale(self):
        self.assertIsNone(self.control.last_seen_at)
        self.assertEqual(self.control.status["code"], "STALE")
        self.assertIn("ever checked in", self.control.status["detail"])

    def test_fresh_heartbeat_with_sweeping_on_is_alive(self):
        self.control.enabled = True
        self.control.save()
        self._seen(5)
        self.assertEqual(self.control.status["code"], "ALIVE")
        self.assertTrue(self.control.status["sweeping"])

    def test_fresh_heartbeat_with_sweeping_off_is_stopped_not_stale(self):
        self.control.enabled = False
        self.control.save()
        self._seen(5)
        status = self.control.status
        self.assertEqual(status["code"], "STOPPED")
        self.assertFalse(status["sweeping"])
        self.assertIn("still polling", status["meaning"])

    def test_heartbeat_older_than_two_intervals_is_stale(self):
        self.control.enabled = True
        self.control.save()
        self._seen(61)  # 2 * 30s + 1
        self.assertEqual(self.control.status["code"], "STALE")

    def test_just_inside_two_intervals_is_not_stale(self):
        self.control.enabled = True
        self.control.save()
        self._seen(59)
        self.assertEqual(self.control.status["code"], "ALIVE")

    def test_stale_says_the_site_cannot_start_the_daemon(self):
        self._seen(600)
        self.assertIn("cannot start", self.control.status["meaning"])

    def test_the_stale_window_follows_the_configured_interval(self):
        self.control.enabled = True
        self.control.interval_seconds = 5
        self.control.save()
        self._seen(11)
        self.assertEqual(self.control.status["code"], "STALE")
        self.control.interval_seconds = 300
        self.control.save()
        self.assertEqual(self.control.status["code"], "ALIVE")


class ControlUpdateViewTest(TestCase):
    def setUp(self):
        self.vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=self.vantage)
        self.url = reverse("trail:control_update", kwargs={"slug": "home"})

    def _control(self):
        return ProberControl.objects.get(vantage=self.vantage)

    def test_toggle_flips_sweeping(self):
        self.client.post(self.url, {"action": "toggle"})
        self.assertTrue(self._control().enabled)
        self.client.post(self.url, {"action": "toggle"})
        self.assertFalse(self._control().enabled)

    def test_interval_and_timeout_are_settable(self):
        self.client.post(self.url, {"action": "settings", "interval_seconds": "15", "timeout_seconds": "1.5"})
        control = self._control()
        self.assertEqual(control.interval_seconds, 15)
        self.assertEqual(control.timeout_seconds, 1.5)

    def test_absurd_values_are_clamped_not_accepted(self):
        self.client.post(self.url, {"action": "settings", "interval_seconds": "0", "timeout_seconds": "999"})
        control = self._control()
        self.assertEqual(control.interval_seconds, 5)
        self.assertEqual(control.timeout_seconds, 30.0)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_the_card_comes_back_rendered(self):
        response = self.client.post(self.url, {"action": "toggle"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "control-card")
        self.assertContains(response, "Stop sweeping")

    def test_the_card_says_the_page_controls_sweeping_not_the_daemon(self):
        response = self.client.post(self.url, {"action": "toggle"})
        self.assertContains(response, "not the daemon")

    def test_status_endpoint_renders_the_badge(self):
        response = self.client.get(reverse("trail:control_status", kwargs={"slug": "home"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "STALE")


@override_settings(INGEST_TOKEN=TOKEN)
class IngestHeartbeatTest(TestCase):
    def setUp(self):
        self.vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=self.vantage)
        Rung.objects.create(trail=self.trail, depth=0, kind="loopback", host="127.0.0.1", label="loopback")
        self.control = get_or_create_control(self.vantage, trail=self.trail)

    def test_an_ingest_records_a_sweep_heartbeat(self):
        payload = {
            "trail": "home",
            "fired_at": timezone.now().isoformat(),
            "pid": 777,
            "hostname": "krismac",
            "probes": [{"host": "127.0.0.1", "rtt_ms": 0.4, "sent": 1, "received": 1}],
        }
        response = self.client.post(
            reverse("trail:ingest"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_PING_TRAIL_TOKEN=TOKEN,
        )
        self.assertEqual(response.status_code, 201)
        self.control.refresh_from_db()
        self.assertIsNotNone(self.control.last_sweep_at)
        self.assertEqual(self.control.pid, 777)

    def test_a_reported_error_raises_the_failure_count_and_a_clean_run_clears_it(self):
        record_check_in(self.control, error="boom")
        self.control.refresh_from_db()
        self.assertEqual(self.control.consecutive_failures, 1)
        self.assertEqual(self.control.last_error, "boom")

        record_check_in(self.control, error="boom again")
        self.control.refresh_from_db()
        self.assertEqual(self.control.consecutive_failures, 2)

        record_check_in(self.control)
        self.control.refresh_from_db()
        self.assertEqual(self.control.consecutive_failures, 0)
        self.assertEqual(self.control.last_error, "")


# --------------------------------------------------------------------------
# THE ACCEPTANCE TEST FOR THE ARCHITECTURAL LEAK.
#
# SimpleTestCase forbids database queries outright (databases = []). If
# ping_sweep so much as opens a connection, this fails — which is precisely the
# guarantee the spec asks for: "ping_sweep starts and runs correctly with no
# DJANGO_DB_PATH set and no DB reachable."
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


LADDER = [
    {"depth": 0, "kind": "loopback", "host": "127.0.0.1", "label": "loopback"},
    {"depth": 1, "kind": "gateway", "host": "192.168.11.1", "label": "gateway"},
]


class PingSweepHoldsNoDatabaseTest(SimpleTestCase):
    """No DB allowed. None needed."""

    def _run(self, orders, argv, sweep_results=None):
        """Run ping_sweep with HTTP stubbed. Returns (stdout, posted_payloads)."""
        posted = []

        def fake_urlopen(request, timeout=None):
            if request.get_method() == "GET":
                return _FakeResponse(orders)
            posted.append(json.loads(request.data.decode("utf-8")))
            return _FakeResponse({"status": "recorded", "skipped_hosts": []})

        results = sweep_results if sweep_results is not None else [
            __import__("trail.prober", fromlist=["prober"]).ProbeResult("127.0.0.1", rtt_ms=0.4, received=1),
            __import__("trail.prober", fromlist=["prober"]).ProbeResult("192.168.11.1", rtt_ms=3.9, received=1),
        ]

        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch(
            "trail.prober.sweep", return_value=(results, "icmp_dgram")
        ):
            call_command("ping_sweep", *argv, stdout=out, stderr=io.StringIO())
        return out.getvalue(), posted

    def test_a_sweep_runs_end_to_end_without_touching_the_database(self):
        orders = {
            "status": "ok",
            "vantage": "home-mac",
            "enabled": True,
            "interval_seconds": 30,
            "timeout_seconds": 2.0,
            "trail": "home",
            "ladder": LADDER,
        }
        stdout, posted = self._run(orders, ["--vantage", "home-mac", "--once"])
        self.assertIn("2/2 answered", stdout)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["trail"], "home")
        self.assertEqual([p["host"] for p in posted[0]["probes"]], ["127.0.0.1", "192.168.11.1"])

    def test_the_ladder_comes_from_http_not_the_orm(self):
        orders = {
            "status": "ok",
            "enabled": True,
            "interval_seconds": 30,
            "timeout_seconds": 2.0,
            "trail": "home",
            "ladder": [{"depth": 0, "kind": "loopback", "host": "127.0.0.1", "label": "loopback"}],
        }
        results = [__import__("trail.prober", fromlist=["prober"]).ProbeResult("127.0.0.1", rtt_ms=0.4, received=1)]
        _stdout, posted = self._run(orders, ["--vantage", "home-mac", "--once"], sweep_results=results)
        self.assertEqual([p["host"] for p in posted[0]["probes"]], ["127.0.0.1"])

    def test_the_command_module_imports_no_models(self):
        """A grep-proof guard: an ORM import is how the leak comes back."""
        from trail.management.commands import ping_sweep as module

        source = open(module.__file__, encoding="utf-8").read()
        self.assertNotIn("from trail.models import", source)
        self.assertNotIn("trail.services import", source)
        self.assertNotIn(".objects.", source)

    def test_the_heartbeat_rides_along_with_the_measurement(self):
        orders = {
            "status": "ok",
            "enabled": True,
            "interval_seconds": 30,
            "timeout_seconds": 2.0,
            "trail": "home",
            "ladder": LADDER,
        }
        _stdout, posted = self._run(orders, ["--vantage", "home-mac", "--once"])
        self.assertIn("pid", posted[0])
        self.assertIn("hostname", posted[0])

    def test_disabled_orders_mean_polled_but_fired_nothing(self):
        orders = {
            "status": "ok",
            "enabled": False,
            "interval_seconds": 30,
            "timeout_seconds": 2.0,
            "trail": "home",
            "ladder": LADDER,
        }
        stdout, posted = self._run(orders, ["--vantage", "home-mac", "--once"])
        self.assertIn("sweeping disabled", stdout)
        self.assertEqual(posted, [], "no tick may be ingested while sweeping is off")

    def test_force_overrides_disabled_for_a_manual_one_shot(self):
        orders = {
            "status": "ok",
            "enabled": False,
            "interval_seconds": 30,
            "timeout_seconds": 2.0,
            "trail": "home",
            "ladder": LADDER,
        }
        _stdout, posted = self._run(orders, ["--vantage", "home-mac", "--once", "--force"])
        self.assertEqual(len(posted), 1)

    def test_neither_once_nor_loop_refuses_to_start(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("ping_sweep", "--vantage", "home-mac", stdout=io.StringIO())
        self.assertIn("ships disabled", str(ctx.exception))

    def test_an_unreachable_control_plane_refuses_to_guess_a_ladder(self):
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "ping_sweep", "--vantage", "home-mac", "--once",
                    stdout=io.StringIO(), stderr=io.StringIO(),
                )
        self.assertIn("Cannot reach the control plane", str(ctx.exception))

    def test_an_empty_ladder_is_named_not_swept(self):
        orders = {
            "status": "ok",
            "enabled": True,
            "interval_seconds": 30,
            "timeout_seconds": 2.0,
            "trail": "home",
            "ladder": [],
        }
        posted = []

        def fake_urlopen(request, timeout=None):
            if request.get_method() == "GET":
                return _FakeResponse(orders)
            posted.append(1)
            return _FakeResponse({"status": "recorded"})

        err = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            call_command("ping_sweep", "--vantage", "home-mac", "--once", stdout=io.StringIO(), stderr=err)
        self.assertIn("EMPTY ladder", err.getvalue())
        self.assertEqual(posted, [])


class LiveRegionTest(TestCase):
    """Polled regions must render on their own, and must not contain the panel."""

    def setUp(self):
        vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=vantage)
        rung = Rung.objects.create(
            trail=self.trail, depth=0, kind="loopback", host="127.0.0.1", label="loopback"
        )
        sample = Sample.objects.create(trail=self.trail, fired_at=timezone.now())
        sample.probes.create(rung=rung, rtt_ms=0.4, sent=1, received=1)

    def test_each_region_renders_and_re_arms_its_own_poll(self):
        for name, marker in [
            ("live_stats", "live-stats"),
            ("live_chart", "live-chart"),
            ("live_rollup", "live-rollup"),
            ("live_ticks", "live-ticks"),
        ]:
            with self.subTest(region=name):
                response = self.client.get(reverse(f"trail:{name}", kwargs={"slug": "home"}))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'id="{marker}"')
                self.assertContains(response, "hx-trigger")

    def test_no_polled_region_contains_the_inline_detail_panel(self):
        """If a region owned #panel-tick, every poll would close an open panel."""
        for name in ("live_stats", "live_chart", "live_rollup", "live_ticks"):
            with self.subTest(region=name):
                response = self.client.get(reverse(f"trail:{name}", kwargs={"slug": "home"}))
                self.assertNotContains(response, 'id="panel-tick"')

    def test_the_dashboard_owns_the_panel_exactly_once(self):
        response = self.client.get(reverse("trail:dashboard", kwargs={"slug": "home"}))
        self.assertEqual(response.content.decode().count('id="panel-tick"'), 1)

    def test_the_ladder_does_not_poll(self):
        """A poll would yank a half-typed correction out from under the operator."""
        response = self.client.get(reverse("trail:dashboard", kwargs={"slug": "home"}))
        self.assertNotContains(response, "live/ladder")


class RungInlineEditTest(TestCase):
    def setUp(self):
        vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=vantage)
        self.rung = Rung.objects.create(
            trail=self.trail, depth=2, kind="isp_hop", host="192.168.1.1", label="isp_hop1"
        )

    def test_the_edit_form_is_inline_and_never_a_modal(self):
        response = self.client.get(
            reverse("trail:rung_edit", kwargs={"slug": "home", "pk": self.rung.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("modal", body)
        self.assertIn("hx-post", body)

    def test_saving_a_kind_pins_it(self):
        self.client.post(
            reverse("trail:rung_update", kwargs={"slug": "home", "pk": self.rung.pk}),
            {"kind": "home_router", "label": "isp_hop1"},
        )
        self.rung.refresh_from_db()
        self.assertEqual(self.rung.kind, "home_router")
        self.assertTrue(self.rung.kind_pinned)
        self.assertFalse(self.rung.label_pinned)

    def test_saving_a_label_pins_it(self):
        self.client.post(
            reverse("trail:rung_update", kwargs={"slug": "home", "pk": self.rung.pk}),
            {"kind": "isp_hop", "label": "DSL box"},
        )
        self.rung.refresh_from_db()
        self.assertEqual(self.rung.label, "DSL box")
        self.assertTrue(self.rung.label_pinned)

    def test_an_unknown_kind_is_refused_not_stored(self):
        self.client.post(
            reverse("trail:rung_update", kwargs={"slug": "home", "pk": self.rung.pk}),
            {"kind": "wishful_thinking", "label": "isp_hop1"},
        )
        self.rung.refresh_from_db()
        self.assertEqual(self.rung.kind, "isp_hop")
        self.assertFalse(self.rung.kind_pinned)

    def test_unpin_hands_the_rung_back_to_discovery(self):
        self.rung.kind_pinned = True
        self.rung.label_pinned = True
        self.rung.save()
        self.client.post(
            reverse("trail:rung_update", kwargs={"slug": "home", "pk": self.rung.pk}),
            {"kind": "isp_hop", "label": "isp_hop1", "unpin": "1"},
        )
        self.rung.refresh_from_db()
        self.assertFalse(self.rung.kind_pinned)
        self.assertFalse(self.rung.label_pinned)

    def test_a_rung_from_another_trail_is_a_404(self):
        other = Trail.objects.create(
            slug="other", name="Other", vantage=self.trail.vantage
        )
        stray = Rung.objects.create(
            trail=other, depth=0, kind="loopback", host="127.0.0.1", label="loopback"
        )
        response = self.client.get(
            reverse("trail:rung_edit", kwargs={"slug": "home", "pk": stray.pk})
        )
        self.assertEqual(response.status_code, 404)
