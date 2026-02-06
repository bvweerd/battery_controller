"""Data update coordinators for the Heating Curve Optimizer integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, Event
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AREA_M2,
    CONF_ENERGY_LABEL,
    CONF_GLASS_EAST_M2,
    CONF_GLASS_SOUTH_M2,
    CONF_GLASS_U_VALUE,
    CONF_GLASS_WEST_M2,
    CONF_INDOOR_TEMPERATURE_SENSOR,
    CONF_INDOOR_TEMP_HYSTERESIS,
    CONF_INDOOR_TEMP_HYSTERESIS_LOWER,
    CONF_INDOOR_TEMP_HYSTERESIS_UPPER,
    CONF_K_FACTOR,
    CONF_OFFSET_DELTA_T,
    CONF_BASE_COP,
    CONF_COP_COMPENSATION_FACTOR,
    CONF_OUTDOOR_TEMP_COEFFICIENT,
    CONF_CONSUMPTION_PRICE_SENSOR,
    CONF_PLANNING_WINDOW,
    CONF_TARGET_INDOOR_TEMP,
    CONF_TIME_BASE,
    CONF_MAX_BUFFER_DEBT,
    CONF_HEAT_CURVE_MIN,
    CONF_HEAT_CURVE_MAX,
    CONF_HEAT_CURVE_MIN_OUTDOOR,
    CONF_HEAT_CURVE_MAX_OUTDOOR,
    CONF_PV_EAST_WP,
    CONF_PV_SOUTH_WP,
    CONF_PV_WEST_WP,
    CONF_PV_TILT,
    CONF_VENTILATION_TYPE,
    CONF_CEILING_HEIGHT,
    DEFAULT_COP_AT_35,
    DEFAULT_INDOOR_TEMP_HYSTERESIS_LOWER,
    DEFAULT_INDOOR_TEMP_HYSTERESIS_UPPER,
    DEFAULT_K_FACTOR,
    DEFAULT_OFFSET_DELTA_T,
    DEFAULT_COP_COMPENSATION_FACTOR,
    DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
    DEFAULT_PLANNING_WINDOW,
    DEFAULT_TARGET_INDOOR_TEMP,
    DEFAULT_TIME_BASE,
    DEFAULT_MAX_BUFFER_DEBT,
    DEFAULT_VENTILATION_TYPE,
    DEFAULT_CEILING_HEIGHT,
    DEFAULT_PV_TILT,
    DOMAIN,
    INDOOR_TEMPERATURE,
    calculate_htc_from_energy_label,
)
from .helpers import extract_price_forecast_with_interval
from .optimizer import optimize_offsets

_LOGGER = logging.getLogger(__name__)


def _calculate_supply_temp_from_curve(
    outdoor_temp: float,
    min_supply: float,
    max_supply: float,
    min_outdoor: float,
    max_outdoor: float,
) -> float:
    """Calculate supply temperature from heating curve parameters."""
    if outdoor_temp <= min_outdoor:
        return max_supply
    if outdoor_temp >= max_outdoor:
        return min_supply
    ratio = (outdoor_temp - min_outdoor) / (max_outdoor - min_outdoor)
    return max_supply + (min_supply - max_supply) * ratio


def _calculate_cop(
    supply_temp: float,
    outdoor_temp: float,
    base_cop: float,
    k_factor: float,
    outdoor_temp_coefficient: float,
    cop_compensation: float,
) -> float:
    """Calculate COP from supply and outdoor temperatures."""
    cop = (
        base_cop
        + outdoor_temp_coefficient * outdoor_temp
        - k_factor * (supply_temp - 35)
    ) * cop_compensation
    return max(0.5, cop)


class WeatherDataCoordinator(DataUpdateCoordinator):
    """Coordinator for weather and radiation data from open-meteo.com."""

    def __init__(self, hass: HomeAssistant):
        """Initialize the weather data coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Weather Data",
            update_interval=timedelta(minutes=30),
        )
        self.latitude = hass.config.latitude
        self.longitude = hass.config.longitude
        self.session = async_get_clientsession(hass)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch weather and radiation data from open-meteo.com."""
        _LOGGER.debug(
            "Fetching weather data for %.4f, %.4f", self.latitude, self.longitude
        )

        # Combine temperature, humidity, and radiation in one API call
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.latitude}&longitude={self.longitude}"
            "&hourly=temperature_2m,relative_humidity_2m,shortwave_radiation"
            "&current_weather=true&timezone=UTC&forecast_days=2"
        )

        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"API returned status {resp.status}")
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error fetching weather data: {err}")

        # Extract current weather
        current_weather = data.get("current_weather", {})
        current_temp = float(current_weather.get("temperature", 0))

        # Extract hourly forecasts
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        humidity = hourly.get("relative_humidity_2m", [])
        radiation = hourly.get("shortwave_radiation", [])

        if not times or not temps:
            raise UpdateFailed("No forecast data in API response")

        # Find current hour index
        now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        start_idx = 0
        for i, ts in enumerate(times):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t >= now:
                start_idx = i
                break

        # Extract next 48 hours (2 days)
        temp_forecast = [float(v) for v in temps[start_idx : start_idx + 48]]
        humidity_forecast = (
            [float(v) for v in humidity[start_idx : start_idx + 48]] if humidity else []
        )
        radiation_forecast = (
            [float(v) for v in radiation[start_idx : start_idx + 48]]
            if radiation
            else []
        )

        result = {
            "current_temperature": round(current_temp, 2),
            "temperature_forecast": [round(v, 2) for v in temp_forecast],
            "humidity_forecast": [round(v, 1) for v in humidity_forecast],
            "radiation_forecast": [round(v, 1) for v in radiation_forecast],
            "timestamp": dt_util.utcnow(),
        }

        _LOGGER.debug(
            "Weather data updated: current=%.1f°C, forecast=%d hours",
            current_temp,
            len(temp_forecast),
        )

        return result


class HeatCalculationCoordinator(DataUpdateCoordinator):
    """Coordinator for heat loss, solar gain, and PV production calculations."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator: WeatherDataCoordinator,
        config: dict[str, Any],
    ):
        """Initialize the heat calculation coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Heat Calculations",
            update_interval=timedelta(minutes=5),
        )
        self.weather_coordinator = weather_coordinator
        self.config = config
        self._indoor_temp_sensor = config.get(CONF_INDOOR_TEMPERATURE_SENSOR)
        self._unsub = None

    async def async_setup(self) -> None:
        """Set up event tracking for indoor temperature changes."""
        if self._indoor_temp_sensor:
            self._unsub = async_track_state_change_event(
                self.hass,
                [self._indoor_temp_sensor],
                self._handle_indoor_temp_change,
            )
            _LOGGER.debug(
                "Tracking indoor temperature sensor: %s", self._indoor_temp_sensor
            )

    async def _handle_indoor_temp_change(self, event: Event) -> None:
        """Handle indoor temperature changes with debouncing."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if not old_state or not new_state:
            return

        try:
            old_temp = float(old_state.state)
            new_temp = float(new_state.state)
            # Only update if temperature changed by more than 0.5°C
            if abs(new_temp - old_temp) >= 0.5:
                _LOGGER.debug(
                    "Indoor temperature changed significantly: %.1f -> %.1f",
                    old_temp,
                    new_temp,
                )
                await self.async_request_refresh()
        except (ValueError, TypeError):
            pass

    async def async_shutdown(self) -> None:
        """Clean up event tracking."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Calculate heat loss, solar gain, and PV production."""
        # Get weather data from coordinator
        weather_data = self.weather_coordinator.data
        if not weather_data:
            raise UpdateFailed("No weather data available")

        # Get configuration
        area_m2 = self.config.get(CONF_AREA_M2)
        energy_label = self.config.get(CONF_ENERGY_LABEL)

        if not area_m2 or not energy_label:
            raise UpdateFailed("Missing area or energy label configuration")

        # Get indoor temperature
        indoor_temp = INDOOR_TEMPERATURE
        if self._indoor_temp_sensor:
            indoor_state = self.hass.states.get(self._indoor_temp_sensor)
            if indoor_state and indoor_state.state not in ("unknown", "unavailable"):
                try:
                    indoor_temp = float(indoor_state.state)
                except (ValueError, TypeError):
                    pass

        # Get target temperature and hysteresis from runtime data (number entities) or config
        runtime = self.hass.data.get(DOMAIN, {}).get("runtime", {})
        target_temp = runtime.get(
            CONF_TARGET_INDOOR_TEMP,
            self.config.get(CONF_TARGET_INDOOR_TEMP, DEFAULT_TARGET_INDOOR_TEMP),
        )

        # Get separate lower and upper hysteresis values
        # Lower hysteresis: how far below target before heat pump turns ON
        # Upper hysteresis: how far above target before heat pump turns OFF
        hysteresis_lower = runtime.get(
            CONF_INDOOR_TEMP_HYSTERESIS_LOWER,
            self.config.get(
                CONF_INDOOR_TEMP_HYSTERESIS_LOWER,
                # Fallback to legacy symmetric hysteresis
                self.config.get(
                    CONF_INDOOR_TEMP_HYSTERESIS, DEFAULT_INDOOR_TEMP_HYSTERESIS_LOWER
                ),
            ),
        )
        hysteresis_upper = runtime.get(
            CONF_INDOOR_TEMP_HYSTERESIS_UPPER,
            self.config.get(
                CONF_INDOOR_TEMP_HYSTERESIS_UPPER,
                # Fallback to legacy symmetric hysteresis
                self.config.get(
                    CONF_INDOOR_TEMP_HYSTERESIS, DEFAULT_INDOOR_TEMP_HYSTERESIS_UPPER
                ),
            ),
        )

        # Calculate heat demand factor based on indoor temp vs target
        # Lower bound: target - lower_hysteresis (heat pump turns ON below this)
        # Upper bound: target + upper_hysteresis (heat pump turns OFF above this)
        lower_bound = target_temp - hysteresis_lower
        upper_bound = target_temp + hysteresis_upper
        total_band = hysteresis_lower + hysteresis_upper

        if indoor_temp <= lower_bound:
            # Room is cold, need full heating + extra to catch up
            temp_deficit = lower_bound - indoor_temp
            heat_demand_factor = 1.0 + (temp_deficit * 0.5)  # 50% extra per °C below
        elif indoor_temp >= upper_bound:
            # Room is warm enough, no heating needed (heat pump OFF)
            heat_demand_factor = 0.0
        else:
            # In the hysteresis band, linear reduction from 1.0 to 0.0
            heat_demand_factor = (upper_bound - indoor_temp) / total_band

        # Calculate HTC (Heat Transfer Coefficient)
        ventilation_type = self.config.get(
            CONF_VENTILATION_TYPE, DEFAULT_VENTILATION_TYPE
        )
        ceiling_height = float(
            self.config.get(CONF_CEILING_HEIGHT, DEFAULT_CEILING_HEIGHT)
        )

        htc = calculate_htc_from_energy_label(
            energy_label,
            area_m2,
            ventilation_type=ventilation_type,
            ceiling_height=ceiling_height,
        )

        # Calculate current heat loss (base calculation)
        outdoor_temp = weather_data["current_temperature"]
        base_heat_loss = htc * (indoor_temp - outdoor_temp) / 1000  # Convert to kW

        # Apply heat demand factor based on target temperature
        heat_loss = base_heat_loss * heat_demand_factor

        # Calculate heat loss forecast (use target_temp for forecast, not current indoor_temp)
        # This assumes the room will reach target temperature
        heat_loss_forecast = [
            htc * (target_temp - t) / 1000 for t in weather_data["temperature_forecast"]
        ]

        # Calculate solar gain
        solar_gain, solar_forecast = await self.hass.async_add_executor_job(
            self._calculate_solar_gain,
            weather_data["radiation_forecast"],
        )

        # Calculate PV production
        pv_forecast = await self.hass.async_add_executor_job(
            self._calculate_pv_production,
            weather_data["radiation_forecast"],
        )

        # Calculate net heat loss (heat loss - solar gain)
        net_heat_loss = heat_loss - solar_gain
        net_forecast = [h - s for h, s in zip(heat_loss_forecast, solar_forecast)]

        # Determine if heat pump should be ON based on heat demand factor
        # Heat pump is ON when there's any positive demand
        heat_pump_on = heat_demand_factor > 0.0

        result = {
            "heat_loss": round(heat_loss, 3),
            "heat_loss_base": round(base_heat_loss, 3),
            "heat_loss_forecast": [round(v, 3) for v in heat_loss_forecast],
            "solar_gain": round(solar_gain, 3),
            "solar_gain_forecast": [round(v, 3) for v in solar_forecast],
            "pv_production_forecast": [round(v, 3) for v in pv_forecast],
            "net_heat_loss": round(net_heat_loss, 3),
            "net_heat_loss_forecast": [round(v, 3) for v in net_forecast],
            "outdoor_temperature": outdoor_temp,
            "indoor_temperature": indoor_temp,
            "target_temperature": target_temp,
            "heat_demand_factor": round(heat_demand_factor, 3),
            "heat_pump_on": heat_pump_on,
            "hysteresis_lower": hysteresis_lower,
            "hysteresis_upper": hysteresis_upper,
            "lower_bound": round(lower_bound, 2),
            "upper_bound": round(upper_bound, 2),
            "timestamp": dt_util.utcnow(),
        }

        _LOGGER.debug(
            "Heat calculations: loss=%.2f kW (base=%.2f, factor=%.2f), "
            "solar=%.2f kW, net=%.2f kW, indoor=%.1f°C, target=%.1f°C",
            heat_loss,
            base_heat_loss,
            heat_demand_factor,
            solar_gain,
            net_heat_loss,
            indoor_temp,
            target_temp,
        )

        return result

    def _calculate_solar_gain(
        self, radiation_forecast: list[float]
    ) -> tuple[float, list[float]]:
        """Calculate solar gain through windows (blocking call)."""
        glass_east = float(self.config.get(CONF_GLASS_EAST_M2, 0))
        glass_south = float(self.config.get(CONF_GLASS_SOUTH_M2, 0))
        glass_west = float(self.config.get(CONF_GLASS_WEST_M2, 0))
        glass_u = float(self.config.get(CONF_GLASS_U_VALUE, 1.2))

        total_glass = glass_east + glass_south + glass_west

        if total_glass == 0 or not radiation_forecast:
            return 0.0, [0.0] * len(radiation_forecast)

        # SHGC (Solar Heat Gain Coefficient) approximation
        # Lower U-value glass typically has lower SHGC
        shgc = max(0.3, 0.7 - (glass_u - 0.8) * 0.2)

        # Orientation factors (how much radiation reaches each direction)
        # These are rough approximations for Netherlands latitude
        orientation_factors = {
            "east": 0.6,  # Morning sun
            "south": 1.0,  # Maximum sun exposure
            "west": 0.6,  # Afternoon sun
        }

        solar_forecast = []
        for radiation in radiation_forecast:
            # Calculate solar gain for each orientation
            gain = (
                glass_east * radiation * orientation_factors["east"] * shgc
                + glass_south * radiation * orientation_factors["south"] * shgc
                + glass_west * radiation * orientation_factors["west"] * shgc
            ) / 1000  # Convert W to kW

            solar_forecast.append(max(0.0, gain))

        current_solar = solar_forecast[0] if solar_forecast else 0.0

        return current_solar, solar_forecast

    def _calculate_pv_production(self, radiation_forecast: list[float]) -> list[float]:
        """Calculate PV production forecast (blocking call)."""
        pv_east = float(self.config.get(CONF_PV_EAST_WP, 0))
        pv_south = float(self.config.get(CONF_PV_SOUTH_WP, 0))
        pv_west = float(self.config.get(CONF_PV_WEST_WP, 0))
        pv_tilt = float(self.config.get(CONF_PV_TILT, DEFAULT_PV_TILT))

        total_pv = pv_east + pv_south + pv_west

        if total_pv == 0 or not radiation_forecast:
            return [0.0] * len(radiation_forecast)

        # System efficiency (inverter + wiring + temperature losses)
        system_efficiency = 0.85

        # Tilt factor (how much radiation is affected by panel angle)
        # Optimal tilt for Netherlands is ~35°
        tilt_factor = 1.0 if pv_tilt == 35 else max(0.7, 1.0 - abs(pv_tilt - 35) * 0.01)

        # Orientation factors for PV panels
        orientation_factors = {
            "east": 0.65,
            "south": 1.0,
            "west": 0.65,
        }

        pv_forecast = []
        for radiation in radiation_forecast:
            # Calculate production for each orientation
            # Formula: Power (W) = Wp * (radiation / 1000) * efficiency
            # radiation is in W/m², 1000 W/m² is STC (Standard Test Conditions)
            production = (
                (
                    pv_east * radiation * orientation_factors["east"]
                    + pv_south * radiation * orientation_factors["south"]
                    + pv_west * radiation * orientation_factors["west"]
                )
                * tilt_factor
                * system_efficiency
                / 1000
                / 1000
            )  # First /1000 for STC, second for W to kW

            pv_forecast.append(max(0.0, production))

        return pv_forecast


