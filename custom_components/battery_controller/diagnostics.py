"""Diagnostics support for the Battery Controller integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_GRID_EXPORT_SENSORS,
    CONF_GRID_IMPORT_SENSORS,
    CONF_GROSS_LOAD_SENSORS,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_PRICE_SENSOR,
    WEATHER_STALE_AFTER_MINUTES,
)

# Sensor entity IDs may be considered private; redact them
TO_REDACT: set[str] = {
    CONF_PRICE_SENSOR,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    "pv_forecast_sensor",
    CONF_GRID_EXPORT_SENSORS,
    CONF_GRID_IMPORT_SENSORS,
    CONF_GROSS_LOAD_SENSORS,
}


def _subentry_name_map(entry: ConfigEntry) -> dict[str, str]:
    """Return a mapping of subentry_id → title for all subentries."""
    return {sid: sub.title for sid, sub in entry.subentries.items()}


def _remap_keys(d: dict[str, Any], name_map: dict[str, str]) -> dict[str, Any]:
    """Replace subentry ID keys with their human-readable titles."""
    return {name_map.get(k, k): v for k, v in d.items()}


def _data_age_minutes(data: dict[str, Any] | None) -> float | None:
    """Return the age of coordinator data in minutes, from its timestamp."""
    if not data:
        return None
    timestamp = data.get("timestamp")
    if isinstance(timestamp, str):
        timestamp = dt_util.parse_datetime(timestamp)
    if not isinstance(timestamp, datetime):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt_util.UTC)
    age_minutes: float = (dt_util.utcnow() - timestamp).total_seconds() / 60
    return round(age_minutes, 1)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    entry_data = entry.runtime_data if hasattr(entry, "runtime_data") else None
    name_map = _subentry_name_map(entry)

    weather_coord = getattr(entry_data, "weather_coordinator", None)
    forecast_coord = getattr(entry_data, "forecast_coordinator", None)
    optimization_coord = getattr(entry_data, "optimization_coordinator", None)

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
        weather_age = _data_age_minutes(weather_coord.data)
        weather_data = {
            "last_update_success": weather_coord.last_update_success,
            # Age + staleness computed at dump time: last_update_success alone
            # stays True when polling silently stops, hiding a dead coordinator.
            "age_minutes": weather_age,
            "stale": (
                weather_age is not None and weather_age > WEATHER_STALE_AFTER_MINUTES
            ),
            "radiation_forecast": weather_coord.data.get("radiation_forecast"),
            "wind_speed_forecast": weather_coord.data.get("wind_speed_forecast"),
            "temperature_forecast": weather_coord.data.get("temperature_forecast"),
            "forecast_start_utc": str(weather_coord.data.get("forecast_start_utc")),
            "timestamp": str(weather_coord.data.get("timestamp")),
        }

    # Forecast coordinator data
    forecast_data = {}
    if forecast_coord and forecast_coord.data:
        forecast_data = {
            "last_update_success": forecast_coord.last_update_success,
            "age_minutes": _data_age_minutes(forecast_coord.data),
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
            "current_ghi_wm2": forecast_coord.data.get("current_ghi_wm2"),
            "current_wind_speed_ms": forecast_coord.data.get("current_wind_speed_ms"),
            "pv_dc_coupled": forecast_coord.data.get("pv_dc_coupled"),
            "per_pv_array_forecasts": _remap_keys(
                forecast_coord.data.get("per_pv_array_forecasts") or {}, name_map
            ),
            "forecast_interval_minutes": forecast_coord.data.get(
                "forecast_interval_minutes", 60
            ),
            "forecast_start_utc": str(forecast_coord.data.get("forecast_start_utc")),
            "timestamp": str(forecast_coord.data.get("timestamp")),
        }
        # Include learned consumption pattern
        if hasattr(forecast_coord, "consumption_model"):
            model = forecast_coord.consumption_model
            if hasattr(model, "_hourly_pattern"):
                # Convert (hour, dow) dict keys to string representation for JSON
                forecast_data["consumption_hourly_pattern"] = {
                    f"{h:02d}_{d}": round(v, 3)
                    for (h, d), v in model._hourly_pattern.items()
                }

    # Optimization coordinator data
    optimization_data = {}
    if optimization_coord and optimization_coord.data:
        data = optimization_coord.data
        shadow_price = data.get("shadow_price_eur_kwh", 0.0) or 0.0
        sqrt_rte = battery_config.get(
            "charge_efficiency", 1.0
        )  # charge_eff = sqrt(RTE)

        optimization_data = {
            "last_update_success": optimization_coord.last_update_success,
            "failure_reason": optimization_coord.last_failure_reason,
            "control_mode": data.get("control_mode"),
            "optimal_mode": data.get("optimal_mode"),
            "optimal_power_kw": data.get("optimal_power_kw"),
            "schedule_mode": data.get("schedule_mode"),
            "schedule_power_kw": data.get("schedule_power_kw"),
            "control_action": data.get("control_action"),
            "battery_setpoints": _remap_keys(
                data.get("battery_setpoints") or {}, name_map
            ),
            "total_cost": data.get("total_cost"),
            "baseline_cost": data.get("baseline_cost"),
            "savings": data.get("savings"),
            "current_price": data.get("current_price"),
            "current_feed_in_price": data.get("current_feed_in_price"),
            "price_forecast_source": data.get("price_forecast_source"),
            "shadow_price_eur_kwh": data.get("shadow_price_eur_kwh"),
            "raw_total_cost": data.get("raw_total_cost"),
            "raw_savings": data.get("raw_savings"),
            # Sell above lambda / sqrt(RTE), buy below lambda * sqrt(RTE): both
            # conversions lose sqrt(RTE), so the sell threshold is the higher one.
            "discharge_threshold_eur_kwh": round(
                shadow_price / sqrt_rte if sqrt_rte > 0 else 0.0, 4
            ),
            "charge_threshold_eur_kwh": round(shadow_price * sqrt_rte, 4),
            "commitment_state": {
                "action": getattr(optimization_coord, "_committed_action", None),
                "power_kw": getattr(optimization_coord, "_committed_power", None),
                "price": getattr(optimization_coord, "_committed_price", None),
                "step_start": getattr(
                    optimization_coord, "_committed_step_start", None
                ),
            },
            "charge_eff_correction": data.get("charge_eff_correction"),
            "charge_eff_samples": data.get("charge_eff_samples"),
            "discharge_eff_correction": data.get("discharge_eff_correction"),
            "discharge_eff_samples": data.get("discharge_eff_samples"),
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
                # Step timing — needed for exact DP reproduction
                "price_interval": data.get("price_interval"),
                "step_durations_hours": data.get("step_durations_hours"),
                "step_start_times_iso": data.get("step_start_times_iso"),
                # Feed-in and model forecasts — needed for full simulator fidelity
                "feed_in_price_forecast": data.get("feed_in_price_forecast"),
                "price_forecast_model": data.get("price_forecast_model"),
                "feed_in_price_forecast_model": data.get(
                    "feed_in_price_forecast_model"
                ),
                # Current shadow price, exported for informational/analyzer display only.
                # Not used as the DP terminal condition (see optimizer.py terminal_price).
                "terminal_shadow_price": data.get("shadow_price_eur_kwh"),
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

        # Diagnostic history: optimizer run log and real-time setpoint log
        if hasattr(optimization_coord, "_optimizer_run_log"):
            optimization_data["optimizer_run_log"] = list(
                optimization_coord._optimizer_run_log
            )
        if hasattr(optimization_coord, "_setpoint_log"):
            optimization_data["setpoint_log"] = list(optimization_coord._setpoint_log)

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
            "subentries": {
                sub.title: {
                    "type": sub.subentry_type,
                    "data": async_redact_data(dict(sub.data), TO_REDACT),
                }
                for sub in entry.subentries.values()
            },
        },
        "battery_config": battery_config,
        "weather": weather_data,
        "forecast": forecast_data,
        "optimization": optimization_data,
        "entities": entities,
    }
