"""Total cost savings sensor with cumulative tracking."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
    RestoreSensor,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ...entity import BaseUtilitySensor

_LOGGER = logging.getLogger(__name__)


class TotalCostSavingsSensor(RestoreSensor, BaseUtilitySensor):
    """Cumulative cost savings sensor tracking total savings since activation."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        icon: str,
        device: DeviceInfo,
        *,
        offset_sensor: str,
        outdoor_sensor: str,
        calculated_supply_sensor: str,
        consumption_price_sensor: str,
        heat_demand_sensor: str,
        k_factor: float,
        base_cop: float,
        outdoor_temp_coefficient: float,
        cop_compensation_factor: float,
        time_base: int = 60,
    ):
        """Initialize the sensor."""
        BaseUtilitySensor.__init__(
            self,
            name=name,
            unique_id=unique_id,
            unit="€",
            device_class=SensorDeviceClass.MONETARY,
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
        self.offset_sensor = offset_sensor
        self.outdoor_sensor = outdoor_sensor
        self.calculated_supply_sensor = calculated_supply_sensor
        self.consumption_price_sensor = consumption_price_sensor
        self.heat_demand_sensor = heat_demand_sensor

        # COP parameters
        self.k_factor = k_factor
        self.base_cop = base_cop
        self.outdoor_temp_coefficient = outdoor_temp_coefficient
        self.cop_compensation_factor = cop_compensation_factor
        self.time_base = time_base

        # Tracking state
        self._last_update: datetime | None = None
        self._total_savings = 0.0
        self._unsub_timer = None

    async def async_added_to_hass(self) -> None:
        """Restore state when added to hass."""
        await super().async_added_to_hass()

        # Restore previous state
        last_state = await self.async_get_last_sensor_data()
        if last_state and last_state.native_value is not None:
            self._total_savings = float(last_state.native_value)
            self._attr_native_value = self._total_savings
            _LOGGER.debug("Restored total cost savings: €%.3f", self._total_savings)

        # Start periodic updates
        update_interval = timedelta(minutes=self.time_base)
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_update_savings, update_interval
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup when removed."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        await super().async_will_remove_from_hass()

    @callback
    async def _async_update_savings(self, now: datetime | None = None) -> None:
        """Update cumulative savings periodically."""
        # Get current states
        offset_state = self.hass.states.get(self.offset_sensor)
        outdoor_state = self.hass.states.get(self.outdoor_sensor)
        calculated_supply_state = self.hass.states.get(self.calculated_supply_sensor)
        price_state = self.hass.states.get(self.consumption_price_sensor)
        demand_state = self.hass.states.get(self.heat_demand_sensor)

        # Check all required states are available
        if (
            not offset_state
            or not outdoor_state
            or not calculated_supply_state
            or not price_state
            or not demand_state
            or offset_state.state in ("unknown", "unavailable")
            or outdoor_state.state in ("unknown", "unavailable")
            or calculated_supply_state.state in ("unknown", "unavailable")
            or price_state.state in ("unknown", "unavailable")
            or demand_state.state in ("unknown", "unavailable")
        ):
            _LOGGER.debug("Not all sensors available for savings calculation")
            return

        try:
            current_offset = float(offset_state.state)
            outdoor_temp = float(outdoor_state.state)
            baseline_supply_temp = float(calculated_supply_state.state)
            current_price = float(price_state.state)
            heat_demand = float(demand_state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid sensor values for savings calculation")
            return

        # Only calculate savings if offset is active and heat demand is positive
        if abs(current_offset) < 0.01 or heat_demand <= 0:
            return

        # Get optimized supply temperature from offset sensor attributes
        optimized_supply_temp = baseline_supply_temp + current_offset

        # Calculate baseline COP (without offset)
        baseline_cop = (
            self.base_cop
            + self.outdoor_temp_coefficient * outdoor_temp
            - self.k_factor * (baseline_supply_temp - 35)
        ) * self.cop_compensation_factor
        baseline_cop = max(0.5, baseline_cop)

        # Calculate optimized COP (with offset)
        optimized_cop = (
            self.base_cop
            + self.outdoor_temp_coefficient * outdoor_temp
            - self.k_factor * (optimized_supply_temp - 35)
        ) * self.cop_compensation_factor
        optimized_cop = max(0.5, optimized_cop)

        # Calculate electricity consumption for both scenarios
        # electricity (kWh) = heat_demand (kW) * time_base (hours) / COP
        time_hours = self.time_base / 60.0

        baseline_electricity = (heat_demand / baseline_cop) * time_hours
        optimized_electricity = (heat_demand / optimized_cop) * time_hours

        # Calculate cost difference
        baseline_cost = baseline_electricity * current_price
        optimized_cost = optimized_electricity * current_price
        period_savings = baseline_cost - optimized_cost

        # Only add positive savings (negative would mean optimization made it worse)
        if period_savings > 0:
            self._total_savings += period_savings
            self._attr_native_value = round(self._total_savings, 3)
            self._last_update = dt_util.utcnow()

            _LOGGER.debug(
                "Period savings: €%.4f (total: €%.3f)",
                period_savings,
                self._total_savings,
            )

            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {}
        if self._last_update:
            attrs["last_update"] = self._last_update.isoformat()
        attrs["time_base_minutes"] = self.time_base
        return attrs