class OptimizationCoordinator(DataUpdateCoordinator):
    """Coordinator for heating curve optimization using dynamic programming."""

    def __init__(
        self,
        hass: HomeAssistant,
        heat_coordinator: HeatCalculationCoordinator,
        config: dict[str, Any],
    ):
        """Initialize the optimization coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Heating Optimization",
            update_interval=timedelta(minutes=15),
        )
        self.heat_coordinator = heat_coordinator
        self.config = config
        self._price_sensor = config.get(CONF_CONSUMPTION_PRICE_SENSOR)
        self._unsub = None
        self._last_price = None
        self._current_buffer: float = 0.0  # Track actual buffer state
        self._current_offset: int = 0  # Track current offset for change constraint

    async def async_setup(self) -> None:
        """Set up event tracking for price changes."""
        if self._price_sensor:
            self._unsub = async_track_state_change_event(
                self.hass,
                [self._price_sensor],
                self._handle_price_change,
            )
            _LOGGER.debug("Tracking price sensor: %s", self._price_sensor)

    async def _handle_price_change(self, event: Event) -> None:
        """Handle significant price changes."""
        new_state = event.data.get("new_state")
        if not new_state:
            return

        try:
            new_price = float(new_state.state)
            # Only trigger on significant price change (>10%)
            if self._last_price is not None:
                change_pct = abs(new_price - self._last_price) / self._last_price
                if change_pct >= 0.10:
                    _LOGGER.debug(
                        "Significant price change detected: %.2f%%, triggering optimization",
                        change_pct * 100,
                    )
                    await self.async_request_refresh()
            self._last_price = new_price
        except (ValueError, TypeError):
            pass

    async def async_shutdown(self) -> None:
        """Clean up event tracking."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Run heating curve optimization."""
        # Get heat demand forecast
        heat_data = self.heat_coordinator.data
        if not heat_data:
            raise UpdateFailed("No heat calculation data available")

        demand_forecast = list(heat_data["net_heat_loss_forecast"])  # Make a copy

        # Get heat pump status
        heat_demand_factor = heat_data.get("heat_demand_factor", 1.0)
        heat_pump_on = heat_data.get("heat_pump_on", True)
        current_net_heat_loss = heat_data.get("net_heat_loss", 0.0)

        # Get time base for buffer calculations
        time_base = int(self.config.get(CONF_TIME_BASE, DEFAULT_TIME_BASE))
        step_hours = time_base / 60.0

        # Handle passive buffer changes when heat pump is OFF
        # The building exchanges heat with environment regardless of optimizer
        if not heat_pump_on:
            # Passive heat exchange: buffer changes by -net_heat_loss * time
            # net_heat_loss > 0: losing heat → buffer decreases
            # net_heat_loss < 0: solar gain → buffer increases
            passive_buffer_change = -current_net_heat_loss * step_hours
            self._current_buffer += passive_buffer_change

            _LOGGER.debug(
                "Heat pump OFF: passive buffer change %.3f kWh "
                "(net_loss=%.2f kW, buffer now=%.2f kWh)",
                passive_buffer_change,
                current_net_heat_loss,
                self._current_buffer,
            )

            # Set current demand to 0 for optimizer (heat pump not running)
            if demand_forecast:
                demand_forecast[0] = 0.0

        elif heat_demand_factor < 1.0 and demand_forecast:
            # Heat pump ON but with reduced demand - apply factor
            demand_forecast[0] = demand_forecast[0] * heat_demand_factor

        # Get outdoor temperature forecast from weather coordinator
        weather_data = self.heat_coordinator.weather_coordinator.data
        if not weather_data:
            raise UpdateFailed("No weather data available for optimization")

        temp_forecast = weather_data["temperature_forecast"]

        # Get price forecast
        if not self._price_sensor:
            raise UpdateFailed("No price sensor configured")

        price_state = self.hass.states.get(self._price_sensor)
        if not price_state or price_state.state in ("unknown", "unavailable"):
            raise UpdateFailed("Price sensor not available")

        price_forecast, price_interval = extract_price_forecast_with_interval(
            price_state
        )

        if not price_forecast:
            # Fallback to current price
            try:
                current_price = float(price_state.state)
                price_forecast = [current_price]
            except (ValueError, TypeError):
                raise UpdateFailed("Cannot extract price data")

        # Get optimization parameters
        planning_window = int(
            self.config.get(CONF_PLANNING_WINDOW, DEFAULT_PLANNING_WINDOW)
        )
        time_base = int(self.config.get(CONF_TIME_BASE, DEFAULT_TIME_BASE))
        offset_delta_t = int(
            self.config.get(CONF_OFFSET_DELTA_T, DEFAULT_OFFSET_DELTA_T)
        )
        max_buffer_debt = float(
            self.config.get(CONF_MAX_BUFFER_DEBT, DEFAULT_MAX_BUFFER_DEBT)
        )
        k_factor = float(self.config.get(CONF_K_FACTOR, DEFAULT_K_FACTOR))
        base_cop = float(self.config.get(CONF_BASE_COP, DEFAULT_COP_AT_35))
        outdoor_temp_coefficient = float(
            self.config.get(
                CONF_OUTDOOR_TEMP_COEFFICIENT, DEFAULT_OUTDOOR_TEMP_COEFFICIENT
            )
        )
        cop_compensation = float(
            self.config.get(
                CONF_COP_COMPENSATION_FACTOR, DEFAULT_COP_COMPENSATION_FACTOR
            )
        )

        # Get temperature limits
        min_supply = float(self.config.get(CONF_HEAT_CURVE_MIN, 20.0))
        max_supply = float(self.config.get(CONF_HEAT_CURVE_MAX, 45.0))
        min_outdoor = float(self.config.get(CONF_HEAT_CURVE_MIN_OUTDOOR, -20.0))
        max_outdoor = float(self.config.get(CONF_HEAT_CURVE_MAX_OUTDOOR, 20.0))

        # Run optimization in executor (CPU-intensive)
        _LOGGER.debug(
            "Running optimization with %d demand points, buffer=%.2f, offset=%d",
            len(demand_forecast),
            self._current_buffer,
            self._current_offset,
        )
        result = await self.hass.async_add_executor_job(
            self._run_optimization,
            demand_forecast,
            price_forecast,
            temp_forecast,
            planning_window,
            time_base,
            offset_delta_t,
            max_buffer_debt,
            price_interval,
            k_factor,
            base_cop,
            outdoor_temp_coefficient,
            cop_compensation,
            min_supply,
            max_supply,
            min_outdoor,
            max_outdoor,
            self._current_buffer,
            self._current_offset,
        )

        # Update tracked state for next optimization run
        new_offset = int(result.get("optimized_offset", 0))
        buffer_evolution = result.get("buffer_evolution", [])
        new_buffer = buffer_evolution[0] if buffer_evolution else self._current_buffer

        _LOGGER.debug(
            "Optimization complete: offset=%d°C (was %d), buffer=%.2f kWh (was %.2f), cost=%.3f",
            new_offset,
            self._current_offset,
            new_buffer,
            self._current_buffer,
            result.get("total_cost", 0),
        )

        self._current_offset = new_offset
        self._current_buffer = new_buffer

        return result

    def _run_optimization(
        self,
        demand_forecast: list[float],
        price_forecast: list[float],
        temp_forecast: list[float],
        planning_window: int,
        time_base: int,
        offset_delta_t: int,
        max_buffer_debt: float,
        price_interval: int,
        k_factor: float,
        base_cop: float,
        outdoor_temp_coefficient: float,
        cop_compensation: float,
        min_supply: float,
        max_supply: float,
        min_outdoor: float,
        max_outdoor: float,
        current_buffer: float,
        current_offset: int,
    ) -> dict[str, Any]:
        """Run DP optimization (blocking call in executor)."""
        try:
            # Limit forecasts to planning window
            max_steps = planning_window
            demand_limited = demand_forecast[:max_steps]
            price_limited = price_forecast[:max_steps]
            temp_limited = temp_forecast[:max_steps]

            # Call the optimizer with correct parameter names
            offsets, buffer_evolution = optimize_offsets(
                demand=demand_limited,
                prices=price_limited,
                base_temp=base_cop,
                k_factor=k_factor,
                cop_compensation_factor=cop_compensation,
                buffer=current_buffer,  # Use actual buffer state
                water_min=min_supply,
                water_max=max_supply,
                outdoor_temps=temp_limited,
                humidity_forecast=None,  # Not available yet
                outdoor_temp_coefficient=outdoor_temp_coefficient,
                time_base=time_base,
                outdoor_min=min_outdoor,
                outdoor_max=max_outdoor,
                max_buffer_debt=max_buffer_debt,  # Configurable heat debt limit
                current_offset=current_offset,  # Constrain first step change
                offset_delta_t=offset_delta_t,  # Minutes per 1°C offset change
            )

            # Calculate future supply temperatures and COP for both baseline and optimized
            future_supply_temps = []
            baseline_supply_temps = []
            baseline_cop_list = []
            optimized_cop_list = []
            step_hours = time_base / 60.0

            for i, offset in enumerate(offsets):
                if i < len(temp_limited):
                    outdoor_temp = temp_limited[i]
                    base_temp = _calculate_supply_temp_from_curve(
                        outdoor_temp, min_supply, max_supply, min_outdoor, max_outdoor
                    )
                    supply_temp = max(min(base_temp + offset, max_supply), min_supply)

                    baseline_supply_temps.append(round(base_temp, 1))
                    future_supply_temps.append(round(supply_temp, 1))
                    baseline_cop_list.append(
                        round(
                            _calculate_cop(
                                base_temp,
                                outdoor_temp,
                                base_cop,
                                k_factor,
                                outdoor_temp_coefficient,
                                cop_compensation,
                            ),
                            3,
                        )
                    )
                    optimized_cop_list.append(
                        round(
                            _calculate_cop(
                                supply_temp,
                                outdoor_temp,
                                base_cop,
                                k_factor,
                                outdoor_temp_coefficient,
                                cop_compensation,
                            ),
                            3,
                        )
                    )
                else:
                    # No temperature data, use min_supply as fallback
                    baseline_supply_temps.append(round(min_supply, 1))
                    future_supply_temps.append(round(min_supply, 1))
                    baseline_cop_list.append(3.0)
                    optimized_cop_list.append(3.0)

            # Calculate real costs: electricity cost = (heat_demand / COP) * time * price
            baseline_cost = 0.0
            optimized_cost = 0.0

            for i in range(len(offsets)):
                if i < len(demand_limited) and i < len(price_limited):
                    demand = max(0.0, demand_limited[i])
                    price = price_limited[i]
                    b_cop = baseline_cop_list[i]
                    o_cop = optimized_cop_list[i]

                    if b_cop > 0:
                        baseline_cost += (demand / b_cop) * step_hours * price
                    if o_cop > 0:
                        optimized_cost += (demand / o_cop) * step_hours * price

            cost_savings = baseline_cost - optimized_cost

            return {
                "optimized_offset": round(offsets[0], 1) if offsets else 0.0,
                "optimized_offsets": [round(v, 1) for v in offsets],
                "buffer_evolution": [round(v, 3) for v in buffer_evolution],
                "initial_buffer": round(current_buffer, 3),
                "previous_offset": current_offset,
                "future_supply_temperatures": future_supply_temps,
                "baseline_supply_temperatures": baseline_supply_temps,
                "baseline_cop": baseline_cop_list,
                "optimized_cop": optimized_cop_list,
                "baseline_cost": round(baseline_cost, 3),
                "total_cost": round(optimized_cost, 3),
                "cost_savings": round(cost_savings, 3),
                "prices": [round(p, 5) for p in price_limited],
                "demand_forecast": [round(d, 3) for d in demand_limited],
                "outdoor_forecast": [round(t, 1) for t in temp_limited],
                "timestamp": dt_util.utcnow(),
            }

        except Exception as err:
            _LOGGER.error("Optimization failed: %s", err, exc_info=True)
            # Return safe fallback
            return {
                "optimized_offset": 0.0,
                "optimized_offsets": [0.0],
                "buffer_evolution": [0.0],
                "total_cost": 0.0,
                "timestamp": dt_util.utcnow(),
                "error": str(err),
            }
