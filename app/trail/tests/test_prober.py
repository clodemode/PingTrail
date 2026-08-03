"""Prober + discovery unit tests that need no network."""
import socket
import struct

from django.test import TestCase

from trail import discovery, prober


class IcmpParsingTest(TestCase):
    def test_detects_macos_shape_with_ip_header(self):
        """macOS SOCK_DGRAM keeps the 20-byte IPv4 header; ttl is byte 8."""
        ip_header = bytes([0x45, 0x00, 0x00, 0x54, 0x00, 0x00, 0x00, 0x00, 57, 0x01]) + b"\x00" * 10
        icmp = struct.pack("!BBHHH", 0, 0, 0, 1234, 1)
        offset, ttl = prober._icmp_offset(ip_header + icmp)
        self.assertEqual(offset, 20)
        self.assertEqual(ttl, 57)

    def test_detects_linux_shape_without_ip_header(self):
        icmp = struct.pack("!BBHHH", 0, 0, 0, 1234, 1)
        offset, ttl = prober._icmp_offset(icmp)
        self.assertEqual(offset, 0)
        self.assertIsNone(ttl)

    def test_error_is_attributed_to_the_original_destination(self):
        """An ICMP error's SOURCE is the router; the rung is the inner dest."""
        outer_ip = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, 1]) + b"\x00" * 10
        icmp_err = struct.pack("!BBHHH", prober.ICMP_TIME_EXCEEDED, 0, 0, 0, 0)
        # IPv4 header: source is bytes 12..16, destination is bytes 16..20.
        inner_ip = (
            bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 1, 1])  # ..through protocol (10)
            + b"\x00" * 2                             # header checksum -> 12
            + socket.inet_aton("192.168.11.1")        # 12..16 original source
            + socket.inet_aton("8.8.8.8")             # 16..20 original DESTINATION
        )
        self.assertEqual(len(inner_ip), 20)
        packet = outer_ip + icmp_err + inner_ip + b"\x00" * 8
        offset, _ttl = prober._icmp_offset(packet)
        self.assertEqual(prober._error_original_destination(packet, offset), "8.8.8.8")

    def test_truncated_quotation_returns_none_rather_than_guessing(self):
        outer_ip = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, 1]) + b"\x00" * 10
        icmp_err = struct.pack("!BBHHH", prober.ICMP_DEST_UNREACH, 0, 0, 0, 0)
        offset, _ttl = prober._icmp_offset(outer_ip + icmp_err)
        self.assertIsNone(prober._error_original_destination(outer_ip + icmp_err, offset))

    def test_checksum_matches_a_known_value(self):
        self.assertEqual(prober._checksum(b"\x00\x00"), 0xFFFF)


class ProbeResultTest(TestCase):
    def test_timeout_result_is_null_rtt_and_full_loss(self):
        result = prober.ProbeResult("1.1.1.1", error="timeout")
        self.assertIsNone(result.rtt_ms)
        self.assertEqual(result.loss_pct, 100.0)
        self.assertEqual(result.as_dict()["rtt_ms"], None)

    def test_answered_result_has_zero_loss(self):
        result = prober.ProbeResult("1.1.1.1", rtt_ms=12.5, received=1, ttl=58)
        self.assertEqual(result.loss_pct, 0.0)
        self.assertEqual(result.as_dict()["ttl"], 58)


