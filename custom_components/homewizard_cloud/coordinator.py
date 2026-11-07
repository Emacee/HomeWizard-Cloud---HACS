"""Push coordinator: keeps the cloud streams running and fans out updates."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthenticationError, CannotConnect, HomeWizardCloudClient
from .const import (
    CONF_DEVICE_ID,
    CONF_REALTIME,
    CONF_REALTIME_INTERVAL,
    CONF_THREE_PHASES,
    DEFAULT_REALTIME,
    DEFAULT_REALTIME_INTERVAL,
    DEFAULT_THREE_PHASES,
    DOMAIN,
)
from .protocol import RealtimeMeasurement

_LOGGER = logging.getLogger(__name__)

# Entities stay available through short reconnects.
AVAILABILITY_GRACE = 180
# How long setup waits for the first full device state before continuing.
FIRST_STATE_TIMEOUT = 30


@dataclass
class CloudData:
    """Snapshot handed to the entities."""

    device: dict[str, Any] = field(default_factory=dict)
    realtime: RealtimeMeasurement | None = None
    state_available: bool = False
    realtime_available: bool = False
    state_connected: bool = False
    last_state_update: float | None = None


type HomeWizardCloudConfigEntry = ConfigEntry[HomeWizardCloudCoordinator]


class HomeWizardCloudCoordinator(DataUpdateCoordinator[CloudData]):
    """Owns the client and both WebSocket streams of one P1 meter."""

    config_entry: HomeWizardCloudConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HomeWizardCloudConfigEntry,
        client: HomeWizardCloudClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_DEVICE_ID]}",
            update_interval=None,
        )
        self.client = client
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        options = entry.options
        self.realtime_enabled: bool = options.get(CONF_REALTIME, DEFAULT_REALTIME)
        self.three_phases: bool = options.get(CONF_THREE_PHASES, DEFAULT_THREE_PHASES)
        self.min_interval: float = float(
            options.get(CONF_REALTIME_INTERVAL, DEFAULT_REALTIME_INTERVAL)
        )

        self._device: dict[str, Any] = {}
        self._realtime: RealtimeMeasurement | None = None
        self._state_connected = False
        self._state_lost_at: float | None = time.monotonic()
        self._realtime_connected = False
        self._realtime_lost_at: float | None = time.monotonic()
        self._last_state_update: float | None = None
        self._first_state = asyncio.Event()

        self._last_push = 0.0
        self._pending_push: CALLBACK_TYPE | None = None
        self._availability_check: CALLBACK_TYPE | None = None
        self._tasks: list[asyncio.Task] = []

    # --------------------------------------------------------------- setup

    async def _async_update_data(self) -> CloudData:
        """Initial load only: log in, start the streams, wait for a first state."""
        if self.data is not None:
            return self._snapshot()
        try:
            await self.client.async_authenticate()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            raise UpdateFailed(str(err)) from err

        self._start_streams()
        try:
            await asyncio.wait_for(self._first_state.wait(), FIRST_STATE_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning(
                "No state from the HomeWizard cloud for %s within %s s; "
                "sensors appear as soon as it arrives",
                self.device_id,
                FIRST_STATE_TIMEOUT,
            )
        return self._snapshot()

    def _start_streams(self) -> None:
        if self._tasks:
            return
        entry = self.config_entry
        self._tasks.append(
            entry.async_create_background_task(
                self.hass,
                self._guard(
                    self.client.run_state_stream(
                        self.device_id, self._on_state, self._on_state_connection
                    )
                ),
                f"{DOMAIN}_state_{self.device_id}",
            )
        )
        if self.realtime_enabled:
            self._tasks.append(
                entry.async_create_background_task(
                    self.hass,
                    self._guard(
                        self.client.run_realtime_stream(
                            self.device_id,
                            self.three_phases,
                            self._on_realtime,
                            self._on_realtime_connection,
                        )
                    ),
                    f"{DOMAIN}_realtime_{self.device_id}",
                )
            )

    async def _guard(self, stream) -> None:
        """Turn a credential failure inside a stream into a reauth flow."""
        try:
            await stream
        except AuthenticationError as err:
            _LOGGER.error("HomeWizard cloud login rejected: %s", err)
            self.config_entry.async_start_reauth(self.hass)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("HomeWizard cloud stream stopped unexpectedly")
        finally:
            # Do not keep setup waiting for a state that will never come.
            self._first_state.set()

    async def async_shutdown(self) -> None:
        """Stop streams and timers."""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for unsub in (self._pending_push, self._availability_check):
            if unsub is not None:
                unsub()
        self._pending_push = None
        self._availability_check = None
        await super().async_shutdown()

    # ------------------------------------------------------------ callbacks

    @callback
    def _on_state(self, document: dict[str, Any]) -> None:
        # Deep copy: the client keeps mutating its document with later patches.
        self._device = copy.deepcopy(document)
        self._last_state_update = time.time()
        self._first_state.set()
        self._schedule_push()

    @callback
    def _on_state_connection(self, connected: bool) -> None:
        if connected == self._state_connected:
            return
        self._state_connected = connected
        self._state_lost_at = None if connected else time.monotonic()
        self._schedule_availability_check()
        self._schedule_push(force=True)

    @callback
    def _on_realtime(self, measurement: RealtimeMeasurement) -> None:
        self._realtime = measurement
        self._schedule_push()

    @callback
    def _on_realtime_connection(self, connected: bool) -> None:
        if connected == self._realtime_connected:
            return
        self._realtime_connected = connected
        self._realtime_lost_at = None if connected else time.monotonic()
        self._schedule_availability_check()
        self._schedule_push(force=True)

    # ----------------------------------------------------------- fan-out

    @callback
    def _schedule_push(self, force: bool = False) -> None:
        """Push to entities at most once per ``min_interval`` seconds."""
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_push)
        if force or wait <= 0:
            self._push()
            return
        if self._pending_push is None:
            self._pending_push = async_call_later(self.hass, wait, self._delayed_push)

    @callback
    def _delayed_push(self, _now: Any) -> None:
        self._pending_push = None
        self._push()

    @callback
    def _push(self) -> None:
        if self._pending_push is not None:
            self._pending_push()
            self._pending_push = None
        self._last_push = time.monotonic()
        self.async_set_updated_data(self._snapshot())

    @callback
    def _schedule_availability_check(self) -> None:
        """Re-evaluate availability once the grace period of a drop has passed."""
        if self._availability_check is not None:
            self._availability_check()
            self._availability_check = None
        if self._state_lost_at is None and self._realtime_lost_at is None:
            return

        @callback
        def _check(_now: Any) -> None:
            self._availability_check = None
            self._push()

        self._availability_check = async_call_later(
            self.hass, AVAILABILITY_GRACE + 1, _check
        )

    def _available(
        self, connected: bool, lost_at: float | None, has_data: bool
    ) -> bool:
        if not has_data:
            return False
        if connected or lost_at is None:
            return True
        return time.monotonic() - lost_at < AVAILABILITY_GRACE

    def _snapshot(self) -> CloudData:
        return CloudData(
            device=self._device,
            realtime=self._realtime,
            state_available=self._available(
                self._state_connected, self._state_lost_at, bool(self._device)
            ),
            realtime_available=self._available(
                self._realtime_connected,
                self._realtime_lost_at,
                self._realtime is not None,
            ),
            state_connected=self._state_connected,
            last_state_update=self._last_state_update,
        )
