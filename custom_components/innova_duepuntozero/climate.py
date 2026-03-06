"""Innova Duepuntozero climate entity."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import InnovaCoordinator
from .api import (
    FAN_AUTO,
    FAN_MAX,
    FAN_MEDIUM,
    FAN_MIN,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    TYPE_FLAP,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# HVAC mode mappings
HVAC_MODE_TO_INNOVA: dict[HVACMode, int] = {
    HVACMode.AUTO: MODE_AUTO,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.COOL: MODE_COOL,
    HVACMode.FAN_ONLY: MODE_FAN,
    HVACMode.DRY: MODE_DRY,
}
INNOVA_TO_HVAC_MODE: dict[int, HVACMode] = {v: k for k, v in HVAC_MODE_TO_INNOVA.items()}
ALL_HVAC_MODES = [HVACMode.OFF, *HVAC_MODE_TO_INNOVA]

# Fan speed mappings – using plain strings rather than FAN_* constants
# from homeassistant.components.climate.const because those don't include
# a "medium" level that maps cleanly to Min/Medium/Max.
FAN_SPEED_AUTO   = "auto"
FAN_SPEED_MIN    = "min"
FAN_SPEED_MEDIUM = "medium"
FAN_SPEED_MAX    = "max"

FAN_MODE_TO_INNOVA: dict[str, int] = {
    FAN_SPEED_AUTO:   FAN_AUTO,
    FAN_SPEED_MIN:    FAN_MIN,
    FAN_SPEED_MEDIUM: FAN_MEDIUM,
    FAN_SPEED_MAX:    FAN_MAX,
}
INNOVA_TO_FAN_MODE: dict[int, str] = {v: k for k, v in FAN_MODE_TO_INNOVA.items()}
ALL_FAN_MODES = list(FAN_MODE_TO_INNOVA)

# Swing (flap) mode labels
SWING_ON  = "on"
SWING_OFF = "off"
ALL_SWING_MODES = [SWING_ON, SWING_OFF]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Innova Duepuntozero climate entity."""
    coordinator: InnovaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([InnovaDuepuntozeroClimate(coordinator, entry)])


class InnovaDuepuntozeroClimate(CoordinatorEntity[InnovaCoordinator], ClimateEntity):
    """Climate entity for an Innova Duepuntozero device."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = ALL_HVAC_MODES
    _attr_fan_modes = ALL_FAN_MODES
    _attr_swing_modes = ALL_SWING_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
    )

    def __init__(self, coordinator: InnovaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Innova Duepuntozero",
            manufacturer="Innova",
            model="Duepuntozero",
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.room_temperature if self.coordinator.data else None

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.data.setpoint if self.coordinator.data else None

    @property
    def target_temperature_step(self) -> float:
        return self.coordinator.data.setpoint_step if self.coordinator.data else 0.5

    @property
    def min_temp(self) -> float:
        return self.coordinator.data.setpoint_min if self.coordinator.data else 16.0

    @property
    def max_temp(self) -> float:
        return self.coordinator.data.setpoint_max if self.coordinator.data else 31.0

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.coordinator.data or not self.coordinator.data.power_state:
            return HVACMode.OFF
        return INNOVA_TO_HVAC_MODE.get(self.coordinator.data.operation_mode, HVACMode.AUTO)

    @property
    def fan_mode(self) -> str | None:
        if not self.coordinator.data:
            return None
        return INNOVA_TO_FAN_MODE.get(self.coordinator.data.fan_speed, FAN_SPEED_AUTO)

    @property
    def swing_mode(self) -> str | None:
        if not self.coordinator.data:
            return None
        return SWING_ON if self.coordinator.data.flap else SWING_OFF

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.client.async_turn_off()
        else:
            innova_mode = HVAC_MODE_TO_INNOVA.get(hvac_mode, MODE_AUTO)
            await self.coordinator.client.async_set_operation_mode(innova_mode)
            if not self.coordinator.data or not self.coordinator.data.power_state:
                await self.coordinator.client.async_turn_on()
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.client.async_set_temperature(temperature)
        await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan speed."""
        innova_speed = FAN_MODE_TO_INNOVA.get(fan_mode, FAN_AUTO)
        await self.coordinator.client.async_set_fan_speed(innova_speed)
        await self.coordinator.async_request_refresh()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set flap (swing) on or off."""
        await self.coordinator.client.async_set_device_value(
            TYPE_FLAP, 1 if swing_mode == SWING_ON else 0
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        """Turn the device on."""
        await self.coordinator.client.async_turn_on()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn the device off."""
        await self.coordinator.client.async_turn_off()
        await self.coordinator.async_request_refresh()
