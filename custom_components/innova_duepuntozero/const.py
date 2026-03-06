"""Constants for the Innova Duepuntozero integration."""

DOMAIN = "innova_duepuntozero"
CONF_MAC_ADDRESS = "mac_address"

# Options flow key for the fallback polling interval.
CONF_POLL_INTERVAL = "poll_interval"

# Default fallback polling interval in minutes. The event stream handles
# real-time updates; polling is only a safety net for missed events.
DEFAULT_POLL_INTERVAL = 10  # minutes
MIN_POLL_INTERVAL     = 1   # minutes
MAX_POLL_INTERVAL     = 60  # minutes

# Sentinel value meaning "disable fallback polling entirely".
POLL_INTERVAL_DISABLED = 0
