"""Home Assistant tests for the HomeWizard Cloud integration.

Run with the Home Assistant test harness installed:

    pip install pytest-homeassistant-custom-component
    pytest tests/ha -o asyncio_mode=auto
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homewizard_cloud import api as hwc_api
from custom_components.homewizard_cloud.api import (
    AuthenticationError,
    CannotConnect,
    HomeWizardCloudClient,
)
from custom_components.homewizard_cloud.const import (
    CONF_DEVICE_ID,
    CONF_LOCATION_NAME,
    CONF_REALTIME,
    CONF_REALTIME_INTERVAL,
    CONF_THREE_PHASES,
    DOMAIN,
)
from custom_components.homewizard_cloud.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.homewizard_cloud.protocol import CloudDevice, RealtimeMeasurement

DEVICE_ID = "p1dongle/5c2faf3c6a8a"
DEVICE = CloudDevice(DEVICE_ID, "P1 meter", "p1dongle", "1", "Huis oma")
OTHER = CloudDevice("p1dongle/0011", "P1 boot", "p1dongle", "2", "Boot")

STATE_MESSAGE: dict[str, Any] = {
    "type": "p1dongle",
    "device": DEVICE_ID,
    "online": True,
    "wifi_strength": 78,
    "state": {
        "smr_version": 50,
        "meter_model": "ISKRA 2M550T-101",
        "active_tariff": 2,
        "active_power_w": 1234,
        "active_power_l1_w": 400,
        "active_power_l2_w": 500,
        "active_power_l3_w": 334,
        "active_voltage_l1_v": 231.2,
        "active_current_l1_a": 1.8,
        "total_power_import_t1_kwh": 1000.5,
        "total_power_import_t2_kwh": 2000.25,
        "total_power_export_t1_kwh": 10.0,
        "total_power_export_t2_kwh": 20.0,
        "montly_power_peak_w": 4500,
        "montly_power_peak_timestamp": "261103181500",
        "total_gas_m3": 1.0,
        "external": [
            {
                "unique_id": "4730303339303031",
                "type": "gas_meter",
                "timestamp": 261106100000,
                "value": 1234.567,
                "unit": "m3",
            }
        ],
    },
}

ENTRY_DATA = {
    CONF_EMAIL: "maurits@example.com",
    CONF_PASSWORD: "secret",
    CONF_DEVICE_ID: DEVICE_ID,
    CONF_LOCATION_NAME: "Huis oma",
}


# --------------------------------------------------------------- config flow


async def test_user_flow_single_device(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    with (
        patch(
            "custom_components.homewizard_cloud.config_flow.async_validate",
            return_value=[DEVICE],
        ),
        patch(
            "custom_components.homewizard_cloud.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "maurits@example.com", CONF_PASSWORD: "secret"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "P1 meter (Huis oma)"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == DEVICE_ID


async def test_user_flow_pick_device(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.homewizard_cloud.config_flow.async_validate",
        return_value=[DEVICE, OTHER],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "a@b.nl", CONF_PASSWORD: "x"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device"
    with patch(
        "custom_components.homewizard_cloud.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE_ID: OTHER.device_id}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == OTHER.device_id
    assert result["title"] == "P1 boot (Boot)"


@pytest.mark.parametrize(
    ("side_effect", "returned", "error"),
    [
        (AuthenticationError("x"), None, "invalid_auth"),
        (CannotConnect("x"), None, "cannot_connect"),
        (RuntimeError("x"), None, "unknown"),
        (None, [], "no_devices"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, side_effect, returned, error
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.homewizard_cloud.config_flow.async_validate",
        side_effect=side_effect,
        return_value=returned,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "a@b.nl", CONF_PASSWORD: "x"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_user_flow_already_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, data=ENTRY_DATA).add_to_hass(
        hass
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.homewizard_cloud.config_flow.async_validate",
        return_value=[DEVICE],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "a@b.nl", CONF_PASSWORD: "x"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ------------------------------------------------------------------- set-up


class FakeStreams:
    """Replaces the WebSocket loops of the client."""

    def __init__(self) -> None:
        self.on_state = None
        self.on_state_connection = None
        self.on_realtime = None
        self.on_realtime_connection = None
        self.three_phases = None
        self.realtime_started = False
        self.auth_fail = False

    async def run_state_stream(self, client, device_id, on_state, on_connection):
        self.on_state = on_state
        self.on_state_connection = on_connection
        if self.auth_fail:
            raise AuthenticationError("revoked")
        on_connection(True)
        on_state(json.loads(json.dumps(STATE_MESSAGE)))
        await asyncio.Event().wait()

    async def run_realtime_stream(
        self, client, device_id, three_phases, on_measurement, on_connection
    ):
        self.realtime_started = True
        self.three_phases = three_phases
        self.on_realtime = on_measurement
        self.on_realtime_connection = on_connection
        await asyncio.Event().wait()


@pytest.fixture
def streams():
    fake = FakeStreams()

    async def _auth(self):
        return "token"

    async def _state(self, *args):
        return await fake.run_state_stream(self, *args)

    async def _realtime(self, *args):
        return await fake.run_realtime_stream(self, *args)

    with (
        patch.object(HomeWizardCloudClient, "async_authenticate", _auth),
        patch.object(HomeWizardCloudClient, "run_state_stream", _state),
        patch.object(HomeWizardCloudClient, "run_realtime_stream", _realtime),
    ):
        yield fake


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="P1 meter (Huis oma)",
        data=ENTRY_DATA,
        options=options or {CONF_REALTIME_INTERVAL: 1},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_creates_sensors(hass: HomeAssistant, streams: FakeStreams) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.p1_meter_huis_oma_power").state == "1234.0"
    assert hass.states.get("sensor.p1_meter_huis_oma_power_phase_2").state == "500.0"
    assert hass.states.get("sensor.p1_meter_huis_oma_voltage_phase_1").state == "231.2"
    total = hass.states.get("sensor.p1_meter_huis_oma_energy_import")
    assert float(total.state) == pytest.approx(3000.75)
    assert total.attributes["state_class"] == "total_increasing"
    assert total.attributes["unit_of_measurement"] == "kWh"
    assert (
        float(hass.states.get("sensor.p1_meter_huis_oma_energy_export").state) == 30.0
    )
    assert hass.states.get("sensor.p1_meter_huis_oma_tariff").state == "2"
    gas = hass.states.get("sensor.p1_meter_huis_oma_gas")
    assert float(gas.state) == pytest.approx(1234.567)
    assert gas.attributes["device_class"] == "gas"
    # The legacy total_gas_m3 is hidden when an external gas meter exists.
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "5c2faf3c6a8a_total_gas_m3")
        is None
    )
    assert (
        hass.states.get("binary_sensor.p1_meter_huis_oma_cloud_connection").state
        == "on"
    )
    assert (
        hass.states.get("binary_sensor.p1_meter_huis_oma_p1_meter_online").state == "on"
    )
    assert streams.realtime_started and streams.three_phases is True


async def test_patch_updates_and_new_keys(
    hass: HomeAssistant, streams: FakeStreams
) -> None:
    await _setup(hass)
    message = json.loads(json.dumps(STATE_MESSAGE))
    message["state"]["active_power_w"] = -800
    message["state"]["active_frequency_hz"] = 50.01
    streams.on_state(message)
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.p1_meter_huis_oma_power").state == "-800.0"
    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, "5c2faf3c6a8a_active_frequency_hz"
    )


async def test_realtime_sensors(hass: HomeAssistant, streams: FakeStreams) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.p1_meter_huis_oma_power_realtime") is None
    streams.on_realtime_connection(True)
    streams.on_realtime(RealtimeMeasurement(None, 777.0, [100.0, 200.0, 477.0]))
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.p1_meter_huis_oma_power_realtime").state == "777.0"
    assert (
        hass.states.get("sensor.p1_meter_huis_oma_power_phase_3_realtime").state
        == "477.0"
    )


async def test_realtime_disabled(hass: HomeAssistant, streams: FakeStreams) -> None:
    await _setup(hass, {CONF_REALTIME: False, CONF_THREE_PHASES: False})
    assert not streams.realtime_started


async def test_offline_meter_marks_state_unavailable(
    hass: HomeAssistant, streams: FakeStreams
) -> None:
    await _setup(hass)
    message = json.loads(json.dumps(STATE_MESSAGE))
    message["online"] = False
    streams.on_state(message)
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.p1_meter_huis_oma_power").state == STATE_UNAVAILABLE
    assert (
        hass.states.get("binary_sensor.p1_meter_huis_oma_p1_meter_online").state
        == "off"
    )


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, streams: FakeStreams
) -> None:
    streams.auth_fail = True
    entry = await _setup(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)

    flow = next(f for f in flows if f["context"]["source"] == "reauth")
    with patch(
        "custom_components.homewizard_cloud.config_flow.async_validate",
        return_value=[DEVICE],
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_EMAIL: "maurits@example.com", CONF_PASSWORD: "new"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_setup_login_rejected(hass: HomeAssistant) -> None:
    async def _auth(self):
        raise AuthenticationError("no")

    with patch.object(HomeWizardCloudClient, "async_authenticate", _auth):
        entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, data=ENTRY_DATA)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_cloud_down(hass: HomeAssistant) -> None:
    async def _auth(self):
        raise CannotConnect("down")

    with patch.object(HomeWizardCloudClient, "async_authenticate", _auth):
        entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, data=ENTRY_DATA)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_options_flow_and_unload(
    hass: HomeAssistant, streams: FakeStreams
) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_REALTIME: False, CONF_THREE_PHASES: False, CONF_REALTIME_INTERVAL: 10.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {
        CONF_REALTIME: False,
        CONF_THREE_PHASES: False,
        CONF_REALTIME_INTERVAL: 10,
    }
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_redacts(hass: HomeAssistant, streams: FakeStreams) -> None:
    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)
    assert "secret" not in text
    assert "maurits@example.com" not in text
    assert "5c2faf3c6a8a" not in text
    assert diag["device_message"]["state"]["active_power_w"] == 1234


# --------------------------------------------------------- client vs fake cloud


@pytest.fixture
async def fake_cloud(socket_enabled):
    """A local aiohttp server that speaks the cloud protocol."""
    seen: dict[str, Any] = {"hello": [], "subscribe": [], "realtime": [], "logins": 0}
    state = {"reject_hello": 0, "password": "secret"}

    async def token(request: web.Request) -> web.Response:
        seen["logins"] += 1
        auth = aiohttp.BasicAuth.decode(request.headers["Authorization"])
        if auth.password != state["password"]:
            return web.json_response({"error": "invalid"}, status=401)
        return web.json_response(
            {"access_token": f"tok{seen['logins']}", "expires_in": 3600}
        )

    async def locations(request: web.Request) -> web.Response:
        assert request.headers["Authorization"].startswith("Bearer tok")
        return web.json_response(
            [
                {
                    "id": 1,
                    "name": "Huis oma",
                    "devices": [
                        {"type": "p1dongle", "device_id": DEVICE_ID, "name": "P1"}
                    ],
                }
            ]
        )

    async def main_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json()
        seen["hello"].append(hello)
        if state["reject_hello"]:
            state["reject_hello"] -= 1
            await ws.send_json({"type": "response", "status": 401})
            await ws.close()
            return ws
        await ws.send_json(
            {"type": "response", "status": 200, "message_id": hello["message_id"]}
        )
        sub = await ws.receive_json()
        seen["subscribe"].append(sub)
        await ws.send_json(STATE_MESSAGE)
        await ws.send_json(
            {
                "type": "json_patch",
                "device": DEVICE_ID,
                "patch": [
                    {"op": "replace", "path": "/state/active_power_w", "value": -42}
                ],
            }
        )
        await ws.send_json(
            {"type": "json_patch", "device": "p1dongle/other", "patch": []}
        )
        await ws.close()
        return ws

    async def realtime_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        seen["realtime"].append(await ws.receive_json())
        await ws.send_json(
            {"time": 1762425600000, "wattage": 321, "wattages": [1, 2, 318]}
        )
        await ws.send_json({"hello": "ignored"})
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/token", token)
    app.router.add_get("/locations", locations)
    app.router.add_get("/ws", main_ws)
    app.router.add_get("/rt", realtime_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (
        patch.object(hwc_api, "AUTH_URL", f"{base}/token"),
        patch.object(hwc_api, "LOCATIONS_URL", f"{base}/locations"),
        patch.object(hwc_api, "MAIN_WS_URL", f"{base}/ws"),
        patch.object(hwc_api, "REALTIME_WS_URL", f"{base}/rt"),
        patch.object(hwc_api, "BACKOFF_MIN", 0),
    ):
        yield seen, state
    await runner.cleanup()


async def test_client_against_fake_cloud(fake_cloud) -> None:
    seen, state = fake_cloud
    async with aiohttp.ClientSession() as session:
        devices = await hwc_api.async_validate(session, "a@b.nl", "secret")
        assert [d.device_id for d in devices] == [DEVICE_ID]

        client = HomeWizardCloudClient(session, "a@b.nl", "secret")
        states: list[int] = []
        connection: list[bool] = []
        state["reject_hello"] = 1

        def on_state(doc: dict[str, Any]) -> None:
            states.append(doc["state"]["active_power_w"])

        task = asyncio.create_task(
            client.run_state_stream(DEVICE_ID, on_state, connection.append)
        )
        for _ in range(100):
            if len(states) >= 2:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert states[:2] == [1234, -42]
        # First hello was rejected → fresh token for the second attempt.
        assert seen["hello"][0]["token"] != seen["hello"][1]["token"]
        assert seen["subscribe"][0] == {
            "type": "subscribe_device",
            "device": DEVICE_ID,
            "message_id": seen["subscribe"][0]["message_id"],
        }
        assert True in connection

        measurements: list[RealtimeMeasurement] = []
        rt_task = asyncio.create_task(
            client.run_realtime_stream(
                DEVICE_ID, True, measurements.append, lambda _c: None
            )
        )
        for _ in range(100):
            if measurements:
                break
            await asyncio.sleep(0.05)
        rt_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rt_task
        assert measurements[0].power == 321
        assert measurements[0].phases == [1, 2, 318]
        assert seen["realtime"][0]["three_phases"] is True


async def test_client_bad_password(fake_cloud) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AuthenticationError):
            await hwc_api.async_validate(session, "a@b.nl", "wrong")
