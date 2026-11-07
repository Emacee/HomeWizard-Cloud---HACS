"""Async client for the (unofficial) HomeWizard Cloud API.

Uses the aiohttp session that Home Assistant provides, so the integration has no
extra requirements. Message building and parsing live in ``protocol.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from .protocol import (
    AUTH_URL,
    LOCATIONS_URL,
    MAIN_WS_URL,
    REALTIME_WS_URL,
    STATE_MESSAGE_TYPES,
    TOKEN_RENEW_MARGIN,
    CloudDevice,
    RealtimeMeasurement,
    apply_patch,
    build_hello,
    build_realtime_subscribe,
    build_subscribe,
    extract_token,
    hello_accepted,
    is_auth_error,
    parse_locations,
    parse_realtime,
    token_ttl,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
HELLO_TIMEOUT = 15
# No state message for this long → resubscribe; still nothing after another
# STALE_GRACE seconds → reconnect. The state stream normally pushes a patch at
# least every ~40 s.
STALE_AFTER = 120
STALE_GRACE = 30
# Realtime stream: reconnect when silent for this long.
REALTIME_STALE_AFTER = 60
BACKOFF_MIN = 10
BACKOFF_MAX = 300
WS_HEARTBEAT = 30

StateCallback = Callable[[dict[str, Any]], None]
RealtimeCallback = Callable[[RealtimeMeasurement], None]
ConnectionCallback = Callable[[bool], None]


class HomeWizardCloudError(Exception):
    """Base error."""


class CannotConnect(HomeWizardCloudError):
    """The cloud could not be reached or answered unexpectedly."""


class AuthenticationError(HomeWizardCloudError):
    """The account credentials were rejected."""


class HomeWizardCloudClient:
    """Client for one HomeWizard account."""

    def __init__(
        self, session: aiohttp.ClientSession, email: str, password: str
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._auth_lock = asyncio.Lock()
        self._message_id = 0

    # ------------------------------------------------------------------ auth

    async def async_authenticate(self) -> str:
        """Fetch a new access token."""
        async with self._auth_lock:
            return await self._authenticate()

    async def _authenticate(self) -> str:
        try:
            async with self._session.get(
                AUTH_URL,
                auth=aiohttp.BasicAuth(self._email, self._password),
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise AuthenticationError(f"Login rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise CannotConnect(f"Login failed (HTTP {resp.status})")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CannotConnect(f"Login request failed: {err}") from err

        token = extract_token(data)
        if token is None:
            raise CannotConnect("No access token in login response")
        self._token = token
        self._token_expires = time.monotonic() + token_ttl(data)
        return token

    async def async_get_token(self) -> str:
        """Return a valid token, refreshing it shortly before it expires."""
        async with self._auth_lock:
            if (
                self._token
                and time.monotonic() < self._token_expires - TOKEN_RENEW_MARGIN
            ):
                return self._token
            return await self._authenticate()

    def invalidate_token(self) -> None:
        """Force a fresh login on the next request."""
        self._token_expires = 0.0

    # ------------------------------------------------------------ http API

    async def async_get_devices(
        self, device_type: str | None = "p1dongle"
    ) -> list[CloudDevice]:
        """Return the devices of the account (P1 dongles by default)."""
        for attempt in range(2):
            token = await self.async_get_token()
            try:
                async with self._session.get(
                    LOCATIONS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        self.invalidate_token()
                        continue
                    if resp.status != 200:
                        raise CannotConnect(
                            f"Locations request failed (HTTP {resp.status})"
                        )
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise CannotConnect(f"Locations request failed: {err}") from err
            try:
                return parse_locations(data, device_type)
            except TypeError as err:
                raise CannotConnect(str(err)) from err
        raise CannotConnect("Locations request kept being rejected")

    # ------------------------------------------------------------- streams

    def _next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def run_state_stream(
        self,
        device_id: str,
        on_state: StateCallback,
        on_connection: ConnectionCallback,
    ) -> None:
        """Keep the device state stream running until cancelled.

        ``on_state`` receives the full device message after every change.
        Raises :class:`AuthenticationError` when the credentials stop working.
        """
        backoff = BACKOFF_MIN
        while True:
            try:
                received = await self._state_session(device_id, on_state, on_connection)
            except AuthenticationError:
                on_connection(False)
                raise
            except (aiohttp.ClientError, TimeoutError, CannotConnect, OSError) as err:
                on_connection(False)
                _LOGGER.debug("State stream for %s dropped: %s", device_id, err)
                received = False
            on_connection(False)
            backoff = BACKOFF_MIN if received else min(backoff * 2, BACKOFF_MAX)
            delay = backoff + random.uniform(0, 3)
            _LOGGER.debug("Reconnecting state stream in %.0f s", delay)
            await asyncio.sleep(delay)

    async def _state_session(
        self,
        device_id: str,
        on_state: StateCallback,
        on_connection: ConnectionCallback,
    ) -> bool:
        """Run one state WebSocket session. Returns whether data was received."""
        received = False
        token = await self.async_get_token()
        async with self._session.ws_connect(MAIN_WS_URL, heartbeat=WS_HEARTBEAT) as ws:
            await ws.send_json(build_hello(token, self._next_message_id()))
            accepted = await self._await_hello(ws)
            if not accepted:
                # Token may have been revoked server side: log in again. A
                # rejected login raises AuthenticationError (→ reauth).
                self.invalidate_token()
                await self.async_get_token()
                _LOGGER.warning(
                    "HomeWizard cloud did not accept the session; retrying with a fresh token"
                )
                return False

            await ws.send_json(build_subscribe(device_id, self._next_message_id()))
            on_connection(True)
            _LOGGER.debug("Subscribed to %s", device_id)

            document: dict[str, Any] | None = None
            resubscribed = False
            while True:
                timeout = STALE_GRACE if resubscribed else STALE_AFTER
                try:
                    msg = await ws.receive(timeout=timeout)
                except TimeoutError:
                    if resubscribed:
                        _LOGGER.debug("State stream stale, reconnecting")
                        return received
                    _LOGGER.debug(
                        "State stream quiet for %s s, resubscribing", STALE_AFTER
                    )
                    await ws.send_json(
                        build_subscribe(device_id, self._next_message_id())
                    )
                    resubscribed = True
                    continue

                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    return received
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue

                if is_auth_error(message):
                    _LOGGER.debug("State stream reported an auth problem: %s", message)
                    self.invalidate_token()
                    await self.async_get_token()
                    return received

                kind = message.get("type")
                if kind in STATE_MESSAGE_TYPES:
                    if message.get("device") not in (None, device_id):
                        continue
                    document = message
                elif kind == "json_patch":
                    if document is None or message.get("device") not in (
                        None,
                        device_id,
                    ):
                        continue
                    if not apply_patch(document, message.get("patch")):
                        resubscribed = False
                        continue
                elif kind == "error":
                    _LOGGER.debug("State stream error message: %s", message)
                    continue
                else:
                    continue

                received = True
                resubscribed = False
                on_state(document)

    async def _await_hello(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        deadline = time.monotonic() + HELLO_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                msg = await ws.receive(timeout=remaining)
            except TimeoutError:
                return False
            if msg.type != aiohttp.WSMsgType.TEXT:
                return False
            try:
                result = hello_accepted(json.loads(msg.data))
            except ValueError:
                continue
            if result is not None:
                return result

    async def run_realtime_stream(
        self,
        device_id: str,
        three_phases: bool,
        on_measurement: RealtimeCallback,
        on_connection: ConnectionCallback,
    ) -> None:
        """Keep the realtime power stream running until cancelled."""
        backoff = BACKOFF_MIN
        while True:
            received = False
            try:
                token = await self.async_get_token()
                async with self._session.ws_connect(
                    REALTIME_WS_URL, heartbeat=WS_HEARTBEAT
                ) as ws:
                    await ws.send_json(
                        build_realtime_subscribe(token, device_id, three_phases)
                    )
                    while True:
                        try:
                            msg = await ws.receive(timeout=REALTIME_STALE_AFTER)
                        except TimeoutError:
                            _LOGGER.debug("Realtime stream silent, reconnecting")
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        try:
                            measurement = parse_realtime(json.loads(msg.data))
                        except ValueError:
                            continue
                        if measurement is None:
                            continue
                        if not received:
                            received = True
                            on_connection(True)
                        on_measurement(measurement)
            except AuthenticationError:
                on_connection(False)
                raise
            except (aiohttp.ClientError, TimeoutError, CannotConnect, OSError) as err:
                _LOGGER.debug("Realtime stream for %s dropped: %s", device_id, err)
            on_connection(False)
            if not received:
                # Possibly an expired token that the stream rejects silently.
                self.invalidate_token()
            backoff = BACKOFF_MIN if received else min(backoff * 2, BACKOFF_MAX)
            await asyncio.sleep(backoff + random.uniform(0, 3))


async def async_validate(
    session: aiohttp.ClientSession, email: str, password: str
) -> list[CloudDevice]:
    """Log in and return the P1 devices (used by the config flow)."""
    client = HomeWizardCloudClient(session, email, password)
    await client.async_authenticate()
    return await client.async_get_devices()


__all__ = [
    "AuthenticationError",
    "CannotConnect",
    "HomeWizardCloudClient",
    "HomeWizardCloudError",
    "async_validate",
]
