"""Config flow for HomeWizard Cloud."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AuthenticationError, CannotConnect, async_validate
from .const import (
    CONF_DEVICE_ID,
    CONF_LOCATION_NAME,
    CONF_REALTIME,
    CONF_REALTIME_INTERVAL,
    CONF_THREE_PHASES,
    DEFAULT_REALTIME,
    DEFAULT_REALTIME_INTERVAL,
    DEFAULT_THREE_PHASES,
    DOMAIN,
    MAX_REALTIME_INTERVAL,
    MIN_REALTIME_INTERVAL,
)
from .protocol import CloudDevice

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


def _title(device: CloudDevice) -> str:
    if device.location_name and device.location_name != device.name:
        return f"{device.name} ({device.location_name})"
    return device.name


class HomeWizardCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in, then pick a P1 meter."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._devices: list[CloudDevice] = []

    async def _validate(
        self, email: str, password: str
    ) -> tuple[list[CloudDevice], str | None]:
        try:
            devices = await async_validate(
                async_get_clientsession(self.hass), email, password
            )
        except AuthenticationError:
            return [], "invalid_auth"
        except CannotConnect:
            return [], "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while logging in to HomeWizard")
            return [], "unknown"
        return devices, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the HomeWizard Energy account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            devices, error = await self._validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                configured = self._async_current_ids(include_ignore=False)
                available = [d for d in devices if d.device_id not in configured]
                if not devices:
                    errors["base"] = "no_devices"
                elif not available:
                    return self.async_abort(reason="already_configured")
                else:
                    self._email = user_input[CONF_EMAIL]
                    self._password = user_input[CONF_PASSWORD]
                    self._devices = available
                    if len(available) == 1:
                        return await self._async_create(available[0])
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of several P1 meters."""
        if user_input is not None:
            for device in self._devices:
                if device.device_id == user_input[CONF_DEVICE_ID]:
                    return await self._async_create(device)

        selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=d.device_id, label=_title(d))
                    for d in self._devices
                ],
                mode=SelectSelectorMode.LIST,
            )
        )
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): selector}),
        )

    async def _async_create(self, device: CloudDevice) -> ConfigFlowResult:
        await self.async_set_unique_id(device.device_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=_title(device),
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_DEVICE_ID: device.device_id,
                CONF_LOCATION_NAME: device.location_name,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Credentials stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            devices, error = await self._validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            elif entry.unique_id not in {d.device_id for d in devices}:
                errors["base"] = "device_not_found"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input or {CONF_EMAIL: entry.data[CONF_EMAIL]}
            ),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return HomeWizardCloudOptionsFlow()


class HomeWizardCloudOptionsFlow(OptionsFlow):
    """Realtime stream settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_REALTIME_INTERVAL] = int(user_input[CONF_REALTIME_INTERVAL])
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REALTIME, default=options.get(CONF_REALTIME, DEFAULT_REALTIME)
                ): BooleanSelector(),
                vol.Required(
                    CONF_THREE_PHASES,
                    default=options.get(CONF_THREE_PHASES, DEFAULT_THREE_PHASES),
                ): BooleanSelector(),
                vol.Required(
                    CONF_REALTIME_INTERVAL,
                    default=options.get(
                        CONF_REALTIME_INTERVAL, DEFAULT_REALTIME_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_REALTIME_INTERVAL,
                        max=MAX_REALTIME_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
