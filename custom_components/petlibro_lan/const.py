"""Constants."""
from __future__ import annotations

DOMAIN = "petlibro_lan"
NAME = "PetLibro Local"
STORE_VERSION = 1
CLOUD_HOST = "mqtt.us.petlibro.com"

CONF_PORT = "port"
DEFAULT_PORT = 1883

AVAILABILITY_TIMEOUT = 300  # seconds without any message before a device is unavailable
POLL_INTERVAL = 300  # seconds between ATTR_GET_SERVICE requests
PLAN_RESYNC = 86400  # plans are re-sent at least this often
PLAN_RETRY = 60
PLAN_MAX_TRIES = 24
COMMAND_TIMEOUT = 8.0
FILTER_LIFE_DAYS = 30  # next due date written by "Reset filter"; the firmware does not say its own

SIGNAL_NEW_DEVICE = f"{DOMAIN}_new_device"
SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"

SERVICE_FEED = "feed"
SERVICE_SET_PLAN = "set_feeding_plan"
