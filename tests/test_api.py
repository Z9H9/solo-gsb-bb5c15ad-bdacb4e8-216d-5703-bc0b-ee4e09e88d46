import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="parser-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.checksum import crc16_ccitt  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

SENSOR_DEF = {
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def make_message(body: bytes) -> str:
    crc = crc16_ccitt(body)
    return (body + crc.to_bytes(2, "big")).hex()


def test_register_definition():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["device_type"] == "temp_sensor"
    assert data["version"] == "1.0.0"
    assert data["active"] is True
    assert len(data["fields"]) == 6


def test_register_duplicate_conflict():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 409


def test_register_invalid_condition_reference():
    bad = {
        "device_type": "bad_dev",
        "version": "1.0.0",
        "fields": [
            {"name": "a", "type": "uint", "bit_length": 8,
             "condition": {"field": "ghost", "op": "eq", "value": 1}},
        ],
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_register_checksum_width_mismatch():
    bad = {
        "device_type": "bad_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 8}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_parse_temperature_branch():
    # header=0xAA, msg_type=1, flags=0, temperature raw=235 (LE) -> 23.5 °C
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["algorithm"] == "crc16-ccitt"
    assert data["consumed_bits"] == 48

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["header"]["value"] == 0xAA
    assert fields["header"]["bit_offset"] == 0
    assert fields["msg_type"]["value"] == 1
    assert fields["msg_type"]["bit_offset"] == 8
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["temperature"]["bit_offset"] == 16
    assert "humidity" not in fields  # skipped by branch

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "parsed"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"


def test_parse_humidity_branch():
    # msg_type=2 -> humidity branch, temperature skipped
    body = bytes([0xAA, 0x20, 0x55])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["humidity"]["value"] == 0x55
    assert fields["humidity"]["bit_offset"] == 16
    assert "temperature" not in fields
    assert data["checksum"]["valid"] is True


def test_parse_checksum_mismatch():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    good = make_message(body)
    bad = good[:-2] + ("00" if good[-2:] != "00" else "ff")
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": bad},
    )
    assert resp.status_code == 200
    assert resp.json()["checksum"]["valid"] is False


def test_parse_truncated_message():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "AA10"},
    )
    assert resp.status_code == 422


def test_parse_invalid_hex():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "zzzz"},
    )
    assert resp.status_code == 422


def test_parse_unknown_device():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "nope", "hex": "AABB"},
    )
    assert resp.status_code == 404


def test_trailing_bits_warning():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    msg = make_message(body) + "FF"  # extra byte beyond definition
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": msg},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["trailing_bits"] == 8
    assert data["warnings"]


def _register(device_type, version):
    payload = {
        "device_type": device_type,
        "version": version,
        "fields": [{"name": "val", "type": "uint", "bit_length": 8}],
    }
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_latest_version_and_deactivate():
    _register("meter", "1.9.0")
    _register("meter", "1.10.0")  # semver-ish: 1.10.0 > 1.9.0

    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.10.0"

    resp = client.post("/api/v1/definitions/meter/1.10.0/deactivate")
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    # latest now falls back to the still-active older version
    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.json()["version"] == "1.9.0"

    # explicit-version parse of an inactive definition still works
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "meter", "version": "1.10.0", "hex": "2A"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition_active"] is False
    assert resp.json()["fields"][0]["value"] == 0x2A


def test_list_definitions():
    resp = client.get("/api/v1/definitions")
    assert resp.status_code == 200
    data = resp.json()
    types = {item["device_type"] for item in data["items"]}
    assert {"temp_sensor", "meter"} <= types
    # inactive 1.10.0 hidden by default
    versions = {(i["device_type"], i["version"]) for i in data["items"]}
    assert ("meter", "1.10.0") not in versions

    resp = client.get("/api/v1/definitions", params={"include_inactive": True})
    versions = {(i["device_type"], i["version"]) for i in resp.json()["items"]}
    assert ("meter", "1.10.0") in versions

    resp = client.get("/api/v1/definitions", params={"device_type": "meter"})
    assert all(i["device_type"] == "meter" for i in resp.json()["items"])


def test_get_definition_and_404s():
    resp = client.get("/api/v1/definitions/temp_sensor/1.0.0")
    assert resp.status_code == 200
    assert resp.json()["checksum"]["algorithm"] == "crc16-ccitt"

    assert client.get("/api/v1/definitions/temp_sensor/9.9.9").status_code == 404
    assert client.get("/api/v1/definitions/ghost/latest").status_code == 404
    assert client.post("/api/v1/definitions/ghost/1.0.0/deactivate").status_code == 404
