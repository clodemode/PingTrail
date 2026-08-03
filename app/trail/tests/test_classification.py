"""Address-space classification — the v1 defect, pinned shut.

v1 called 192.168.1.1 `isp_hop1` because traceroute found it at hop 2. It is a
DSL router, still inside the house. These tests exist so that reasoning can
never come back: an address is judged on its address space and nothing else.
"""
from unittest import mock

from django.test import TestCase

from trail import discovery
from trail.models import RungKind, Rung, Trail, Vantage, is_in_house
from trail.services import apply_ladder

# Every RFC1918 block, plus RFC6598 CGNAT. Deliberately includes the edges of
# each range: an off-by-one in a netmask is exactly how "mostly right"
# classification code fails.
PRIVATE_ADDRESSES = [
    # 10.0.0.0/8
    "10.0.0.1",
    "10.255.255.254",
    "10.13.37.1",
    # 172.16.0.0/12
    "172.16.0.1",
    "172.31.255.254",
    "172.20.10.1",
    # 192.168.0.0/16
    "192.168.0.1",
    "192.168.1.1",  # the DSL router — the address that started this
    "192.168.11.1",  # the inner LAN router
    "192.168.255.254",
    # 100.64.0.0/10 — CGNAT. Looks public, is not.
    "100.64.0.1",
    "100.127.255.254",
    "100.100.50.20",
]

# NOT the RFC 5737 documentation ranges (192.0.2/24, 198.51.100/24,
# 203.0.113/24): Python's `ipaddress` reports all three as `is_private`, so
# `classify_host` calls them `home_router` and a test built on them would assert
# the opposite of the rule. 192.88.99.0/24 is the deprecated 6to4 relay anycast
# prefix (RFC 7526) — returned to IANA, assigned to no operator, and still
# globally scoped, which is exactly what a stand-in for a public hop must be.
PUBLIC_ADDRESSES = [
    "192.88.99.1",  # stands in for the first TRUE ISP device on a real ladder
    "1.1.1.1",
    "8.8.8.8",
    "193.0.14.129",
    "172.15.255.255",  # one below the 172.16/12 block — public
    "172.32.0.1",  # one above the 172.16/12 block — public
    "100.63.255.255",  # one below CGNAT — public
    "100.128.0.0",  # one above CGNAT — public
    "11.0.0.1",  # one above 10/8 — public
]


class PrivateAddressIsNeverAnIspHopTest(TestCase):
    """The single rule this spec exists to enforce."""

    def test_every_private_block_is_recognised_as_private(self):
        for host in PRIVATE_ADDRESSES:
            with self.subTest(host=host):
                self.assertTrue(
                    discovery.is_private_address(host),
                    f"{host} is RFC1918/CGNAT and must be recognised as private",
                )

    def test_no_private_address_is_ever_classified_as_an_isp_rung(self):
        for host in PRIVATE_ADDRESSES:
            with self.subTest(host=host):
                kind = discovery.classify_host(host)
                self.assertNotEqual(kind, "isp_hop", f"{host} is private — it cannot be an ISP hop")
                self.assertNotEqual(kind, "isp_dns", f"{host} is private — it cannot be ISP infrastructure")
                self.assertEqual(kind, "home_router")

    def test_public_addresses_are_isp_hops(self):
        for host in PUBLIC_ADDRESSES:
            with self.subTest(host=host):
                self.assertFalse(discovery.is_private_address(host))
                self.assertEqual(discovery.classify_host(host), "isp_hop")

    def test_the_default_gateway_is_a_gateway_not_a_home_router(self):
        """Identity beats range: the gateway is private, but it has its own kind."""
        self.assertEqual(
            discovery.classify_host("192.168.11.1", gateway="192.168.11.1"), "gateway"
        )
        self.assertEqual(
            discovery.classify_host("192.168.1.1", gateway="192.168.11.1"), "home_router"
        )

    def test_loopback_outranks_private(self):
        """127.0.0.1 is technically private; it is the noise floor, not a router."""
        self.assertEqual(discovery.classify_host("127.0.0.1"), "loopback")

    def test_hop_position_never_enters_into_it(self):
        """The same address classifies identically wherever traceroute found it."""
        for position in range(1, 8):
            with self.subTest(position=position):
                self.assertEqual(discovery.classify_host("192.168.1.1"), "home_router")
                self.assertEqual(discovery.classify_host("192.88.99.1"), "isp_hop")

    def test_a_garbage_address_classifies_as_nothing_rather_than_guessing(self):
        self.assertIsNone(discovery.classify_host("not-an-ip"))
        self.assertFalse(discovery.is_private_address("not-an-ip"))


