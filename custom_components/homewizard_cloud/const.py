"""Constants for the HomeWizard Cloud integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "homewizard_cloud"
MANUFACTURER: Final = "HomeWizard"
DOCS_URL: Final = "https://github.com/Emacee/HomeWizard-Cloud---HACS"

CONF_DEVICE_ID: Final = "device_id"
CONF_LOCATION_NAME: Final = "location_name"

# Options
CONF_REALTIME: Final = "realtime"
CONF_THREE_PHASES: Final = "three_phases"
CONF_REALTIME_INTERVAL: Final = "realtime_interval"

DEFAULT_REALTIME: Final = True
DEFAULT_THREE_PHASES: Final = True
# Seconds between state writes of the second-by-second realtime stream. The
# stream itself always runs at ~1 s; this only limits recorder load.
DEFAULT_REALTIME_INTERVAL: Final = 5
MIN_REALTIME_INTERVAL: Final = 1
MAX_REALTIME_INTERVAL: Final = 60
