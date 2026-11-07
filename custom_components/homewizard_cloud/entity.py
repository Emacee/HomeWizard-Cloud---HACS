"""Shared entity base for HomeWizard Cloud."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOCS_URL, DOMAIN, MANUFACTURER
from .coordinator import HomeWizardCloudCoordinator


class HomeWizardCloudEntity(CoordinatorEntity[HomeWizardCloudCoordinator]):
    """Base entity: one device per P1 meter."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HomeWizardCloudCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device_id = coordinator.device_id
        serial = device_id.rsplit("/", 1)[-1]
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model="Wi-Fi P1 Meter (cloud)",
            serial_number=serial,
            configuration_url=DOCS_URL,
        )
