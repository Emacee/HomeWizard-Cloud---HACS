"""Connectivity binary sensors for the HomeWizard Cloud P1 meter."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HomeWizardCloudConfigEntry, HomeWizardCloudCoordinator
from .entity import HomeWizardCloudEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeWizardCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the connectivity sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [CloudConnectionSensor(coordinator), MeterOnlineSensor(coordinator)]
    )


class CloudConnectionSensor(HomeWizardCloudEntity, BinarySensorEntity):
    """Whether Home Assistant is connected to the cloud state stream."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "cloud_connection"

    def __init__(self, coordinator: HomeWizardCloudCoordinator) -> None:
        super().__init__(coordinator, "cloud_connection")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.state_connected


class MeterOnlineSensor(HomeWizardCloudEntity, BinarySensorEntity):
    """Whether the P1 meter itself is online according to the cloud."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "meter_online"

    def __init__(self, coordinator: HomeWizardCloudCoordinator) -> None:
        super().__init__(coordinator, "meter_online")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data.state_available
            and isinstance(self.coordinator.data.device.get("online"), bool)
        )

    @property
    def is_on(self) -> bool | None:
        online = self.coordinator.data.device.get("online")
        return online if isinstance(online, bool) else None
