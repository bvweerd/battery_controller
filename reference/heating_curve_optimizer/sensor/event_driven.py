"""Event-driven sensors for Heating Curve Optimizer.

These sensors listen to state changes and update in real-time,
as opposed to coordinator-based sensors that update on a schedule.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.core import HomeAssistant, State
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from ..entity import BaseUtilitySensor
from ..helpers import extract_price_forecast
from ..const import (
    DEFAULT_K_FACTOR,
    DEFAULT_COP_AT_35,
    DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
    DEFAULT_THERMAL_STORAGE_EFFICIENCY,
)

_LOGGER = logging.getLogger(__name__)


class CurrentElectricityPriceSensor(BaseUtilitySensor):
    _unrecorded_attributes = frozenset({"forecast_prices"})

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        price_sensor: str,
        source_type: str,
        price_settings: dict[str, float],
        icon: str,
        device: DeviceInfo,
    ):
        unit = "€/kWh"
        super().__init__(
            name=name,
            unique_id=unique_id,
            unit=unit,
            device_class=None,
            icon=icon,
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self.hass = hass
        self.price_sensor = price_sensor
        self.source_type = source_type
        self.price_settings = price_settings
        self._extra_attrs: dict[str, Any] = {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._extra_attrs

    async def async_update(self):
        state = self.hass.states.get(self.price_sensor)
        if state is None or state.state in ("unknown", "unavailable"):
            self._attr_available = False
            self._extra_attrs = {}
            _LOGGER.warning("Price sensor %s is unavailable", self.price_sensor)
            return
        try:
            base_price = float(state.state)
        except ValueError:
            self._attr_available = False
            self._extra_attrs = {}
            _LOGGER.warning("Price sensor %s has invalid state", self.price_sensor)
            return
        self._attr_available = True

        self._attr_native_value = round(base_price, 8)
        attrs: dict[str, Any] = dict(state.attributes)
        forecast = extract_price_forecast(state)
        if forecast:
            attrs["forecast_prices"] = forecast
        self._extra_attrs = attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                self.price_sensor,
                self._handle_price_change,
            )
        )

    async def async_will_remove_from_hass(self):
        await super().async_will_remove_from_hass()

    async def _handle_price_change(self, event):
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            self._attr_available = False
            _LOGGER.warning("Price sensor %s is unavailable", self.price_sensor)
            return
        await self.async_update()
        # During unit tests the entity is not added via an EntityComponent and
        # therefore does not get an entity_id assigned. In that case
        # ``async_write_ha_state`` would raise ``NoEntitySpecifiedError``. We
        # still want to update the internal value but only call
        # ``async_write_ha_state`` when an entity_id is present.
        if self.entity_id:
            self.async_write_ha_state()


class HeatPumpThermalPowerSensor(BaseUtilitySensor):
    """Calculate current thermal output of the heat pump."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        power_sensor: str,
        supply_sensor: str,
        outdoor_sensor: str | SensorEntity,
        device: DeviceInfo,
        k_factor: float = DEFAULT_K_FACTOR,
        base_cop: float = DEFAULT_COP_AT_35,
    ):
        super().__init__(
            name=name,
            unique_id=unique_id,
            unit="kW",
            device_class=None,
            icon="mdi:fire",
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self.hass = hass
        self.power_sensor = power_sensor
        self.supply_sensor = supply_sensor
        self.outdoor_sensor = outdoor_sensor
        self.k_factor = k_factor
        self.base_cop = base_cop

    async def async_update(self):
        p_state = self.hass.states.get(self.power_sensor)
        if p_state is None:
            self._set_unavailable(
                f"vermogenssensor {self.power_sensor} werd niet gevonden"
            )
            return
        if p_state.state in ("unknown", "unavailable"):
            self._set_unavailable(
                f"vermogenssensor {self.power_sensor} heeft status '{p_state.state}'"
            )
            return

        s_state = self.hass.states.get(self.supply_sensor)
        if s_state is None:
            self._set_unavailable(
                f"aanvoersensor {self.supply_sensor} werd niet gevonden"
            )
            return
        if s_state.state in ("unknown", "unavailable"):
            self._set_unavailable(
                f"aanvoersensor {self.supply_sensor} heeft status '{s_state.state}'"
            )
            return

        entity_id = (
            self.outdoor_sensor.entity_id
            if isinstance(self.outdoor_sensor, SensorEntity)
            else cast(str, self.outdoor_sensor)
        )
        sensor_name = entity_id or str(self.outdoor_sensor)

        if entity_id is None:
            self._set_unavailable("geen buitensensor gevonden")
            return

        o_state = self.hass.states.get(entity_id)
        if (
            isinstance(self.outdoor_sensor, SensorEntity)
            and self.outdoor_sensor.entity_id
        ):
            self.outdoor_sensor = self.outdoor_sensor.entity_id
            entity_id = cast(str, self.outdoor_sensor)
            sensor_name = entity_id

        if o_state is None:
            self._set_unavailable(f"geen buitensensor gevonden ({sensor_name})")
            return
        if o_state.state in ("unknown", "unavailable"):
            self._set_unavailable(
                f"buitensensor {sensor_name} heeft status '{o_state.state}'"
            )
            return

        try:
            power = float(p_state.state)
        except ValueError:
            self._set_unavailable(
                f"waarde van vermogenssensor {self.power_sensor} is ongeldig"
            )
            return
        try:
            s_temp = float(s_state.state)
        except ValueError:
            self._set_unavailable(
                f"waarde van aanvoersensor {self.supply_sensor} is ongeldig"
            )
            return
        try:
            o_temp = float(o_state.state)
        except ValueError:
            self._set_unavailable(f"waarde van buitensensor {sensor_name} is ongeldig")
            return
        cop = self.base_cop + 0.08 * o_temp - self.k_factor * (s_temp - 35)
        thermal_power = power * cop / 1000.0
        _LOGGER.debug(
            "Thermal power calc power=%s cop=%s -> %s",
            power,
            cop,
            thermal_power,
        )
        self._mark_available()
        self._attr_native_value = round(thermal_power, 3)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if isinstance(self.outdoor_sensor, SensorEntity):
            self.outdoor_sensor = self.outdoor_sensor.entity_id


# New sensor classes start here


class CopEfficiencyDeltaSensor(BaseUtilitySensor):
    """Predict COP deltas for future offsets."""

    _unrecorded_attributes = frozenset({"future_cop", "cop_deltas"})

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        *,
        cop_sensor: str | SensorEntity,
        offset_entity: str | SensorEntity,
        outdoor_sensor: str | SensorEntity,
        calculated_supply_sensor: str | SensorEntity,
        device: DeviceInfo,
        k_factor: float = DEFAULT_K_FACTOR,
        base_cop: float = DEFAULT_COP_AT_35,
        outdoor_temp_coefficient: float = DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
        cop_compensation_factor: float = 1.0,
    ) -> None:
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
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self.hass = hass
        self.cop_sensor = cop_sensor
        self.offset_entity = offset_entity
        self.outdoor_sensor = outdoor_sensor
        self.calculated_supply_sensor = calculated_supply_sensor
        self.k_factor = k_factor
        self.base_cop = base_cop
        self.outdoor_temp_coefficient = outdoor_temp_coefficient
        self.cop_compensation_factor = cop_compensation_factor
        self._extra_attrs: dict[str, list[float] | float] = {}

    @property
    def extra_state_attributes(self) -> dict[str, list[float] | float]:
        return self._extra_attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        for ent in (
            self._resolve_entity_id(self.cop_sensor),
            self._resolve_entity_id(self.offset_entity),
            self._resolve_entity_id(self.outdoor_sensor),
            self._resolve_entity_id(self.calculated_supply_sensor),
        ):
            if ent is None:
                continue
            self.async_on_remove(
                async_track_state_change_event(self.hass, ent, self._handle_change)
            )

    async def _handle_change(self, event):  # pragma: no cover - simple callback
        await self.async_update()
        self.async_write_ha_state()

    def _resolve_entity_id(self, entity_ref: str | SensorEntity) -> str | None:
        """Return the entity_id for a reference or None if unavailable."""

        if isinstance(entity_ref, SensorEntity):
            entity_id = entity_ref.entity_id
            if entity_id is not None:
                if entity_ref is self.cop_sensor:
                    self.cop_sensor = entity_id
                elif entity_ref is self.offset_entity:
                    self.offset_entity = entity_id
                elif entity_ref is self.outdoor_sensor:
                    self.outdoor_sensor = entity_id
                elif entity_ref is self.calculated_supply_sensor:
                    self.calculated_supply_sensor = entity_id
            return entity_id
        return cast(str, entity_ref)

    def _get_state(self, entity_ref: str | SensorEntity) -> State | None:
        """Return hass state for the given entity reference."""

        entity_id = self._resolve_entity_id(entity_ref)
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return state

    async def async_update(self):
        offset_state = self._get_state(self.offset_entity)
        outdoor_state = self._get_state(self.outdoor_sensor)
        calculated_supply_state = self._get_state(self.calculated_supply_sensor)

        if (
            offset_state is None
            or outdoor_state is None
            or calculated_supply_state is None
            or outdoor_state.state in ("unknown", "unavailable")
            or calculated_supply_state.state in ("unknown", "unavailable")
        ):
            self._attr_available = False
            return

        try:
            outdoor_temp = float(outdoor_state.state)
            baseline_supply_temp = float(calculated_supply_state.state)
        except ValueError:
            self._attr_available = False
            return

        supply_temps = offset_state.attributes.get("future_supply_temperatures")
        if not supply_temps:
            self._attr_available = False
            return

        # Check current offset - if 0, delta should be 0
        try:
            current_offset = float(offset_state.state)
        except (ValueError, TypeError):
            current_offset = 0.0

        # Calculate baseline COP (without offset)
        baseline_cop = (
            self.base_cop
            + self.outdoor_temp_coefficient * outdoor_temp
            - self.k_factor * (baseline_supply_temp - 35)
        ) * self.cop_compensation_factor
        baseline_cop = max(0.5, baseline_cop)

        # If offset is 0, no optimization is active, so delta is 0
        if abs(current_offset) < 0.01:
            self._attr_native_value = 0.0
            self._extra_attrs = {
                "future_cop": [round(baseline_cop, 3)] * len(supply_temps),
                "cop_deltas": [0.0] * len(supply_temps),
                "baseline_cop": round(baseline_cop, 3),
            }
            self._attr_available = True
            return

        # Calculate predicted COPs with optimized supply temperatures
        predicted_cops = [
            max(
                0.5,
                (
                    self.base_cop
                    + self.outdoor_temp_coefficient * outdoor_temp
                    - self.k_factor * (float(s_temp) - 35)
                )
                * self.cop_compensation_factor,
            )
            for s_temp in supply_temps
        ]
        cop_deltas = [round(c - baseline_cop, 3) for c in predicted_cops]
        self._extra_attrs = {
            "future_cop": [round(c, 3) for c in predicted_cops],
            "cop_deltas": cop_deltas,
            "baseline_cop": round(baseline_cop, 3),
        }
        self._attr_native_value = cop_deltas[0] if cop_deltas else 0.0
        self._attr_available = True


