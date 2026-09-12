"""Strict media allocation reads use real protocol state, never UI fallbacks."""

import unittest
from unittest.mock import patch

from videohub import VideohubClient


DEVICE = ("VIDEOHUB DEVICE:\nDevice present: true\nModel name: Fake VideoHub\n"
          "Video inputs: 4\nVideo outputs: 3\n\n")
ROUTES = "VIDEO OUTPUT ROUTING:\n0 3\n1 0\n2 3\n\n"
PREAMBLE = "PROTOCOL PREAMBLE:\nVersion: 2.3\n\n"


class VideohubMediaStateTests(unittest.TestCase):
    def setUp(self):
        self.client = VideohubClient("fake-never-network")
        self.network = patch("videohub.socket.create_connection", side_effect=AssertionError("Hardware access forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def read(self, text):
        with patch.object(self.client, "_recv_initial_state", return_value=text) as receive:
            result = self.client.get_routing_state_strict()
        receive.assert_called_once_with()
        return result

    def test_complete_protocol_counts_and_routes_are_one_based(self):
        # Input/output counts need not match. Several TVs may share an input.
        expected = {"input_count": 4, "output_count": 3, "routing": [4, 1, 4]}
        self.assertEqual(self.read(PREAMBLE + DEVICE + ROUTES + "END PRELUDE:\n\n"), expected)
        self.assertEqual(self.read((PREAMBLE + DEVICE + ROUTES).replace("\n", "\r\n")), expected)

    def test_route_order_does_not_change_output_indexing(self):
        text = DEVICE + "VIDEO OUTPUT ROUTING:\n2 3\n0 3\n1 0\n\n"
        self.assertEqual(self.read(text)["routing"], [4, 1, 4])

    def test_unrelated_labels_cannot_change_counts_or_create_fallback_ports(self):
        labels = "INPUT LABELS:\n39 Phantom input\n\nOUTPUT LABELS:\n39 Phantom output\n\n"
        self.assertEqual(self.read(DEVICE + labels + ROUTES)["output_count"], 3)
        for text in ("", labels, PREAMBLE + ROUTES, PREAMBLE + DEVICE):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                self.read(text)

    def test_missing_terminators_and_merged_blocks_are_rejected(self):
        for text in (DEVICE + ROUTES.rstrip("\n"), DEVICE + ROUTES[:-1],
                     DEVICE[:-1] + ROUTES, DEVICE.rstrip("\n") + ROUTES,
                     DEVICE.replace("\n\n", "\n") + ROUTES,
                     DEVICE + ROUTES + "VIDEO OUTPUT ROUTING:\n0 1\n"):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                self.read(text)

    def test_missing_and_duplicate_output_routes_are_rejected(self):
        for rows in ("0 3\n1 0", "0 3\n0 3\n2 3", "0 3\n1 0\n1 1\n2 3", ""):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.read(DEVICE + "VIDEO OUTPUT ROUTING:\n" + rows + "\n\n")

    def test_out_of_range_or_negative_routes_are_rejected(self):
        for rows in ("0 4\n1 0\n2 3", "0 -1\n1 0\n2 3", "0 3\n1 0\n3 3", "-1 3\n1 0\n2 3"):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.read(DEVICE + "VIDEO OUTPUT ROUTING:\n" + rows + "\n\n")

    def test_malformed_routing_lines_are_rejected(self):
        for row in ("0", "0 3 1", "output 3", "0 3.0", "0 None"):
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                self.read(DEVICE + "VIDEO OUTPUT ROUTING:\n" + row + "\n1 0\n2 3\n\n")

    def test_device_must_be_present_and_supply_bounded_positive_counts(self):
        cases = [DEVICE.replace("Device present: true", "Device present: false"),
                 DEVICE.replace("Device present: true\n", ""),
                 DEVICE.replace("Video inputs: 4\n", ""),
                 DEVICE.replace("Video outputs: 3\n", "")]
        for field, value in (("Video inputs: 4", "0"), ("Video inputs: 4", "-1"),
                             ("Video outputs: 3", "65537"), ("Video outputs: 3", "3.5")):
            cases.append(DEVICE.replace(field, field.split(":")[0] + ": " + value))
        for device in cases:
            with self.subTest(device=device), self.assertRaises(RuntimeError):
                self.read(device + ROUTES)

    def test_duplicate_device_fields_and_protocol_blocks_are_rejected(self):
        for text in (DEVICE.replace("Video inputs: 4", "Video inputs: 9\nVideo inputs: 4") + ROUTES,
                     DEVICE + DEVICE + ROUTES, DEVICE + ROUTES + ROUTES):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                self.read(text)

    def test_unterminated_device_cannot_absorb_routing_block(self):
        # Both headers/counts exist but DEVICE lacks its terminating blank line.
        with self.assertRaises(RuntimeError):
            self.read(DEVICE[:-1] + ROUTES)

    def test_transport_failure_propagates_instead_of_returning_identity_routing(self):
        with patch.object(self.client, "_recv_initial_state", side_effect=OSError("Device unavailable")):
            with self.assertRaisesRegex(OSError, "Device unavailable"):
                self.client.get_routing_state_strict()


if __name__ == "__main__":
    unittest.main()
