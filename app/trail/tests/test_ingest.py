"""Ingest endpoint — the only write path from a prober into the app."""
import json

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from trail.models import Rung, Sample, Trail, Vantage
from trail.services import apply_ladder

TOKEN = "test-token"


@override_settings(INGEST_TOKEN=TOKEN)
class IngestTest(TestCase):
    def setUp(self):
        vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=vantage)
        Rung.objects.create(trail=self.trail, depth=0, kind="loopback", host="127.0.0.1", label="loopback")
        Rung.objects.create(trail=self.trail, depth=1, kind="gateway", host="192.168.11.1", label="gateway")
        self.url = reverse("trail:ingest")
        self.fired_at = timezone.now()

    def post(self, payload, token=TOKEN):
        headers = {"HTTP_X_PING_TRAIL_TOKEN": token} if token is not None else {}
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json", **headers
        )

    def payload(self, probes=None, fired_at=None):
        return {
            "trail": "home",
            "prober": "icmp_dgram",
            "fired_at": (fired_at or self.fired_at).isoformat(),
            "probes": probes
            or [
                {"host": "127.0.0.1", "rtt_ms": 0.5, "sent": 1, "received": 1, "loss_pct": 0.0, "ttl": 64},
                {"host": "192.168.11.1", "rtt_ms": 3.2, "sent": 1, "received": 1, "loss_pct": 0.0, "ttl": 64},
            ],
        }

    def test_records_a_sweep(self):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "recorded")
        self.assertEqual(body["probes_recorded"], 2)
        self.assertEqual(Sample.objects.count(), 1)

    def test_rejects_a_missing_token(self):
        response = self.post(self.payload(), token=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Sample.objects.count(), 0)

    def test_rejects_a_wrong_token(self):
        response = self.post(self.payload(), token="nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Sample.objects.count(), 0)

    def test_timeout_is_stored_as_null_not_zero(self):
        probes = [
            {"host": "127.0.0.1", "rtt_ms": 0.5, "sent": 1, "received": 1, "loss_pct": 0.0},
            {"host": "192.168.11.1", "rtt_ms": None, "sent": 1, "received": 0, "loss_pct": 100.0, "error": "timeout"},
        ]
        self.post(self.payload(probes=probes))
        sample = Sample.objects.get()
        lost = sample.probes.get(rung__host="192.168.11.1")
        self.assertIsNone(lost.rtt_ms)
        self.assertNotEqual(lost.rtt_ms, 0.0)
        self.assertEqual(lost.loss_pct, 100.0)
        self.assertEqual(lost.error, "timeout")

    def test_duplicate_tick_is_idempotent(self):
        self.assertEqual(self.post(self.payload()).status_code, 201)
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "duplicate")
        self.assertEqual(Sample.objects.count(), 1)

    def test_unknown_trail_is_named_not_invented(self):
        payload = self.payload()
        payload["trail"] = "nope"
        response = self.post(payload)
        self.assertEqual(response.status_code, 404)
        self.assertIn("unknown trail", response.json()["detail"])
        self.assertEqual(Trail.objects.count(), 1)

    def test_host_not_on_the_ladder_is_reported_not_silently_added(self):
        probes = self.payload()["probes"] + [
            {"host": "8.8.8.8", "rtt_ms": 12.0, "sent": 1, "received": 1}
        ]
        response = self.post(self.payload(probes=probes))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["skipped_hosts"], ["8.8.8.8"])
        self.assertEqual(Rung.objects.filter(host="8.8.8.8").count(), 0)

    def test_bad_json_is_a_400(self):
        response = self.client.post(
            self.url, data="{not json", content_type="application/json",
            HTTP_X_PING_TRAIL_TOKEN=TOKEN,
        )
        self.assertEqual(response.status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


class ApplyLadderIdempotencyTest(TestCase):
    """discover_trail must be re-runnable when the ISP path changes."""

    def setUp(self):
        vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=vantage)
        self.ladder = [
            {"depth": 0, "kind": "loopback", "host": "127.0.0.1", "label": "loopback", "note": ""},
            {"depth": 1, "kind": "gateway", "host": "192.168.11.1", "label": "gateway", "note": ""},
            {"depth": 2, "kind": "isp_hop", "host": "10.0.0.1", "label": "isp_hop1", "note": ""},
        ]

    def test_rerunning_the_same_ladder_creates_nothing_new(self):
        apply_ladder(self.trail, self.ladder)
        self.assertEqual(self.trail.rungs.count(), 3)
        created, updated, deactivated, reactivated = apply_ladder(self.trail, self.ladder)
        self.assertEqual(created, [])
        self.assertEqual(updated, [])
        self.assertEqual(deactivated, [])
        self.assertEqual(self.trail.rungs.count(), 3)

    def test_changed_isp_path_updates_rather_than_duplicates(self):
        apply_ladder(self.trail, self.ladder)
        changed = list(self.ladder)
        changed[2] = {"depth": 2, "kind": "isp_hop", "host": "10.9.9.9", "label": "isp_hop1", "note": ""}
        created, _updated, deactivated, _re = apply_ladder(self.trail, changed)
        self.assertEqual(created, ["10.9.9.9"])
        self.assertEqual(deactivated, ["10.0.0.1"])
        # 4 rows, but only 3 active — history is preserved, not duplicated.
        self.assertEqual(self.trail.rungs.count(), 4)
        self.assertEqual(self.trail.rungs.filter(is_active=True).count(), 3)

    def test_a_dropped_rung_is_deactivated_never_deleted(self):
        """Deleting would cascade away its probes and rewrite the past."""
        apply_ladder(self.trail, self.ladder)
        sample = Sample.objects.create(trail=self.trail, fired_at=timezone.now())
        rung = self.trail.rungs.get(host="10.0.0.1")
        sample.probes.create(rung=rung, rtt_ms=5.0, sent=1, received=1)

        apply_ladder(self.trail, self.ladder[:2])
        rung.refresh_from_db()
        self.assertFalse(rung.is_active)
        self.assertEqual(sample.probes.count(), 1, "historical probe must survive")

    def test_a_returning_rung_is_reactivated(self):
        apply_ladder(self.trail, self.ladder)
        apply_ladder(self.trail, self.ladder[:2])
        _created, _updated, _deact, reactivated = apply_ladder(self.trail, self.ladder)
        self.assertEqual(reactivated, ["10.0.0.1"])
        self.assertEqual(self.trail.rungs.filter(is_active=True).count(), 3)