class HeatGenerationDeltaSensor(BaseUtilitySensor):
    """Calculate buffer change rate based on offset and heat demand.

    This sensor shows how much the thermal buffer is changing per hour (in kW)
    based on the heating curve offset and current heat demand.

    Formula: buffer_change_rate = offset × heat_demand × thermal_storage_efficiency

    Where:
    - offset: heating curve offset in °C
    - heat_demand: net heat loss in kW
    - thermal_storage_efficiency: 0.15 (15% of demand stored per °C offset)
    """

    _unrecorded_attributes = frozenset({"future_buffer_change_rates"})

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        *,
        thermal_power_sensor: str | SensorEntity,
        cop_sensor: str | SensorEntity,
        offset_entity: str | SensorEntity,
        outdoor_sensor: str | SensorEntity,
        calculated_supply_sensor: str | SensorEntity,
        device: DeviceInfo,
        k_factor: float = DEFAULT_K_FACTOR,
        base_cop: float = DEFAULT_COP_AT_35,
        outdoor_temp_coefficient: float = DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
        cop_compensation_factor: float = 1.0,
    ) -> None:
        super().__init__(
            name=name,
            unique_id=unique_id,
            unit="kW",
            device_class=None,
            icon="mdi:fire",
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self.hass = hass
        self.thermal_power_sensor = thermal_power_sensor
        self.cop_sensor = cop_sensor
        self.offset_entity = offset_entity
        self.outdoor_sensor = outdoor_sensor
        self.calculated_supply_sensor = calculated_supply_sensor
        self.k_factor = k_factor
        self.base_cop = base_cop
        self.outdoor_temp_coefficient = outdoor_temp_coefficient
        self.cop_compensation_factor = cop_compensation_factor
        self._extra_attrs: dict[str, list[float] | float] = {}

    @property
    def extra_state_attributes(self) -> dict[str, list[float] | float]:
        return self._extra_attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Track offset sensor for changes
        offset_entity_id = self._resolve_entity_id(self.offset_entity)
        if offset_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, offset_entity_id, self._handle_change
                )
            )
        # Track net heat loss sensor for changes
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                "sensor.heating_curve_optimizer_net_heat_loss",
                self._handle_change,
            )
        )

    async def _handle_change(self, event):  # pragma: no cover - simple callback
        await self.async_update()
        self.async_write_ha_state()

    def _resolve_entity_id(self, entity_ref: str | SensorEntity) -> str | None:
        """Return the entity_id for a reference or None if unavailable."""

        if isinstance(entity_ref, SensorEntity):
            entity_id = entity_ref.entity_id
            if entity_id is not None:
                if entity_ref is self.thermal_power_sensor:
                    self.thermal_power_sensor = entity_id
                elif entity_ref is self.cop_sensor:
                    self.cop_sensor = entity_id
                elif entity_ref is self.offset_entity:
                    self.offset_entity = entity_id
                elif entity_ref is self.outdoor_sensor:
                    self.outdoor_sensor = entity_id
                elif entity_ref is self.calculated_supply_sensor:
                    self.calculated_supply_sensor = entity_id
            return entity_id
        return cast(str, entity_ref)

    def _get_state(self, entity_ref: str | SensorEntity) -> State | None:
        """Return hass state for the given entity reference."""

        entity_id = self._resolve_entity_id(entity_ref)
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return state

    async def async_update(self):
        """Calculate buffer change rate based on offset and heat demand.

        Buffer change rate = offset × heat_demand × thermal_storage_efficiency

        This represents how much thermal energy is being stored (positive)
        or released (negative) from the building's thermal mass per hour.
        """
        offset_state = self._get_state(self.offset_entity)

        if offset_state is None:
            self._attr_available = False
            return

        # Get current offset
        try:
            current_offset = float(offset_state.state)
        except (ValueError, TypeError):
            self._attr_available = False
            return

        # If offset is 0, no thermal storage is happening
        if abs(current_offset) < 0.01:
            self._attr_native_value = 0.0
            self._extra_attrs = {
                "buffer_change_rate": 0.0,
                "future_buffer_change_rates": [],
                "explanation": "No offset applied - no buffer change",
            }
            self._attr_available = True
            return

        # Get heat demand forecast from optimization coordinator data
        # We need to access the heating curve offset sensor's attributes
        demand_forecast = offset_state.attributes.get("demand_forecast", [])
        optimized_offsets = offset_state.attributes.get("optimized_offsets", [])

        if not demand_forecast or not optimized_offsets:
            # Fallback: try to get net heat loss from the net heat loss sensor
            net_heat_loss_state = self.hass.states.get(
                "sensor.heating_curve_optimizer_net_heat_loss"
            )
            if net_heat_loss_state is None or net_heat_loss_state.state in (
                "unknown",
                "unavailable",
            ):
                self._attr_available = False
                return

            try:
                current_heat_demand = max(0.0, float(net_heat_loss_state.state))
            except (ValueError, TypeError):
                self._attr_available = False
                return

            # Calculate current buffer change rate
            buffer_change_rate = (
                current_offset
                * current_heat_demand
                * DEFAULT_THERMAL_STORAGE_EFFICIENCY
            )

            self._attr_native_value = round(buffer_change_rate, 3)
            self._extra_attrs = {
                "buffer_change_rate": round(buffer_change_rate, 3),
                "current_offset": current_offset,
                "current_heat_demand": round(current_heat_demand, 3),
                "thermal_storage_efficiency": DEFAULT_THERMAL_STORAGE_EFFICIENCY,
                "explanation": (
                    f"Buffer changing at {buffer_change_rate:.3f} kW "
                    f"(offset {current_offset}°C × demand {current_heat_demand:.3f} kW "
                    f"× efficiency {DEFAULT_THERMAL_STORAGE_EFFICIENCY})"
                ),
            }
            self._attr_available = True
            return

        # Calculate future buffer change rates from optimization data
        future_buffer_change_rates = [
            round(
                offset * max(0.0, demand) * DEFAULT_THERMAL_STORAGE_EFFICIENCY,
                3,
            )
            for offset, demand in zip(optimized_offsets, demand_forecast)
        ]

        # Current buffer change rate
        if demand_forecast:
            current_heat_demand = max(0.0, demand_forecast[0])
            buffer_change_rate = (
                current_offset
                * current_heat_demand
                * DEFAULT_THERMAL_STORAGE_EFFICIENCY
            )
        else:
            buffer_change_rate = 0.0
            current_heat_demand = 0.0

        self._attr_native_value = round(buffer_change_rate, 3)
        self._extra_attrs = {
            "buffer_change_rate": round(buffer_change_rate, 3),
            "future_buffer_change_rates": future_buffer_change_rates,
            "current_offset": current_offset,
            "current_heat_demand": round(current_heat_demand, 3),
            "thermal_storage_efficiency": DEFAULT_THERMAL_STORAGE_EFFICIENCY,
            "explanation": (
                f"Buffer changing at {buffer_change_rate:.3f} kW "
                f"(offset {current_offset}°C × demand {current_heat_demand:.3f} kW "
                f"× efficiency {DEFAULT_THERMAL_STORAGE_EFFICIENCY})"
            ),
        }
        self._attr_available = True
