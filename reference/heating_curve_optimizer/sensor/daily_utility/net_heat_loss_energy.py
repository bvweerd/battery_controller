"""Daily utility sensor for net heat loss energy (kWh)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
    RestoreSensor,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_track_time_interval,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from ...entity import BaseUtilitySensor

_LOGGER = logging.getLogger(__name__)


class NetHeatLossEnergyDailySensor(RestoreSensor, BaseUtilitySensor):
    """Daily utility sensor tracking net heat loss energy in kWh."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        icon: str,
        device: DeviceInfo,
        *,
        net_heat_loss_sensor: str,
    ):
        """Initialize the sensor."""
        BaseUtilitySensor.__init__(
            self,
            name=name,
            unique_id=unique_id,
            unit="kWh",
            device_class=SensorDeviceClass.ENERGY,
            icon=icon,
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self.hass = hass
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_should_poll = False
        self._attr_native_value = 0.0

        # Sensor references
        self.net_heat_loss_sensor = net_heat_loss_sensor

        # Tracking state
        self._last_update: datetime | None = None
        self._last_reset: datetime | None = None
        self._daily_total = 0.0
        self._unsub_timer = None
        self._unsub_state = None

    async def async_added_to_hass(self) -> None:
        """Restore state when added to hass."""
        await super().async_added_to_hass()

        # Restore previous state
        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data and last_sensor_data.native_value is not None:
            self._daily_total = float(last_sensor_data.native_value)
            self._attr_native_value = self._daily_total
            _LOGGER.debug("Restored daily net heat loss: %.3f kWh", self._daily_total)

        # Check if we need to reset (new day)
        now = dt_util.utcnow()
        last_state = await self.async_get_last_state()
        if last_state and last_state.last_updated:
            last_date = last_state.last_updated.date()
            current_date = now.date()
            if current_date > last_date:
                _LOGGER.info("New day detected, resetting daily net heat loss counter")
                self._daily_total = 0.0
                self._attr_native_value = 0.0
                self._last_reset = now

        # Start periodic updates every 5 minutes
        update_interval = timedelta(minutes=5)
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_update_energy, update_interval
        )

        # Also track state changes for more responsive updates
        self._unsub_state = async_track_state_change_event(
            self.hass,
            [self.net_heat_loss_sensor],
            self._handle_state_change,
        )

        # Schedule daily reset at midnight
        self._schedule_daily_reset()

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup when removed."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    def _schedule_daily_reset(self):
        """Schedule reset at midnight."""
        now = dt_util.utcnow()
        # Calculate next midnight
        tomorrow = now.date() + timedelta(days=1)
        next_midnight = dt_util.as_utc(datetime.combine(tomorrow, datetime.min.time()))

        async def _reset_at_midnight(_now):
            """Reset counter at midnight."""
            _LOGGER.info("Midnight reset: Daily net heat loss counter")
            self._daily_total = 0.0
            self._attr_native_value = 0.0
            self._last_reset = dt_util.utcnow()
            self.async_write_ha_state()
            # Schedule next reset
            self._schedule_daily_reset()

        # Schedule the reset
        delta = next_midnight - now
        self.hass.loop.call_later(
            delta.total_seconds(),
            lambda: self.hass.async_create_task(_reset_at_midnight(None)),
        )

    async def _handle_state_change(self, event):
        """Handle state change of net heat loss sensor."""
        await self._async_update_energy()

    async def _async_update_energy(self, now: datetime | None = None) -> None:
        """Update cumulative energy periodically."""
        current_time = dt_util.utcnow()

        # Get current net heat loss
        heat_loss_state = self.hass.states.get(self.net_heat_loss_sensor)

        # Check if sensor is available
        if not heat_loss_state or heat_loss_state.state in ("unknown", "unavailable"):
            _LOGGER.debug("Net heat loss sensor not available")
            return

        try:
            heat_loss_kw = float(heat_loss_state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid net heat loss value: %s", heat_loss_state.state)
            return

        # Calculate energy since last update
        if self._last_update:
            time_delta_hours = (
                current_time - self._last_update
            ).total_seconds() / 3600.0
            # Energy (kWh) = Power (kW) × Time (h)
            # Note: Net heat loss can be negative (solar gain > heat loss)
            # We only accumulate positive values (actual heat loss)
            energy_delta = max(0, heat_loss_kw) * time_delta_hours

            # Only add if within reasonable time window
            if time_delta_hours < 1.0:  # Sanity check: max 1 hour gap
                self._daily_total += energy_delta
                self._attr_native_value = round(self._daily_total, 3)

                _LOGGER.debug(
                    "Energy update: +%.4f kWh (total: %.3f kWh, power: %.2f kW, duration: %.2f h)",
                    energy_delta,
                    self._daily_total,
                    heat_loss_kw,
                    time_delta_hours,
                )

                self.async_write_ha_state()

        self._last_update = current_time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {}
        if self._last_update:
            attrs["last_update"] = self._last_update.isoformat()
        if self._last_reset:
            attrs["last_reset"] = self._last_reset.isoformat()
        attrs["source_sensor"] = self.net_heat_loss_sensor
        return attrs
