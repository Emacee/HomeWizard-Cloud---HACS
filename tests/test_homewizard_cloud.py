"""Tests for the pure protocol logic of the HomeWizard Cloud integration.

These import ``protocol.py`` directly, so they need no Home Assistant install:

    pytest tests/test_homewizard_cloud.py
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "homewizard_cloud"
    / "protocol.py"
)
_spec = importlib.util.spec_from_file_location("hwc_protocol", _MODULE)
protocol = importlib.util.module_from_spec(_spec)
sys.modules["hwc_protocol"] = protocol
_spec.loader.exec_module(protocol)


LOCATIONS = [
    {
        "id": 1234,
        "name": "Thuis",
        "devices": [
            {
                "type": "p1dongle",
                "device_id": "p1dongle/5c2faf3c6a8a",
                "name": "P1 meter",
            },
            {
                "type": "energysocket",
                "device_id": "energysocket/aabbcc",
                "name": "Wasmachine",
            },
        ],
    },
    {
        "id": 99,
        "name": "Boot",
        "devices": [{"type": "p1dongle", "device_id": "p1dongle/0011", "name": None}],
    },
    "garbage",
    {"id": 5, "name": "Leeg", "devices": None},
]


def test_parse_locations_filters_p1():
    devices = protocol.parse_locations(LOCATIONS)
    assert [d.device_id for d in devices] == ["p1dongle/5c2faf3c6a8a", "p1dongle/0011"]
    assert devices[0].location_name == "Thuis"
    assert devices[0].location_id == "1234"
    assert devices[0].serial == "5c2faf3c6a8a"
    assert devices[1].name == "P1 Meter"


def test_parse_locations_all_types():
    assert len(protocol.parse_locations(LOCATIONS, None)) == 3


def test_parse_locations_rejects_non_list():
    with pytest.raises(TypeError):
        protocol.parse_locations({"error": "nope"})


def test_token_helpers():
    assert protocol.extract_token({"access_token": "abc", "expires_in": 3600}) == "abc"
    assert protocol.extract_token({"access_token": ""}) is None
    assert protocol.extract_token(["abc"]) is None
    assert protocol.token_ttl({"expires_in": 3600}) == 3600
    assert protocol.token_ttl({"expires_in": True}) == protocol.DEFAULT_TOKEN_TTL
    assert protocol.token_ttl({}) == protocol.DEFAULT_TOKEN_TTL


def test_messages():
    hello = protocol.build_hello("tok", 1)
    assert hello["type"] == "hello"
    assert hello["token"] == "tok"
    assert hello["os"] == "android"
    assert protocol.build_subscribe("p1dongle/x", 2) == {
        "type": "subscribe_device",
        "device": "p1dongle/x",
        "message_id": 2,
    }
    rt = protocol.build_realtime_subscribe("tok", "p1dongle/x", True)
    assert rt["devices"][0]["identifier"] == "p1dongle/x"
    assert rt["three_phases"] is True


def test_hello_accepted():
    assert protocol.hello_accepted({"type": "response", "status": 200}) is True
    assert protocol.hello_accepted({"type": "response", "status": 401}) is False
    assert protocol.hello_accepted({"type": "p1dongle"}) is None
    assert protocol.hello_accepted("x") is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"type": "response", "status": 401}, True),
        ({"type": "response", "status": 200}, False),
        ({"type": "error", "message": "Authentication is required"}, True),
        ({"type": "error", "code": "forbidden"}, True),
        ({"type": "error", "message": "Device not found"}, False),
        ({"type": "json_patch"}, False),
        (None, False),
    ],
)
def test_is_auth_error(message, expected):
    assert protocol.is_auth_error(message) is expected


def _document():
    return {
        "type": "p1dongle",
        "device": "p1dongle/x",
        "online": True,
        "state": {
            "active_power_w": 100,
            "external": [{"unique_id": "g", "value": 1.0}],
        },
    }


def test_apply_patch_replace_state_and_top_level():
    doc = _document()
    changed = protocol.apply_patch(
        doc,
        [
            {"op": "replace", "path": "/state/active_power_w", "value": -250},
            {"op": "replace", "path": "/online", "value": False},
        ],
    )
    assert changed
    assert doc["state"]["active_power_w"] == -250
    assert doc["online"] is False


def test_apply_patch_no_change_and_unknown_ops():
    doc = _document()
    assert not protocol.apply_patch(
        doc,
        [
            {"op": "replace", "path": "/state/active_power_w", "value": 100},
            {"op": "move", "from": "/a", "path": "/b"},
            "garbage",
            {"op": "replace", "path": "", "value": 1},
        ],
    )
    assert not protocol.apply_patch(doc, None)


def test_apply_patch_creates_nested_and_lists():
    doc = _document()
    assert protocol.apply_patch(
        doc, [{"op": "add", "path": "/state/new/deep", "value": 1}]
    )
    assert doc["state"]["new"] == {"deep": 1}
    assert protocol.apply_patch(
        doc, [{"op": "replace", "path": "/state/external/0/value", "value": 2.5}]
    )
    assert doc["state"]["external"][0]["value"] == 2.5
    assert protocol.apply_patch(
        doc, [{"op": "add", "path": "/state/external/-", "value": {}}]
    )
    assert len(doc["state"]["external"]) == 2
    assert protocol.apply_patch(doc, [{"op": "remove", "path": "/state/external/1"}])
    assert len(doc["state"]["external"]) == 1
    assert protocol.apply_patch(doc, [{"op": "remove", "path": "/state/new"}])
    assert "new" not in doc["state"]
    # Out-of-range and missing paths are ignored.
    assert not protocol.apply_patch(
        doc, [{"op": "replace", "path": "/state/external/9/value", "value": 1}]
    )
    assert not protocol.apply_patch(doc, [{"op": "remove", "path": "/state/nope/x"}])


def test_apply_patch_escaped_path():
    doc = {"state": {}}
    protocol.apply_patch(doc, [{"op": "add", "path": "/state/a~1b", "value": 1}])
    assert doc["state"] == {"a/b": 1}


def test_parse_realtime():
    m = protocol.parse_realtime(
        {"time": 1762425600000, "wattage": 512, "wattages": [100, None, "412"]}
    )
    assert m.power == 512
    assert m.phases == [100, None, 412]
    assert m.timestamp == datetime(2025, 11, 6, 10, 40, tzinfo=UTC)
    assert protocol.parse_realtime(
        {"time": "2026-11-06T10:00:00Z", "wattage": -3}
    ).timestamp.tzinfo
    assert protocol.parse_realtime({"time": 1}) is None
    assert protocol.parse_realtime("x") is None


def test_parse_p1_timestamp():
    for raw in ("261103181500", 261103181500):
        parsed = protocol.parse_p1_timestamp(raw)
        assert parsed.tzinfo is None  # meter local time, localised by the sensor
        assert parsed.timetuple()[:6] == (2026, 11, 3, 18, 15, 0)
    assert protocol.parse_p1_timestamp("2026-11-03T18:15:00+01:00").hour == 18
    assert protocol.parse_p1_timestamp("not a date") is None
    assert protocol.parse_p1_timestamp(None) is None


def test_sum_tariffs():
    assert protocol.sum_tariffs({"total_power_import_kwh": 10.5}, "import") == 10.5
    assert (
        protocol.sum_tariffs(
            {
                "total_power_import_t1_kwh": 1000.123,
                "total_power_import_t2_kwh": 2000.001,
            },
            "import",
        )
        == 3000.124
    )
    assert protocol.sum_tariffs({"total_power_import_t1_kwh": None}, "import") is None
    assert protocol.sum_tariffs({}, "export") is None


def test_external_meters_and_state():
    state = {
        "external": [
            {"unique_id": "abc", "type": "gas_meter", "value": 1.0, "unit": "m3"},
            {"unique_id": "", "type": "water_meter"},
            "x",
        ]
    }
    assert [m["unique_id"] for m in protocol.external_meters(state)] == ["abc"]
    assert protocol.external_meters({}) == []
    assert protocol.p1_state({"state": state}) is state
    assert protocol.p1_state({}) == {}
