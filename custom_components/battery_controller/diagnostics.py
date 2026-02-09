"""Diagnostics support for the Battery Controller integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


# Sensor entity IDs may be considered private; redact them
TO_REDACT: set[str] = {
    "price_sensor",
    "feed_in_price_sensor",
    "battery_soc_sensor",
    "battery_power_sensor",
    "pv_forecast_sensor",
    "electricity_consumption_sensors",
    "electricity_production_sensors",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    entry_data = entry.runtime_data if hasattr(entry, "runtime_data") else {}

    weather_coord = entry_data.get("weather_coordinator")
    forecast_coord = entry_data.get("forecast_coordinator")
    optimization_coord = entry_data.get("optimization_coordinator")

    # Battery configuration (derived values are useful for debugging)
    battery_config = {}
    if optimization_coord and hasattr(optimization_coord, "battery_config"):
        cfg = optimization_coord.battery_config
        battery_config = {
            "capacity_kwh": cfg.capacity_kwh,
            "usable_capacity_kwh": cfg.usable_capacity_kwh,
            "max_charge_power_kw": cfg.max_charge_power_kw,
            "max_discharge_power_kw": cfg.max_discharge_power_kw,
            "round_trip_efficiency": cfg.round_trip_efficiency,
            "charge_efficiency": round(cfg.charge_efficiency, 4),
            "discharge_efficiency": round(cfg.discharge_efficiency, 4),
            "min_soc_percent": cfg.min_soc_percent,
            "max_soc_percent": cfg.max_soc_percent,
            "min_soc_kwh": cfg.min_soc_kwh,
            "max_soc_kwh": cfg.max_soc_kwh,
            "pv_dc_coupled": cfg.pv_dc_coupled,
            "pv_dc_peak_power_kwp": cfg.pv_dc_peak_power_kwp,
            "pv_dc_efficiency": cfg.pv_dc_efficiency,
        }

    # Weather coordinator data
    weather_data = {}
    if weather_coord and weather_coord.data:
        weather_data = {
            "last_update_success": weather_coord.last_update_success,
            "radiation_forecast": weather_coord.data.get("radiation_forecast"),
            "forecast_start_utc": str(weather_coord.data.get("forecast_start_utc")),
            "timestamp": str(weather_coord.data.get("timestamp")),
        }

    # Forecast coordinator data
    forecast_data = {}
    if forecast_coord and forecast_coord.data:
        forecast_data = {
            "last_update_success": forecast_coord.last_update_success,
            "pv_forecast_kw": forecast_coord.data.get("pv_forecast_kw"),
            "pv_dc_forecast_kw": forecast_coord.data.get("pv_dc_forecast_kw"),
            "consumption_forecast_kw": forecast_coord.data.get(
                "consumption_forecast_kw"
            ),
            "net_load_forecast_kw": forecast_coord.data.get("net_load_forecast_kw"),
            "current_pv_kw": forecast_coord.data.get("current_pv_kw"),
            "current_dc_pv_kw": forecast_coord.data.get("current_dc_pv_kw"),
            "current_consumption_kw": forecast_coord.data.get("current_consumption_kw"),
            "current_net_load_kw": forecast_coord.data.get("current_net_load_kw"),
            "pv_dc_coupled": forecast_coord.data.get("pv_dc_coupled"),
            "timestamp": str(forecast_coord.data.get("timestamp")),
        }
        # Include learned consumption pattern
        if hasattr(forecast_coord, "consumption_model"):
            model = forecast_coord.consumption_model
            if hasattr(model, "_hourly_pattern"):
                forecast_data["consumption_hourly_pattern"] = [
                    round(v, 3) for v in model._hourly_pattern
                ]

    # Optimization coordinator data
    optimization_data = {}
    if optimization_coord and optimization_coord.data:
        data = optimization_coord.data
        optimization_data = {
            "last_update_success": optimization_coord.last_update_success,
            "control_mode": data.get("control_mode"),
            "optimal_mode": data.get("optimal_mode"),
            "optimal_power_kw": data.get("optimal_power_kw"),
            "schedule_mode": data.get("schedule_mode"),
            "schedule_power_kw": data.get("schedule_power_kw"),
            "total_cost": data.get("total_cost"),
            "baseline_cost": data.get("baseline_cost"),
            "savings": data.get("savings"),
            "current_price": data.get("current_price"),
            "timestamp": str(data.get("timestamp")),
        }

        # Include full schedule data
        result = data.get("optimization_result")
        if result:
            optimization_data["schedule"] = {
                "power_schedule_kw": result.power_schedule_kw,
                "mode_schedule": result.mode_schedule,
                "soc_schedule_kwh": result.soc_schedule_kwh,
                "price_forecast": result.price_forecast,
                "pv_forecast": result.pv_forecast,
                "consumption_forecast": result.consumption_forecast,
            }

        # Battery state at time of optimization
        battery_state = data.get("battery_state")
        if battery_state:
            optimization_data["battery_state"] = {
                "soc_kwh": battery_state.soc_kwh,
                "soc_percent": battery_state.soc_percent,
                "power_kw": battery_state.power_kw,
                "mode": battery_state.mode,
            }

    # Collect all entity states
    ent_reg = er.async_get(hass)
    entities: list[dict[str, Any]] = []
    for ent_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        state = hass.states.get(ent_entry.entity_id)
        entities.append(
            {
                "entity_id": ent_entry.entity_id,
                "unique_id": ent_entry.unique_id,
                "state": state.state if state else None,
                "attributes": dict(state.attributes) if state else {},
            }
        )

    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "battery_config": battery_config,
        "weather": weather_data,
        "forecast": forecast_data,
        "optimization": optimization_data,
        "entities": entities,
    }
