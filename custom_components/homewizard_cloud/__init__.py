"""The HomeWizard Cloud integration.

Reads a HomeWizard Wi-Fi P1 meter through the HomeWizard cloud (the same
WebSocket streams the HomeWizard Energy app uses), for meters that are not on
the same network as Home Assistant. Use the official ``homewizard``
integration whenever the local API is reachable.
"""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HomeWizardCloudClient
from .coordinator import HomeWizardCloudConfigEntry, HomeWizardCloudCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: HomeWizardCloudConfigEntry
) -> bool:
    """Set up one P1 meter from a config entry."""
    client = HomeWizardCloudClient(
        async_get_clientsession(hass), entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
    )
    coordinator = HomeWizardCloudCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HomeWizardCloudConfigEntry
) -> bool:
    """Unload a config entry and stop its streams."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_reload_entry(
    hass: HomeAssistant, entry: HomeWizardCloudConfigEntry
) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
