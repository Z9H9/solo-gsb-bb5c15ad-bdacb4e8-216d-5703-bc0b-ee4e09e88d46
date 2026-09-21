"""API 端到端测试（FastAPI TestClient + 临时 SQLite）。"""
import os
import tempfile
import unittest

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(), "test-api.db")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from parser import _compute_checksum  # noqa: E402


SENSOR_DEF = {
    "endian": "big",
    "fields": [
        {"name": "address", "type": "uint", "bits": 8},
        {"name": "function", "type": "fixed", "bits": 8, "value": 3},
        {"name": "mode", "type": "enum", "bits": 4,
         "values": {"0": "poll", "1": "event"}},
        {"name": "reserved", "type": "padding", "bits": 4},
        {"name": "payload", "type": "switch", "selector": "mode", "cases": [
            {"name": "poll_request", "match": 0, "fields": [
                {"name": "register", "type": "uint", "bits": 16},
                {"name": "count", "type": "uint", "bits": 8}]},
            {"name": "event_report", "match": 1, "fields": [
                {"name": "status", "type": "enum", "bits": 8,
                 "values": {"0": "ok", "1": "alarm"}},
                {"name": "battery_percent", "type": "uint", "bits": 8},
                {"name": "temperature", "type": "int", "bits": 16,
                 "endian": "little"}]}]},
        {"name": "crc", "type": "checksum", "bits": 16,
         "algorithm": "crc16/modbus", "endian": "little", "cover": "before"},
    ],
}


def modbus_frame(head: bytes) -> str:
    return (head + _compute_checksum(head, "crc16/modbus", None)
            .to_bytes(2, "little")).hex()


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 使用 with 语义触发 lifespan（数据库自动初始化）
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_01_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_02_register_and_docs(self):
        r = self.client.post("/api/v1/definitions", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "definition": SENSOR_DEF, "description": "demo"})
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["version"], "1.0.0")
        self.assertTrue(body["active"])

    def test_03_register_invalid_definition(self):
        r = self.client.post("/api/v1/definitions", json={
            "device_type": "x", "version": "1.0.0",
            "definition": {"fields": [{"name": "a", "type": "nope"}]}})
        self.assertEqual(r.status_code, 422)

    def test_04_register_bad_version(self):
        r = self.client.post("/api/v1/definitions", json={
            "device_type": "x", "version": "1.x",
            "definition": {"endian": "big",
                           "fields": [{"name": "a", "type": "uint", "bits": 8}]}})
        self.assertEqual(r.status_code, 422)

    def test_05_duplicate_conflict(self):
        r = self.client.post("/api/v1/definitions", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "definition": SENSOR_DEF})
        self.assertEqual(r.status_code, 409)

    def test_06_register_v2(self):
        r = self.client.post("/api/v1/definitions", json={
            "device_type": "sensor-ht", "version": "1.2.0",
            "definition": SENSOR_DEF})
        self.assertEqual(r.status_code, 201)

    def test_07_latest_version(self):
        r = self.client.get("/api/v1/definitions/sensor-ht/latest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], "1.2.0")
        self.client.post("/api/v1/definitions/sensor-ht/1.2.0/deactivate")
        r = self.client.get("/api/v1/definitions/sensor-ht/latest")
        self.assertEqual(r.json()["version"], "1.0.0")
        r = self.client.get(
            "/api/v1/definitions/sensor-ht/latest?include_inactive=true")
        self.assertEqual(r.json()["version"], "1.2.0")

    def test_08_list_filters(self):
        r = self.client.get("/api/v1/definitions")
        rows = r.json()
        self.assertEqual(len(rows), 2)
        r = self.client.get("/api/v1/definitions?device_type=sensor-ht&active=true")
        self.assertEqual(len(r.json()), 1)

    def test_09_parse_branch_and_checksum(self):
        # 轮询分支: address=01 function=03 mode=0 register=0010 count=01
        frame = modbus_frame(bytes.fromhex("010300001001"))
        r = self.client.post("/api/v1/parse", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "hex_payload": frame})
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()["parse"]
        self.assertTrue(p["valid"], p["errors"])
        self.assertEqual(p["branch_path"][0]["taken_case"], "poll_request")
        self.assertEqual(p["branch_path"][0]["selector_enum_label"], "poll")
        crc = p["checksums"][0]
        self.assertTrue(crc["passed"])
        self.assertEqual(crc["algorithm"], "crc16/modbus")
        offsets = {f["name"]: f["bit_offset"] for f in p["fields"] if f["taken"]}
        self.assertEqual(offsets["address"], 0)
        self.assertEqual(offsets["mode"], 16)
        self.assertEqual(offsets["payload.poll_request.register"], 24)
        vals = {f["name"]: f["value"] for f in p["fields"] if f["taken"]}
        self.assertEqual(vals["payload.poll_request.register"], 0x0010)

    def test_10_parse_event_branch_little_endian(self):
        # 事件分支: 01 03 1(mode高半字节) status=00 battery=64 temp=-100(9C FF LE)
        head = bytes.fromhex("01031000649CFF")
        r = self.client.post("/api/v1/parse", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "hex_payload": modbus_frame(head)})
        p = r.json()["parse"]
        self.assertTrue(p["valid"], p["errors"])
        self.assertEqual(p["branch_path"][0]["taken_case"], "event_report")
        vals = {f["name"]: f["value"] for f in p["fields"] if f["taken"]}
        self.assertEqual(vals["payload.event_report.temperature"], -100)
        self.assertEqual(vals["payload.event_report.status"], 0)

    def test_11_parse_bad_checksum(self):
        good = modbus_frame(bytes.fromhex("010300001001"))
        bad = good[:-2] + "0000"
        r = self.client.post("/api/v1/parse", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "hex_payload": bad})
        self.assertEqual(r.status_code, 200)
        p = r.json()["parse"]
        self.assertFalse(p["valid"])
        self.assertFalse(p["checksums"][0]["passed"])

    def test_12_parse_truncated(self):
        r = self.client.post("/api/v1/parse", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "hex_payload": "0103"})
        self.assertEqual(r.status_code, 422)

    def test_13_parse_missing_and_inactive(self):
        r = self.client.post("/api/v1/parse", json={
            "device_type": "ghost", "version": "1.0.0", "hex_payload": "00"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/v1/parse", json={
            "device_type": "sensor-ht", "version": "1.2.0",
            "hex_payload": "00"})
        self.assertEqual(r.status_code, 409)

    def test_14_spaces_and_0x_prefix_accepted(self):
        frame = modbus_frame(bytes.fromhex("010300001001"))
        pretty = "0x" + " ".join(frame[i:i + 2] for i in range(0, len(frame), 2))
        r = self.client.post("/api/v1/parse", json={
            "device_type": "sensor-ht", "version": "1.0.0",
            "hex_payload": pretty})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["parse"]["valid"])


if __name__ == "__main__":
    unittest.main()
