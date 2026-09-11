import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).parents[1] / "python" / "mxswitch.py"
SPEC = importlib.util.spec_from_file_location("mxswitch", MODULE_PATH)
mxswitch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mxswitch)


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))

    def read(self, timeout_ms=100):
        return self.replies.pop(0) if self.replies else b""


class ChangeHostTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes signature")
    def test_setupapi_functions_accept_pointer_sized_handles(self):
        for name in (
            "SetupDiEnumDeviceInterfaces",
            "SetupDiGetDeviceInterfaceDetailW",
            "SetupDiDestroyDeviceInfoList",
        ):
            function = getattr(mxswitch.setupapi, name)
            self.assertIsNotNone(function.argtypes, name)
            self.assertIs(function.argtypes[0], mxswitch.wintypes.HANDLE, name)

    def test_get_host_info_decodes_count_current_and_capability(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 0A 0A 03 01 01") + bytes(13),
        ])

        info = mxswitch.get_host_info(dev, 0x02, 0x11, 0x0A)

        self.assertEqual(info, {
            "host_count": 3,
            "current_host": 1,
            "capabilities": 1,
        })
        self.assertEqual(
            dev.writes,
            [bytes.fromhex("11 02 0A 0A") + bytes(16)],
        )

    def test_get_cookies_uses_function_two_and_returns_one_byte_per_slot(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 0A 2A A1 00 7F") + bytes(13),
        ])

        cookies = mxswitch.get_cookies(dev, 0x02, 0x11, 0x0A, 3)

        self.assertEqual(cookies, [0xA1, 0x00, 0x7F])
        self.assertEqual(
            dev.writes,
            [bytes.fromhex("11 02 0A 2A") + bytes(16)],
        )

    def test_cookie_value_does_not_claim_pairing_state(self):
        lines = mxswitch.format_slot_lines(
            {"host_count": 3, "current_host": 0, "capabilities": 1},
            [0x00, 0x22, 0xFF],
        )

        self.assertEqual(lines, [
            "Slot 1: current=yes  cookie=0x00  paired=unknown",
            "Slot 2: current=no   cookie=0x22  paired=unknown",
            "Slot 3: current=no   cookie=0xFF  paired=unknown",
        ])

    def test_missing_cookie_response_is_reported_as_unknown(self):
        lines = mxswitch.format_slot_lines(
            {"host_count": 3, "current_host": 2, "capabilities": 0},
            None,
        )

        self.assertEqual(lines[0], "Slot 1: current=no   cookie=unknown  paired=unknown")
        self.assertEqual(lines[2], "Slot 3: current=yes  cookie=unknown  paired=unknown")

    def test_invalid_host_info_response_is_rejected(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 0A 0A 00 03 00") + bytes(13),
        ])

        self.assertIsNone(mxswitch.get_host_info(dev, 0x02, 0x11, 0x0A))

    def test_feature_lookup_returns_dynamic_index_and_version(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 00 0A 0B 00 02") + bytes(13),
        ])

        feature = mxswitch.get_feature(dev, 0x02, 0x11, 0x1815)

        self.assertEqual(feature, {"index": 0x0B, "type": 0, "version": 2})
        self.assertEqual(
            dev.writes,
            [bytes.fromhex("11 02 00 0A 18 15 00") + bytes(13)],
        )

    def test_device_identity_decodes_transport_ids(self):
        dev = FakeDevice([
            bytes.fromhex(
                "11 02 02 0A 03 81 85 8A AB 00 02 B0 34 00 00 00 00 01 01 00"
            ),
        ])

        identity = mxswitch.get_device_identity(dev, 0x02, 0x11, 0x02)

        self.assertEqual(identity["unit_id"], "81858AAB")
        self.assertEqual(identity["transport_ids"], {"btleid": "B034"})

    def test_device_name_is_read_in_chunks(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 03 0A 0C") + bytes(15),
            bytes.fromhex("11 02 03 1A 4D 58 20 4D 61 73 74 65 72 20 33 53") + bytes(4),
        ])

        name = mxswitch.get_device_name(dev, 0x02, 0x11, 0x03)

        self.assertEqual(name, "MX Master 3S")

    def test_hosts_info_exposes_delete_capability_and_real_slot_status(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 0B 0A 13 08 03 00") + bytes(12),
            bytes.fromhex("11 02 0B 1A 00 01 05 01 0F 18") + bytes(10),
            bytes.fromhex("11 02 0B 1A 01 00 00 00 00 18") + bytes(10),
            bytes.fromhex("11 02 0B 1A 02 01 04 01 0F 18") + bytes(10),
        ])

        feature_info = mxswitch.get_hosts_info(dev, 0x02, 0x11, 0x0B)
        slots = [
            mxswitch.get_host_slot_info(dev, 0x02, 0x11, 0x0B, slot)
            for slot in range(3)
        ]

        self.assertFalse(feature_info["capabilities"] & 0x08)
        self.assertEqual([slot["status"] for slot in slots], [1, 0, 1])
        self.assertEqual([slot["bus_type"] for slot in slots], [5, 0, 4])

    def test_hosts_info_status_replaces_unknown_pairing_text(self):
        lines = mxswitch.format_slot_lines(
            {"host_count": 3, "current_host": 0, "capabilities": 0},
            [0, 0, 0],
            [
                {"status": 1, "bus_type": 5},
                {"status": 0, "bus_type": 0},
                {"status": 7, "bus_type": 0},
            ],
        )

        self.assertIn("paired=yes bus=BLE Pro", lines[0])
        self.assertIn("paired=no bus=Undefined", lines[1])
        self.assertIn("paired=unknown(raw=0x07)", lines[2])

    def test_reset_assessment_only_reads_documented_hosts_info(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 00 0A 0B 00 02") + bytes(13),
            bytes.fromhex("11 02 0B 0A 13 08 03 00") + bytes(12),
        ])

        assessment = mxswitch.assess_reset_all(dev, 0x02, 0x11)

        self.assertFalse(assessment["available"])
        self.assertFalse(assessment["delete_advertised"])
        self.assertIn("does not advertise", assessment["reason"])
        self.assertEqual(dev.writes, [
            bytes.fromhex("11 02 00 0A 18 15 00") + bytes(13),
            bytes.fromhex("11 02 0B 0A") + bytes(16),
        ])

    def test_advertised_delete_still_requires_verified_wire_command(self):
        dev = FakeDevice([
            bytes.fromhex("11 02 00 0A 0B 00 02") + bytes(13),
            bytes.fromhex("11 02 0B 0A 1B 08 03 00") + bytes(12),
        ])

        assessment = mxswitch.assess_reset_all(dev, 0x02, 0x11)

        self.assertFalse(assessment["available"])
        self.assertTrue(assessment["delete_advertised"])
        self.assertIn("wire command", assessment["reason"])


if __name__ == "__main__":
    unittest.main()