class DiscoveryParsingTest(TestCase):
    def test_ipv4_validation(self):
        self.assertTrue(discovery.is_ipv4("192.168.11.1"))
        self.assertFalse(discovery.is_ipv4("999.1.1.1"))
        self.assertFalse(discovery.is_ipv4("not-an-ip"))
        self.assertFalse(discovery.is_ipv4(""))
        self.assertFalse(discovery.is_ipv4(None))

    def test_resolvers_parsed_from_a_resolv_conf(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write("# comment\nnameserver 1.1.1.1\nnameserver 9.9.9.9\nsearch lan\n")
            path = handle.name
        found, err = discovery.resolvers(path)
        self.assertIsNone(err)
        self.assertEqual(found, ["1.1.1.1", "9.9.9.9"])

    def test_missing_resolv_conf_degrades_with_a_message(self):
        found, err = discovery.resolvers("/nope/does/not/exist.conf")
        self.assertEqual(found, [])
        self.assertIn("could not read", err)

class LadderAssemblyTest(TestCase):
    """build_ladder must degrade gracefully, never crash, and never duplicate."""

    def _patch(self, gateway=("192.168.11.1", None), hops=None, resolvers=(["8.8.4.4"], None)):
        from unittest import mock

        hops = hops if hops is not None else ([(1, "192.168.11.1"), (2, "10.0.0.1"), (3, "192.88.99.2")], None)
        return (
            mock.patch.object(discovery, "default_gateway", return_value=gateway),
            mock.patch.object(discovery, "traceroute_hops", return_value=hops),
            mock.patch.object(discovery, "resolvers", return_value=resolvers),
        )

    def _build(self, **kwargs):
        patches = self._patch(**kwargs)
        for patch in patches:
            patch.start()
        try:
            return discovery.build_ladder()
        finally:
            for patch in patches:
                patch.stop()

    def test_full_ladder_assembles_in_depth_order(self):
        """CHANGED IN v2, deliberately — this test used to assert the defect.

        The fixture's hop 2 is 10.0.0.1, an RFC1918 address. v1 labelled it
        `isp_hop1` purely because traceroute found it at position 2, which is the
        exact reasoning that mislabelled a house DSL router as the ISP. It is a
        `home_router` now, and the single remaining public hop is `isp_hop`
        rather than `isp_hop2` — a number that implied a sibling that never
        existed. Depths are untouched.
        """
        rungs, _notes = self._build()
        depths = [r["depth"] for r in rungs]
        self.assertEqual(depths, sorted(depths))
        labels = [r["label"] for r in rungs]
        self.assertEqual(
            labels,
            ["loopback", "gateway", "home_router", "isp_hop", "isp_dns", "public_dns", "public_dns", "anchor"],
        )
        kinds = {r["host"]: r["kind"] for r in rungs}
        self.assertEqual(kinds["10.0.0.1"], "home_router", "a private hop is never an ISP hop")
        self.assertEqual(kinds["192.88.99.2"], "isp_hop")

    def test_both_public_resolvers_share_depth_five(self):
        rungs, _notes = self._build()
        depth5 = [r for r in rungs if r["depth"] == 5]
        self.assertEqual(len(depth5), 2)
        self.assertEqual({r["host"] for r in depth5}, {"1.1.1.1", "8.8.8.8"})

    def test_resolver_that_is_already_public_dns_is_skipped_with_a_reason(self):
        """A host whose resolver IS 1.1.1.1 — a genuine collision, not an error."""
        rungs, notes = self._build(resolvers=(["1.1.1.1"], None))
        hosts = [r["host"] for r in rungs]
        self.assertEqual(hosts.count("1.1.1.1"), 1, "1.1.1.1 must appear on the ladder once")
        skips = [str(n) for n in notes if n.level == "skip"]
        self.assertTrue(
            any("already on the ladder" in s for s in skips),
            f"expected a clear skip note, got: {skips}",
        )

    def test_missing_gateway_skips_that_rung_and_keeps_going(self):
        rungs, notes = self._build(gateway=(None, "no default route"))
        labels = [r["label"] for r in rungs]
        self.assertNotIn("gateway", labels)
        self.assertIn("anchor", labels)
        self.assertTrue(any("no default route" in str(n) for n in notes))

    def test_traceroute_failure_skips_isp_hops_and_keeps_going(self):
        rungs, notes = self._build(hops=([], "traceroute failed: 127"))
        labels = [r["label"] for r in rungs]
        self.assertNotIn("isp_hop1", labels)
        self.assertIn("gateway", labels)
        self.assertIn("anchor", labels)
        self.assertTrue(any("traceroute failed" in str(n) for n in notes))

    def test_unreadable_resolv_conf_skips_isp_dns_and_keeps_going(self):
        rungs, notes = self._build(resolvers=([], "could not read /etc/resolv.conf"))
        labels = [r["label"] for r in rungs]
        self.assertNotIn("isp_dns", labels)
        self.assertIn("public_dns", labels)
        self.assertTrue(any("could not read" in str(n) for n in notes))

    def test_only_the_gateway_reachable_still_yields_a_usable_ladder(self):
        rungs, _notes = self._build(
            gateway=("192.168.11.1", None), hops=([], "no hops"), resolvers=([], "none")
        )
        self.assertTrue(rungs)
        self.assertIn("loopback", [r["label"] for r in rungs])
        self.assertIn("gateway", [r["label"] for r in rungs])