class TwoRouterHouseLadderTest(TestCase):
    """The topology that exposed the v1 defect, end to end through build_ladder.

    Two routers in series inside the house — an inner LAN router, then the
    DSL box — and only the first PUBLIC address beyond them is a true ISP
    device. It is a common shape, and the one v1 got wrong.
    """

    HOPS = [(1, "192.168.11.1"), (2, "192.168.1.1"), (3, "192.88.99.1")]

    def _build(self, hops=None, resolver_list=("1.1.1.1",)):
        patches = [
            mock.patch.object(discovery, "default_gateway", return_value=("192.168.11.1", None)),
            mock.patch.object(
                discovery, "traceroute_hops", return_value=(hops if hops is not None else self.HOPS, None)
            ),
            mock.patch.object(discovery, "resolvers", return_value=(list(resolver_list), None)),
        ]
        for patch in patches:
            patch.start()
        try:
            return discovery.build_ladder()
        finally:
            for patch in patches:
                patch.stop()

    def test_two_home_routers_in_series_are_both_named_in_house(self):
        rungs, _notes = self._build()
        by_depth = {r["depth"]: r for r in rungs}

        self.assertEqual(by_depth[1]["kind"], "gateway")
        self.assertEqual(by_depth[1]["host"], "192.168.11.1")

        self.assertEqual(by_depth[2]["kind"], "home_router")
        self.assertEqual(by_depth[2]["host"], "192.168.1.1")
        self.assertEqual(by_depth[2]["label"], "home_router")

        self.assertEqual(by_depth[3]["kind"], "isp_hop")
        self.assertEqual(by_depth[3]["host"], "192.88.99.1")

    def test_the_only_isp_hop_is_not_numbered(self):
        """One ISP hop is `isp_hop` — `isp_hop2` would imply a sibling."""
        rungs, _notes = self._build()
        labels = [r["label"] for r in rungs]
        self.assertIn("isp_hop", labels)
        self.assertNotIn("isp_hop1", labels)
        self.assertNotIn("isp_hop2", labels)

    def test_two_isp_hops_are_numbered(self):
        rungs, _notes = self._build(hops=[(1, "192.168.11.1"), (2, "192.88.99.11"), (3, "192.88.99.12")])
        labels = [r["label"] for r in rungs]
        self.assertIn("isp_hop1", labels)
        self.assertIn("isp_hop2", labels)

    def test_a_private_resolver_is_the_house_router_doing_dns(self):
        rungs, _notes = self._build(resolver_list=("192.168.11.53",))
        resolver = next(r for r in rungs if r["host"] == "192.168.11.53")
        self.assertEqual(resolver["kind"], "home_router")
        self.assertEqual(resolver["label"], "local_dns")

    def test_an_all_private_path_warns_that_nothing_measures_the_isp(self):
        rungs, notes = self._build(hops=[(1, "192.168.11.1"), (2, "192.168.1.1"), (3, "10.0.0.1")])
        kinds = {r["kind"] for r in rungs if r["depth"] in (2, 3)}
        self.assertEqual(kinds, {"home_router"})
        self.assertTrue(
            any("no PUBLIC hop" in str(n) for n in notes),
            f"expected a warning that no ISP hop was found; got {[str(n) for n in notes]}",
        )

    def test_a_path_it_could_not_see_is_flagged_DEGRADED_not_merely_skipped(self):
        """Observed live on 2026-08-01 with the ISP hop at 100% loss.

        Traceroute saw only the private hop, `discover_trail` treated the ISP
        rung's absence as evidence it was gone, and retired the one rung that
        measured the ISP — taking the ISP attribution off the dashboard with it.
        A pass that cannot see the path must not be allowed to judge absences.
        """
        _rungs, notes = self._build(hops=[(1, "192.168.11.1"), (2, "192.168.1.1")])
        self.assertTrue(discovery.is_degraded(notes))

        _rungs, notes = self._build(hops=[])
        self.assertTrue(discovery.is_degraded(notes), "no hop beyond the gateway cannot judge absences")

    def test_a_failed_traceroute_is_degraded(self):
        with mock.patch.object(
            discovery, "default_gateway", return_value=("192.168.11.1", None)
        ), mock.patch.object(
            discovery, "traceroute_hops", return_value=([], "traceroute failed: 127")
        ), mock.patch.object(
            discovery, "resolvers", return_value=(["1.1.1.1"], None)
        ):
            _rungs, notes = discovery.build_ladder()
        self.assertTrue(discovery.is_degraded(notes), "a failed traceroute cannot judge absences")

    def test_a_healthy_pass_is_not_degraded(self):
        _rungs, notes = self._build()
        self.assertFalse(discovery.is_degraded(notes))

    def test_depths_are_unchanged_from_v1(self):
        """History stays readable: the ladder is re-LABELLED, never renumbered."""
        rungs, _notes = self._build()
        self.assertEqual(
            [(r["depth"], r["host"]) for r in rungs],
            [
                (0, "127.0.0.1"),
                (1, "192.168.11.1"),
                (2, "192.168.1.1"),
                (3, "192.88.99.1"),
                (4, "1.1.1.1"),
                (5, "8.8.8.8"),
                (6, "193.0.14.129"),
            ],
        )


