"""Diagnostics for HomeWizard Cloud.

The full device message is included (credentials and identifiers redacted)
because the cloud API is undocumented: it shows exactly which fields the cloud
sends for a given meter.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID
from .coordinator import HomeWizardCloudConfigEntry

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_DEVICE_ID,
    "device",
    "unique_id",
    "serial",
    "wifi_ssid",
    "token",
    "access_token",
    "title",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomeWizardCloudConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    realtime = asdict(data.realtime) if data and data.realtime else None
    if realtime and realtime.get("timestamp"):
        realtime["timestamp"] = realtime["timestamp"].isoformat()
    return {
        "entry": async_redact_data(
            {"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
        ),
        "realtime_enabled": coordinator.realtime_enabled,
        "three_phases": coordinator.three_phases,
        "state_connected": data.state_connected if data else None,
        "state_available": data.state_available if data else None,
        "realtime_available": data.realtime_available if data else None,
        "last_state_update": data.last_state_update if data else None,
        "device_message": async_redact_data(data.device, TO_REDACT) if data else None,
        "realtime": realtime,
    }
