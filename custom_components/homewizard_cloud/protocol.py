"""HomeWizard Cloud protocol helpers.

Everything in this module is pure Python (no Home Assistant, no aiohttp) so it
can be unit tested without a Home Assistant install. The network client in
``api.py`` uses these helpers to build and interpret messages.

The cloud API is not documented by HomeWizard. It was reverse engineered from
the HomeWizard Energy app by Sven Serlier (homey-homewizard-energy-cloud) and
later implementations (jtebbens/com.homewizard, MauroDruwel/homewizard-cloud):

1. ``GET api.homewizardeasyonline.com/v1/auth/account/token`` with HTTP Basic
   auth (account e-mail and password) returns an ``access_token``.
2. ``GET homes.api.homewizard.com/locations`` lists the homes and their devices.
3. ``wss://energy-app-ws.homewizard.com/ws/`` carries the device state: send a
   ``hello`` (with the token), then ``subscribe_device``. The server answers with
   one full device message (``type`` = device type, e.g. ``p1dongle``) followed
   by ``json_patch`` deltas.
4. ``wss://tsdb-reader.homewizard.com/devices/date/now`` streams the active
   power (and per-phase power) about once per second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

AUTH_URL: Final = (
    "https://api.homewizardeasyonline.com/v1/auth/account/token?include=account"
)
LOCATIONS_URL: Final = "https://homes.api.homewizard.com/locations"
MAIN_WS_URL: Final = "wss://energy-app-ws.homewizard.com/ws/"
REALTIME_WS_URL: Final = "wss://tsdb-reader.homewizard.com/devices/date/now"

# The state WebSocket checks the client identity; the Android energy app's
# identity is known to be accepted.
HELLO_COMPATIBILITY: Final = 5
HELLO_OS: Final = "android"
HELLO_SOURCE: Final = "nl.homewizard.android.energy"
HELLO_VERSION: Final = (
    "nl.homewizard.android.energy/1.25.9(124) Dalvik/2.1.0 "
    "(Linux; U; Android 12; sdk_gphone64_x86_64 Build/SE1A.220826.008)"
)

P1_DEVICE_TYPE: Final = "p1dongle"
STATE_MESSAGE_TYPES: Final = frozenset({"p1dongle", "energysocket", "watermeter"})

# Used when the token response carries no ``expires_in``.
DEFAULT_TOKEN_TTL: Final = 3300
# Refresh this many seconds before the token expires.
TOKEN_RENEW_MARGIN: Final = 300


@dataclass(slots=True)
class CloudDevice:
    """A device in the HomeWizard account."""

    device_id: str
    name: str
    device_type: str
    location_id: str = ""
    location_name: str = ""

    @property
    def serial(self) -> str:
        """Device id without the type prefix (``p1dongle/5c2f…`` → ``5c2f…``)."""
        return self.device_id.rsplit("/", 1)[-1]


@dataclass(slots=True)
class RealtimeMeasurement:
    """One sample from the realtime stream."""

    timestamp: datetime | None
    power: float | None
    phases: list[float | None] = field(default_factory=list)


def token_ttl(response: Any) -> float:
    """Return the lifetime in seconds of a token response."""
    if isinstance(response, dict):
        expires = response.get("expires_in")
        if (
            isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and expires > 0
        ):
            return float(expires)
    return float(DEFAULT_TOKEN_TTL)


def extract_token(response: Any) -> str | None:
    """Return the access token from a token response, if present."""
    if isinstance(response, dict):
        token = response.get("access_token")
        if isinstance(token, str) and token:
            return token
    return None


def parse_locations(
    data: Any, device_type: str | None = P1_DEVICE_TYPE
) -> list[CloudDevice]:
    """Return the devices from a ``/locations`` response.

    ``device_type`` filters on the device type; ``None`` returns every device.
    """
    if not isinstance(data, list):
        raise TypeError(f"Expected a list of locations, got {type(data).__name__}")
    devices: list[CloudDevice] = []
    for location in data:
        if not isinstance(location, dict):
            continue
        location_id = str(location.get("id", ""))
        location_name = str(location.get("name") or "")
        for raw in location.get("devices") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "")
            device_id = str(raw.get("device_id") or raw.get("identifier") or "")
            if not device_id:
                continue
            if device_type is not None and kind != device_type:
                continue
            devices.append(
                CloudDevice(
                    device_id=device_id,
                    name=str(raw.get("name") or "P1 Meter"),
                    device_type=kind,
                    location_id=location_id,
                    location_name=location_name,
                )
            )
    return devices


def build_hello(token: str, message_id: int) -> dict[str, Any]:
    """Return the ``hello`` that authenticates the state WebSocket."""
    return {
        "type": "hello",
        "message_id": message_id,
        "compatibility": HELLO_COMPATIBILITY,
        "os": HELLO_OS,
        "source": HELLO_SOURCE,
        "token": token,
        "version": HELLO_VERSION,
    }


def build_subscribe(device_id: str, message_id: int) -> dict[str, Any]:
    """Return the message that subscribes the state WebSocket to a device."""
    return {"type": "subscribe_device", "device": device_id, "message_id": message_id}


def build_realtime_subscribe(
    token: str, device_id: str, three_phases: bool
) -> dict[str, Any]:
    """Return the message that starts the realtime power stream."""
    return {
        "token": token,
        "type": "main_connection",
        "devices": [{"identifier": device_id, "measurementType": "main_connection"}],
        "three_phases": three_phases,
    }


def hello_accepted(message: Any) -> bool | None:
    """Interpret the first message after ``hello``.

    Returns ``True`` when accepted, ``False`` when rejected and ``None`` when the
    message is not a response to the hello.
    """
    if not isinstance(message, dict) or message.get("type") != "response":
        return None
    return message.get("status") == 200


def is_auth_error(message: Any) -> bool:
    """Return whether a state-stream message says the token is not accepted."""
    if not isinstance(message, dict):
        return False
    kind = message.get("type")
    if kind == "response" and message.get("status") in (401, 403):
        return True
    if kind == "error":
        if message.get("code") in ("unauthorized", "forbidden"):
            return True
        text = str(message.get("message", "")).lower()
        return "authentication" in text or "unauthorized" in text or "token" in text
    return False


def apply_patch(document: dict[str, Any], patch: Any) -> bool:
    """Apply a JSON patch (RFC 6902 subset) to a device message in place.

    The cloud addresses the whole device message, e.g. ``/state/active_power_w``
    or ``/online``. ``replace`` and ``add`` set a value (creating intermediate
    objects), ``remove`` deletes it. List indices are supported. Returns whether
    anything changed.
    """
    changed = False
    if not isinstance(patch, list):
        return False
    for operation in patch:
        if not isinstance(operation, dict):
            continue
        op = operation.get("op")
        if op not in ("replace", "add", "remove"):
            continue
        parts = [
            p.replace("~1", "/").replace("~0", "~")
            for p in str(operation.get("path", "")).split("/")[1:]
        ]
        if not parts or parts == [""]:
            continue
        target: Any = document
        ok = True
        for part in parts[:-1]:
            if isinstance(target, dict):
                if not isinstance(target.get(part), (dict, list)):
                    if op == "remove":
                        ok = False
                        break
                    target[part] = {}
                target = target[part]
            elif (
                isinstance(target, list) and part.isdigit() and int(part) < len(target)
            ):
                target = target[int(part)]
            else:
                ok = False
                break
        if not ok:
            continue
        last = parts[-1]
        if isinstance(target, dict):
            if op == "remove":
                if last in target:
                    del target[last]
                    changed = True
            elif target.get(last, _MISSING) != operation.get("value"):
                target[last] = operation.get("value")
                changed = True
        elif isinstance(target, list):
            if last == "-" and op == "add":
                target.append(operation.get("value"))
                changed = True
            elif last.isdigit():
                index = int(last)
                if op == "remove" and index < len(target):
                    del target[index]
                    changed = True
                elif op == "add" and index <= len(target):
                    target.insert(index, operation.get("value"))
                    changed = True
                elif op == "replace" and index < len(target):
                    target[index] = operation.get("value")
                    changed = True
    return changed


_MISSING = object()


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        # Seconds or milliseconds since the epoch.
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def parse_realtime(message: Any) -> RealtimeMeasurement | None:
    """Return a measurement from a realtime-stream message, or ``None``."""
    if not isinstance(message, dict) or "wattage" not in message:
        return None
    phases_raw = message.get("wattages")
    phases = (
        [_as_float(value) for value in phases_raw]
        if isinstance(phases_raw, list)
        else []
    )
    return RealtimeMeasurement(
        timestamp=_as_datetime(message.get("time")),
        power=_as_float(message.get("wattage")),
        phases=phases,
    )


def parse_p1_timestamp(value: Any) -> datetime | None:
    """Parse a P1 telegram timestamp (``YYMMDDhhmmss``, local meter time).

    The meter clock is Dutch local time; the returned datetime is naive and is
    localised by the caller. ISO strings and epoch numbers are accepted too.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 1e11 and value < 1e12 and len(str(int(value))) == 12:
            value = str(int(value))
        else:
            return _as_datetime(value)
    if not isinstance(value, str):
        return None
    if len(value) == 12 and value.isdigit():
        try:
            # Naive on purpose: the meter clock is local time, see docstring.
            return datetime.strptime(value, "%y%m%d%H%M%S")  # noqa: DTZ007
        except ValueError:
            return None
    return _as_datetime(value)


def p1_state(message: Any) -> dict[str, Any]:
    """Return the ``state`` object of a device message (empty when missing)."""
    if isinstance(message, dict) and isinstance(message.get("state"), dict):
        return message["state"]
    return {}


def external_meters(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the external (M-Bus) meters reported by the P1 meter."""
    meters = state.get("external")
    if not isinstance(meters, list):
        return []
    return [
        meter
        for meter in meters
        if isinstance(meter, dict) and meter.get("unique_id") and meter.get("type")
    ]


def sum_tariffs(state: dict[str, Any], direction: str) -> float | None:
    """Return total import/export energy, summing tariff registers if needed.

    ``direction`` is ``import`` or ``export``.
    """
    total = _as_float(state.get(f"total_power_{direction}_kwh"))
    if total is not None:
        return total
    values = [
        _as_float(state.get(f"total_power_{direction}_t{tariff}_kwh"))
        for tariff in range(1, 5)
    ]
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present), 3)