class DegradedDiscoveryNeverRetiresARungTest(TestCase):
    """The command-level guard, end to end."""

    def setUp(self):
        vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=vantage)
        apply_ladder(
            self.trail,
            [
                {"depth": 0, "kind": "loopback", "host": "127.0.0.1", "label": "loopback", "note": ""},
                {"depth": 1, "kind": "gateway", "host": "192.168.11.1", "label": "gateway", "note": ""},
                {"depth": 2, "kind": "home_router", "host": "192.168.1.1", "label": "home_router", "note": ""},
                {"depth": 3, "kind": "isp_hop", "host": "192.88.99.1", "label": "isp_hop", "note": ""},
            ],
        )

    def _discover(self, hops):
        from io import StringIO

        from django.core.management import call_command

        patches = [
            mock.patch.object(discovery, "default_gateway", return_value=("192.168.11.1", None)),
            mock.patch.object(discovery, "traceroute_hops", return_value=(hops, None)),
            mock.patch.object(discovery, "resolvers", return_value=(["1.1.1.1"], None)),
        ]
        for patch in patches:
            patch.start()
        try:
            out = StringIO()
            call_command("discover_trail", "--trail", "home", stdout=out)
            return out.getvalue()
        finally:
            for patch in patches:
                patch.stop()

    def test_a_degraded_pass_leaves_the_isp_rung_active(self):
        output = self._discover([(1, "192.168.11.1"), (2, "192.168.1.1")])
        isp = Rung.objects.get(trail=self.trail, host="192.88.99.1")
        self.assertTrue(isp.is_active, "a lossy traceroute must not retire the ISP rung")
        self.assertIn("DEGRADED PASS", output)

    def test_a_clean_pass_still_retires_a_rung_that_genuinely_left_the_path(self):
        """The guard must not become a blanket refusal to ever retire anything."""
        self._discover([(1, "192.168.11.1"), (2, "192.168.1.1"), (3, "192.88.99.9")])
        old = Rung.objects.get(trail=self.trail, host="192.88.99.1")
        self.assertFalse(old.is_active, "a clean pass that saw a different ISP hop still retires the old one")
        self.assertTrue(Rung.objects.get(trail=self.trail, host="192.88.99.9").is_active)


