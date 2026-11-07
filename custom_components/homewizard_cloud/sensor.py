"""Sensors for the HomeWizard Cloud P1 meter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import (
    CloudData,
    HomeWizardCloudConfigEntry,
    HomeWizardCloudCoordinator,
)
from .entity import HomeWizardCloudEntity
from .protocol import external_meters, p1_state, parse_p1_timestamp, sum_tariffs

PARALLEL_UPDATES = 0

PHASES = (1, 2, 3)
TARIFFS = (1, 2, 3, 4)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, kw_only=True)
class HWCloudSensorDescription(SensorEntityDescription):
    """Describes a sensor read from the device state."""

    present_fn: Callable[[CloudData], bool]
    value_fn: Callable[[CloudData], Any]


def _state_key(key: str, **kwargs: Any) -> HWCloudSensorDescription:
    """Sensor that maps one key of the P1 state one-to-one."""
    return HWCloudSensorDescription(
        key=key,
        present_fn=lambda data: key in p1_state(data.device),
        value_fn=lambda data: _number(p1_state(data.device).get(key)),
        **kwargs,
    )


def _has_external_gas(data: CloudData) -> bool:
    return any(
        m.get("type") == "gas_meter" for m in external_meters(p1_state(data.device))
    )


def _total_present(direction: str) -> Callable[[CloudData], bool]:
    def _present(data: CloudData) -> bool:
        state = p1_state(data.device)
        return any(
            key in state
            for key in (
                f"total_power_{direction}_kwh",
                *(f"total_power_{direction}_t{t}_kwh" for t in TARIFFS),
            )
        )

    return _present


def _tariff(data: CloudData) -> str | None:
    value = p1_state(data.device).get("active_tariff")
    if value is None or isinstance(value, bool):
        return None
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return None


def _peak_timestamp(data: CloudData) -> datetime | None:
    parsed = parse_p1_timestamp(
        p1_state(data.device).get("montly_power_peak_timestamp")
    )
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return parsed


def _wifi_strength(data: CloudData) -> float | None:
    value = data.device.get("wifi_strength")
    if value is None:
        value = p1_state(data.device).get("wifi_strength")
    return _number(value)


def _text(key: str) -> Callable[[CloudData], str | None]:
    def _value(data: CloudData) -> str | None:
        value = p1_state(data.device).get(key)
        return None if value is None else str(value)

    return _value


def _realtime_phase(phase: int) -> Callable[[CloudData], float | None]:
    def _value(data: CloudData) -> float | None:
        if data.realtime is None or len(data.realtime.phases) < phase:
            return None
        return data.realtime.phases[phase - 1]

    return _value


POWER = {
    "device_class": SensorDeviceClass.POWER,
    "native_unit_of_measurement": UnitOfPower.WATT,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 0,
}
ENERGY = {
    "device_class": SensorDeviceClass.ENERGY,
    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    "state_class": SensorStateClass.TOTAL_INCREASING,
    "suggested_display_precision": 3,
}
VOLTAGE = {
    "device_class": SensorDeviceClass.VOLTAGE,
    "native_unit_of_measurement": UnitOfElectricPotential.VOLT,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 1,
}
CURRENT = {
    "device_class": SensorDeviceClass.CURRENT,
    "native_unit_of_measurement": UnitOfElectricCurrent.AMPERE,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 2,
}
COUNTER = {
    "entity_category": EntityCategory.DIAGNOSTIC,
    "entity_registry_enabled_default": False,
}


STATE_SENSORS: tuple[HWCloudSensorDescription, ...] = (
    _state_key("active_power_w", translation_key="active_power", **POWER),
    *(
        _state_key(
            f"active_power_l{p}_w",
            translation_key="active_phase_power",
            translation_placeholders={"phase": str(p)},
            **POWER,
        )
        for p in PHASES
    ),
    _state_key("active_voltage_v", translation_key="active_voltage", **VOLTAGE),
    *(
        _state_key(
            f"active_voltage_l{p}_v",
            translation_key="active_phase_voltage",
            translation_placeholders={"phase": str(p)},
            **VOLTAGE,
        )
        for p in PHASES
    ),
    _state_key("active_current_a", translation_key="active_current", **CURRENT),
    *(
        _state_key(
            f"active_current_l{p}_a",
            translation_key="active_phase_current",
            translation_placeholders={"phase": str(p)},
            **CURRENT,
        )
        for p in PHASES
    ),
    _state_key(
        "active_frequency_hz",
        translation_key="active_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
    ),
    HWCloudSensorDescription(
        key="total_power_import_kwh",
        translation_key="total_energy_import",
        present_fn=_total_present("import"),
        value_fn=lambda data: sum_tariffs(p1_state(data.device), "import"),
        **ENERGY,
    ),
    *(
        _state_key(
            f"total_power_import_t{t}_kwh",
            translation_key="total_energy_import_tariff",
            translation_placeholders={"tariff": str(t)},
            entity_registry_enabled_default=t <= 2,
            **ENERGY,
        )
        for t in TARIFFS
    ),
    HWCloudSensorDescription(
        key="total_power_export_kwh",
        translation_key="total_energy_export",
        present_fn=_total_present("export"),
        value_fn=lambda data: sum_tariffs(p1_state(data.device), "export"),
        **ENERGY,
    ),
    *(
        _state_key(
            f"total_power_export_t{t}_kwh",
            translation_key="total_energy_export_tariff",
            translation_placeholders={"tariff": str(t)},
            entity_registry_enabled_default=t <= 2,
            **ENERGY,
        )
        for t in TARIFFS
    ),
    HWCloudSensorDescription(
        key="active_tariff",
        translation_key="active_tariff",
        device_class=SensorDeviceClass.ENUM,
        options=[str(t) for t in TARIFFS],
        present_fn=lambda data: "active_tariff" in p1_state(data.device),
        value_fn=_tariff,
    ),
    _state_key(
        "active_power_average_w",
        translation_key="active_power_average",
        **POWER,
    ),
    _state_key(
        "montly_power_peak_w",
        translation_key="monthly_power_peak",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    HWCloudSensorDescription(
        key="montly_power_peak_timestamp",
        translation_key="monthly_power_peak_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        present_fn=lambda data: "montly_power_peak_timestamp" in p1_state(data.device),
        value_fn=_peak_timestamp,
    ),
    HWCloudSensorDescription(
        key="total_gas_m3",
        translation_key="total_gas",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        present_fn=lambda data: (
            "total_gas_m3" in p1_state(data.device) and not _has_external_gas(data)
        ),
        value_fn=lambda data: _number(p1_state(data.device).get("total_gas_m3")),
    ),
    *(
        _state_key(
            f"voltage_sag_l{p}_count",
            translation_key="voltage_sag_phase_count",
            translation_placeholders={"phase": str(p)},
            **COUNTER,
        )
        for p in PHASES
    ),
    *(
        _state_key(
            f"voltage_swell_l{p}_count",
            translation_key="voltage_swell_phase_count",
            translation_placeholders={"phase": str(p)},
            **COUNTER,
        )
        for p in PHASES
    ),
    _state_key(
        "any_power_fail_count", translation_key="any_power_fail_count", **COUNTER
    ),
    _state_key(
        "long_power_fail_count", translation_key="long_power_fail_count", **COUNTER
    ),
    HWCloudSensorDescription(
        key="wifi_strength",
        translation_key="wifi_strength",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        present_fn=lambda data: (
            "wifi_strength" in data.device or "wifi_strength" in p1_state(data.device)
        ),
        value_fn=_wifi_strength,
    ),
    HWCloudSensorDescription(
        key="smr_version",
        translation_key="dsmr_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        present_fn=lambda data: "smr_version" in p1_state(data.device),
        value_fn=_text("smr_version"),
    ),
    HWCloudSensorDescription(
        key="meter_model",
        translation_key="meter_model",
        entity_category=EntityCategory.DIAGNOSTIC,
        present_fn=lambda data: "meter_model" in p1_state(data.device),
        value_fn=_text("meter_model"),
    ),
)


REALTIME_SENSORS: tuple[HWCloudSensorDescription, ...] = (
    HWCloudSensorDescription(
        key="realtime_power_w",
        translation_key="realtime_power",
        present_fn=lambda data: data.realtime is not None,
        value_fn=lambda data: data.realtime.power if data.realtime else None,
        **POWER,
    ),
    *(
        HWCloudSensorDescription(
            key=f"realtime_power_l{p}_w",
            translation_key="realtime_phase_power",
            translation_placeholders={"phase": str(p)},
            present_fn=lambda data, p=p: (
                data.realtime is not None and len(data.realtime.phases) >= p
            ),
            value_fn=_realtime_phase(p),
            **POWER,
        )
        for p in PHASES
    ),
)


EXTERNAL_TYPES: dict[str, dict[str, Any]] = {
    "gas_meter": {
        "device_class": SensorDeviceClass.GAS,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "water_meter": {
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "warm_water_meter": {
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "heat_meter": {
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "inlet_heat_meter": {
        "device_class": SensorDeviceClass.VOLUME,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
}

EXTERNAL_UNITS: dict[str, str] = {
    "m3": UnitOfVolume.CUBIC_METERS,
    "m³": UnitOfVolume.CUBIC_METERS,
    "gj": UnitOfEnergy.GIGA_JOULE,
    "kwh": UnitOfEnergy.KILO_WATT_HOUR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeWizardCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors as soon as their value shows up in the stream."""
    coordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        data = coordinator.data
        if data is None:
            return
        new: list[SensorEntity] = []
        for description in STATE_SENSORS:
            if description.key not in added and description.present_fn(data):
                added.add(description.key)
                new.append(HWCloudStateSensor(coordinator, description))
        if coordinator.realtime_enabled:
            for description in REALTIME_SENSORS:
                if description.key not in added and description.present_fn(data):
                    added.add(description.key)
                    new.append(HWCloudRealtimeSensor(coordinator, description))
        for meter in external_meters(p1_state(data.device)):
            key = f"external_{meter['unique_id']}"
            if key not in added:
                added.add(key)
                new.append(HWCloudExternalSensor(coordinator, meter))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class _HWCloudDescribedSensor(HomeWizardCloudEntity, SensorEntity):
    """Sensor whose value comes from a description."""

    entity_description: HWCloudSensorDescription

    def __init__(
        self,
        coordinator: HomeWizardCloudCoordinator,
        description: HWCloudSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)


