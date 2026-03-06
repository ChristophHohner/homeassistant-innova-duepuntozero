"""Config flow for Innova Duepuntozero integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .api import InnovaApiError, InnovaClient
from .const import (
    CONF_MAC_ADDRESS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    POLL_INTERVAL_DISABLED,
)

_LOGGER = logging.getLogger(__name__)

# We use our own key names (not CONF_EMAIL / CONF_PASSWORD from HA) so that
# the labels and descriptions in strings.json / translations/*.json are picked
# up by the UI instead of being overridden by HA's built-in translations.
CONF_EMAIL_KEY    = "innova_email"
CONF_PASSWORD_KEY = "innova_password"
CONF_MAC_KEY      = "innova_mac_address"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL_KEY): str,
        vol.Required(CONF_PASSWORD_KEY): str,
        vol.Required(CONF_MAC_KEY): str,
    }
)


class InnovaDuepuntozeroConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Innova Duepuntozero."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return InnovaDuepuntozeroOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = InnovaClient(
                email=user_input[CONF_EMAIL_KEY],
                password=user_input[CONF_PASSWORD_KEY],
                mac_address=user_input[CONF_MAC_KEY],
            )
            try:
                await client.async_ensure_logged_in()
            except InnovaApiError:
                _LOGGER.debug("Cannot connect to Innova API", exc_info=True)
                errors["base"] = "cannot_connect"
            except aiohttp.ClientError:
                _LOGGER.debug("Network error during Innova setup", exc_info=True)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Innova Duepuntozero setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_MAC_KEY])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Innova Duepuntozero ({user_input[CONF_MAC_KEY]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class InnovaDuepuntozeroOptionsFlow(OptionsFlow):
    """Handle options for Innova Duepuntozero (e.g. polling interval)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._current = config_entry.options

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self._current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=POLL_INTERVAL_DISABLED, max=MAX_POLL_INTERVAL),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