class InHouseVerdictTest(TestCase):
    def test_the_in_house_set_is_exactly_the_equipment_the_operator_owns(self):
        self.assertTrue(is_in_house(RungKind.LOOPBACK))
        self.assertTrue(is_in_house(RungKind.GATEWAY))
        self.assertTrue(is_in_house(RungKind.HOME_ROUTER))
        self.assertFalse(is_in_house(RungKind.ISP_HOP))
        self.assertFalse(is_in_house(RungKind.ISP_DNS))
        self.assertFalse(is_in_house(RungKind.PUBLIC_DNS))
        self.assertFalse(is_in_house(RungKind.ANCHOR))


class OperatorCorrectionSurvivesRediscoveryTest(TestCase):
    """Discovery guesses; the operator corrects; the correction has to stick."""

    def setUp(self):
        vantage = Vantage.objects.create(slug="home-mac", name="Home Mac")
        self.trail = Trail.objects.create(slug="home", name="Home", vantage=vantage)
        self.ladder = [
            {"depth": 0, "kind": "loopback", "host": "127.0.0.1", "label": "loopback", "note": ""},
            {"depth": 1, "kind": "gateway", "host": "192.168.11.1", "label": "gateway", "note": ""},
            {"depth": 2, "kind": "home_router", "host": "192.168.1.1", "label": "home_router", "note": ""},
            {"depth": 3, "kind": "isp_hop", "host": "192.88.99.1", "label": "isp_hop", "note": ""},
        ]
        apply_ladder(self.trail, self.ladder)

    def _rung(self, host):
        return Rung.objects.get(trail=self.trail, host=host)

    def test_a_pinned_label_survives_a_rerun_of_discover_trail(self):
        rung = self._rung("192.168.1.1")
        rung.label = "DSL box"
        rung.label_pinned = True
        rung.save()

        apply_ladder(self.trail, self.ladder)

        rung.refresh_from_db()
        self.assertEqual(rung.label, "DSL box", "discovery must not overwrite an operator label")

    def test_a_pinned_kind_survives_a_rerun_of_discover_trail(self):
        rung = self._rung("192.88.99.1")
        rung.kind = "isp_dns"
        rung.kind_pinned = True
        rung.save()

        apply_ladder(self.trail, self.ladder)

        rung.refresh_from_db()
        self.assertEqual(rung.kind, "isp_dns", "discovery must not overwrite an operator kind")

    def test_pinning_one_field_leaves_the_other_free(self):
        rung = self._rung("192.168.1.1")
        rung.label = "DSL box"
        rung.label_pinned = True
        rung.save()

        changed = list(self.ladder)
        changed[2] = dict(changed[2], kind="isp_hop", label="isp_hop1")
        apply_ladder(self.trail, changed)

        rung.refresh_from_db()
        self.assertEqual(rung.label, "DSL box", "pinned label held")
        self.assertEqual(rung.kind, "isp_hop", "unpinned kind still tracks discovery")

    def test_an_unpinned_rung_still_follows_discovery(self):
        changed = list(self.ladder)
        changed[2] = dict(changed[2], label="renamed_by_discovery")
        apply_ladder(self.trail, changed)
        self.assertEqual(self._rung("192.168.1.1").label, "renamed_by_discovery")

    def test_rediscovery_does_not_orphan_history(self):
        """The whole point of relabelling in place rather than rebuilding."""
        from django.utils import timezone

        from trail.models import Sample

        sample = Sample.objects.create(trail=self.trail, fired_at=timezone.now())
        rung = self._rung("192.168.1.1")
        sample.probes.create(rung=rung, rtt_ms=3.9, sent=1, received=1)
        probe_pk = sample.probes.get().pk

        rung.label = "DSL box"
        rung.label_pinned = True
        rung.save()
        apply_ladder(self.trail, self.ladder)

        self.assertEqual(sample.probes.count(), 1)
        self.assertEqual(sample.probes.get().pk, probe_pk)
        self.assertEqual(sample.probes.get().rung_id, rung.pk)