class HWCloudStateSensor(_HWCloudDescribedSensor):
    """Sensor fed by the state stream."""

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data.state_available
            and self.coordinator.data.device.get("online", True) is not False
        )


class HWCloudRealtimeSensor(_HWCloudDescribedSensor):
    """Sensor fed by the second-by-second realtime stream."""

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.realtime_available


class HWCloudExternalSensor(HomeWizardCloudEntity, SensorEntity):
    """External (M-Bus) meter reported by the P1 meter, e.g. gas."""

    def __init__(
        self, coordinator: HomeWizardCloudCoordinator, meter: dict[str, Any]
    ) -> None:
        self._meter_id = str(meter["unique_id"])
        super().__init__(coordinator, f"external_{self._meter_id}")
        meter_type = str(meter.get("type"))
        attrs = EXTERNAL_TYPES.get(
            meter_type, {"state_class": SensorStateClass.TOTAL_INCREASING}
        )
        self._attr_translation_key = (
            f"external_{meter_type}"
            if meter_type in EXTERNAL_TYPES
            else "external_other"
        )
        self._attr_device_class = attrs.get("device_class")
        self._attr_state_class = attrs.get("state_class")
        unit = str(meter.get("unit") or "").lower()
        self._attr_native_unit_of_measurement = EXTERNAL_UNITS.get(
            unit, meter.get("unit")
        )
        self._attr_suggested_display_precision = 3

    def _meter(self) -> dict[str, Any] | None:
        for meter in external_meters(p1_state(self.coordinator.data.device)):
            if str(meter.get("unique_id")) == self._meter_id:
                return meter
        return None

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data.state_available
            and self._meter() is not None
        )

    @property
    def native_value(self) -> float | None:
        meter = self._meter()
        return _number(meter.get("value")) if meter else None
