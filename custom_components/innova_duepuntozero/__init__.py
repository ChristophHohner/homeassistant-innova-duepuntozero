"""Innova Duepuntozero integration."""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceStatus, InnovaApiError, InnovaClient
from .const import (
    CONF_MAC_ADDRESS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    POLL_INTERVAL_DISABLED,
)
from .config_flow import CONF_EMAIL_KEY, CONF_MAC_KEY, CONF_PASSWORD_KEY

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]


class InnovaCoordinator(DataUpdateCoordinator[DeviceStatus]):
    """Coordinator for Innova Duepuntozero.

    Fetches the full device status on startup and after errors, and keeps
    it up to date in real time via SubscribeToDeviceEvents.  Periodic
    polling is kept as a fallback in case the event stream drops.
    """

    def __init__(self, hass: HomeAssistant, client: InnovaClient, entry: ConfigEntry) -> None:
        poll_minutes = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        update_interval = (
            timedelta(minutes=poll_minutes)
            if poll_minutes != POLL_INTERVAL_DISABLED
            else None  # None disables periodic polling in DataUpdateCoordinator
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = client
        self._stop_event = threading.Event()
        self._stream_task: asyncio.Task | None = None

    async def _async_update_data(self) -> DeviceStatus:
        """Ensure login and fetch full device status."""
        try:
            await self.client.async_ensure_logged_in()
            status = await self.client.async_get_device_status()
        except InnovaApiError as err:
            raise UpdateFailed(f"Innova API error: {err}") from err
        if status is None:
            raise UpdateFailed("Device status could not be parsed")
        return status

    def _on_event(self, event_type: int, event_value: bytes) -> None:
        """Called from the streaming thread when an event arrives."""
        if self.data is None:
            return
        updated = self.data.apply_event(event_type, event_value)
        if updated:
            _LOGGER.debug("Event type=%d updated status", event_type)
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, self.data)
        else:
            _LOGGER.debug("Received unknown event type=%d, triggering full poll", event_type)
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self.async_request_refresh())
            )

    def start_streaming(self) -> None:
        """Schedule the background event stream as a fire-and-forget task.

        Must be called from the event loop thread. Does not block.
        """
        self._stop_event.clear()
        self._stream_task = self.hass.async_create_task(
            self._async_stream_loop(),
            name=f"innova_duepuntozero_stream_{id(self)}",
        )

    async def _async_stream_loop(self) -> None:
        """Run the event stream, reconnecting automatically on errors."""
        while not self._stop_event.is_set():
            try:
                await self.client.async_ensure_logged_in()
                _LOGGER.debug("Starting SubscribeToDeviceEvents stream")
                await self.hass.async_add_executor_job(
                    self.client._stream_device_events,
                    self._on_event,
                    self._stop_event,
                )
                _LOGGER.debug("Event stream ended, reconnecting in 5s")
            except InnovaApiError as err:
                _LOGGER.warning("Event stream error: %s – reconnecting in 30s", err)
                await asyncio.sleep(30)
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("Unexpected error in event stream – reconnecting in 30s")
                await asyncio.sleep(30)
                continue
            if not self._stop_event.is_set():
                await asyncio.sleep(5)

    async def async_stop_streaming(self) -> None:
        """Stop the background event stream and wait for it to finish."""
        self._stop_event.set()
        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stream_task = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Innova Duepuntozero from a config entry."""
    client = InnovaClient(
        email=entry.data[CONF_EMAIL_KEY],
        password=entry.data[CONF_PASSWORD_KEY],
        mac_address=entry.data[CONF_MAC_KEY],
    )
    coordinator = InnovaCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    # Start the event stream as a background task – does not block setup.
    coordinator.start_streaming()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when options change (e.g. polling interval).
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: InnovaCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_stop_streaming()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
