"""COP sensor that reads from supply and outdoor temperature sensors."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import DeviceInfo

from ...entity import BaseUtilitySensor
from ...const import (
    DEFAULT_K_FACTOR,
    DEFAULT_COP_AT_35,
    DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
    DEFAULT_COP_COMPENSATION_FACTOR,
)


class CoordinatorQuadraticCopSensor(BaseUtilitySensor):
    """COP sensor that reads from supply and outdoor temperature sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator,
        name: str,
        unique_id: str,
        supply_sensor: str,
        device: DeviceInfo,
        k_factor: float = DEFAULT_K_FACTOR,
        base_cop: float = DEFAULT_COP_AT_35,
        outdoor_temp_coefficient: float = DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
        cop_compensation_factor: float = DEFAULT_COP_COMPENSATION_FACTOR,
    ):
        """Initialize the COP sensor."""
        super().__init__(
            name=name,
            unique_id=unique_id,
            unit="",
            device_class=None,
            icon="mdi:alpha-c-circle",
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self.hass = hass
        self.weather_coordinator = weather_coordinator
        self.supply_sensor = supply_sensor
        self.k_factor = k_factor
        self.base_cop = base_cop
        self.outdoor_temp_coefficient = outdoor_temp_coefficient
        self.cop_compensation_factor = cop_compensation_factor
        self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_update(self):
        """Update COP based on supply and outdoor temperature."""
        # Get supply temperature
        s_state = self.hass.states.get(self.supply_sensor)
        if not s_state or s_state.state in ("unknown", "unavailable"):
            self._set_unavailable(f"Supply sensor {self.supply_sensor} unavailable")
            return

        try:
            supply_temp = float(s_state.state)
        except (ValueError, TypeError):
            self._set_unavailable("Invalid supply temperature")
            return

        # Get outdoor temperature from coordinator
        weather_data = self.weather_coordinator.data
        if not weather_data:
            self._set_unavailable("No weather data available")
            return

        outdoor_temp = weather_data.get("current_temperature")
        if outdoor_temp is None:
            self._set_unavailable("No outdoor temperature")
            return

        # Calculate COP using quadratic formula
        # COP = (base_cop + outdoor_coefficient * T_out - k_factor * (T_supply - 35)) * compensation
        cop = (
            self.base_cop
            + self.outdoor_temp_coefficient * outdoor_temp
            - self.k_factor * (supply_temp - 35)
        ) * self.cop_compensation_factor

        self._attr_native_value = round(max(1.0, cop), 2)
        self._mark_available()
