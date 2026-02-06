"""Helper functions for the Heating Curve Optimizer integration."""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.core import State

_LOGGER = logging.getLogger(__name__)


def _coerce_time_base(value: Any) -> int | None:
    """Return a positive integer time-base in minutes if possible."""

    try:
        base = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(base) or base <= 0:
        return None
    return int(round(base))


def _normalize_price_value(value: Any) -> float | None:
    """Normalize a raw price value to a float if possible."""

    if isinstance(value, dict):
        value = value.get("value")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _detect_interval_from_entries(entries: Any) -> int:
    """Detect the interval in minutes from a list of price entries with timestamps.

    Returns 60 (hourly) if interval cannot be determined.
    """
    from homeassistant.util import dt as dt_util

    if not isinstance(entries, (list, tuple)) or len(entries) < 2:
        return 60

    timestamps = []
    for entry in entries[:3]:  # Check first 3 entries
        if isinstance(entry, dict):
            start = entry.get("start") or entry.get("from")
            if isinstance(start, str):
                start_dt = dt_util.parse_datetime(start)
                if start_dt is not None:
                    timestamps.append(dt_util.as_utc(start_dt))

    if len(timestamps) >= 2:
        delta = timestamps[1] - timestamps[0]
        minutes = int(delta.total_seconds() / 60)
        if minutes in (15, 30, 60):
            return minutes

    return 60


def extract_price_forecast_with_interval(state: State) -> tuple[list[float], int]:
    """Extract price forecast and detected interval from a Home Assistant price state.

    Returns:
        Tuple of (prices list, interval in minutes)
    """
    # First check for forecast_prices (assumed hourly)
    forecast_attr = state.attributes.get("forecast_prices")
    if isinstance(forecast_attr, (list, tuple)):
        forecast: list[float] = []
        for entry in forecast_attr:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return forecast, 60  # forecast_prices is assumed hourly

    # Check for net_prices_today/tomorrow BEFORE generic forecast
    # These attributes have timestamps that allow proper interval detection
    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow()

    interval_forecast: list[float] = []
    detected_interval = 60

    def _extend_interval_forecast(entries: Any, *, skip_past: bool = False) -> bool:
        nonlocal detected_interval
        if not isinstance(entries, (list, tuple)):
            return False

        # Detect interval from entries with timestamps
        interval = _detect_interval_from_entries(entries)
        if interval != 60:
            detected_interval = interval

        added = False
        for entry in entries:
            if skip_past and isinstance(entry, dict):
                start = entry.get("start") or entry.get("from")
                if isinstance(start, str):
                    start_dt = dt_util.parse_datetime(start)
                    if start_dt is not None:
                        start_dt = dt_util.as_utc(start_dt)
                        if start_dt < now:
                            continue

            price = _normalize_price_value(entry)
            if price is not None:
                interval_forecast.append(price)
                added = True
        return added

    _extend_interval_forecast(state.attributes.get("net_prices_today"), skip_past=True)
    _extend_interval_forecast(state.attributes.get("net_prices_tomorrow"))

    if interval_forecast:
        return interval_forecast, detected_interval

    # Fallback to generic forecast (assumed hourly)
    generic_forecast = state.attributes.get("forecast")
    if isinstance(generic_forecast, (list, tuple)):
        forecast = []
        for entry in generic_forecast:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return forecast, 60  # generic forecast is assumed hourly

    hour = now.hour

    forecast: list[float] = []
    raw_today = state.attributes.get("raw_today")
    if isinstance(raw_today, list):
        for entry in raw_today[hour:]:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)

    raw_tomorrow = state.attributes.get("raw_tomorrow")
    if isinstance(raw_tomorrow, list):
        for entry in raw_tomorrow:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)

    if forecast:
        return forecast, 60  # raw_today/tomorrow is assumed hourly

    combined: list[Any] = []
    for key in ("today", "tomorrow"):
        attr = state.attributes.get(key)
        if isinstance(attr, list):
            combined.extend(attr)

    for entry in combined:
        price = _normalize_price_value(entry)
        if price is not None:
            forecast.append(price)

    if forecast:
        return forecast, 60

    try:
        price = float(state.state)
    except (TypeError, ValueError):
        return [], 60

    return [price], 60


def extract_price_forecast(state: State) -> list[float]:
    """Extract an hourly price forecast from a Home Assistant price state."""
    prices, _ = extract_price_forecast_with_interval(state)
    return prices


def calculate_supply_temperature(
    outdoor_temp: float,
    *,
    water_min: float,
    water_max: float,
    outdoor_min: float,
    outdoor_max: float,
) -> float:
    """Return supply temperature for a given outdoor temperature."""
    if outdoor_temp <= outdoor_min:
        return water_max
    if outdoor_temp >= outdoor_max:
        return water_min
    ratio = (outdoor_temp - outdoor_min) / (outdoor_max - outdoor_min)
    return water_max + (water_min - water_max) * ratio


def calculate_defrost_factor(outdoor_temp: float, humidity: float = 80.0) -> float:
    """Calculate COP degradation due to defrost cycles for air-source heat pumps.

    Based on research for air-source heat pumps in humid climates (like Netherlands).
    Frosting occurs when outdoor temperature is between -10°C and 6°C with sufficient humidity.
    The worst frosting occurs around 0-3°C with high humidity (70-90%).

    Args:
        outdoor_temp: Outdoor temperature in °C
        humidity: Relative humidity in % (default 80% for Dutch maritime climate)

    Returns:
        Multiplier (0.60-1.0) to apply to base COP accounting for defrost losses

    Research references:
    - Frosting occurs at 100% RH below 3.1°C, at 70% RH below 5.3°C
    - COP degradation: typical 10-15%, worst case up to 40%
    - Most critical range: 0-7°C in humid climates
    """
    # No frosting above 6°C - heat pump operates at full efficiency
    if outdoor_temp >= 6.0:
        return 1.0

    # No frosting below -10°C (air too dry, insufficient moisture to freeze)
    if outdoor_temp <= -10.0:
        return 1.0

    # Calculate humidity-dependent frosting threshold
    # At 100% RH: frosting starts at 3.1°C
    # At 70% RH: frosting starts at 5.3°C
    # Linear interpolation for other humidity levels
    frosting_threshold = 3.1 + (humidity - 100) * (5.3 - 3.1) / (70 - 100)
    frosting_threshold = max(min(frosting_threshold, 6.0), -10.0)

    # No frosting if temperature is above the humidity-dependent threshold
    if outdoor_temp >= frosting_threshold:
        return 1.0

    # Frosting zone: calculate defrost penalty
    if outdoor_temp >= 0:
        # Worst frosting zone: 0-3°C
        # COP loss increases as we approach 0-2°C
        if outdoor_temp <= 3:
            # Maximum penalty at 0-2°C: 15-40% depending on humidity
            base_penalty = 0.25  # 25% base COP loss in worst conditions
            temp_factor = (
                1.0 - (outdoor_temp / 3.0) * 0.4
            )  # Reduces penalty as temp increases
        else:
            # Moderate frosting zone: 3-6°C
            # Linear reduction in penalty from 3°C to frosting threshold
            base_penalty = 0.15  # 15% COP loss
            temp_factor = (frosting_threshold - outdoor_temp) / (
                frosting_threshold - 3.0
            )
    else:
        # Below freezing: -10 to 0°C
        # Moderate frosting, less severe than near-zero temperatures
        base_penalty = 0.12  # 12% COP loss
        temp_factor = (outdoor_temp + 10) / 10.0

    # Adjust for humidity (Dutch climate typically 75-90% RH in winter)
    # Higher humidity = more frost formation = worse COP degradation
    humidity_factor = min(1.0, max(0.5, humidity / 80.0))  # Normalized to 80% baseline

    # Calculate final defrost penalty
    defrost_penalty = base_penalty * temp_factor * humidity_factor

    # Return COP multiplier (1.0 = no loss, 0.6 = 40% loss in worst case)
    cop_multiplier = 1.0 - defrost_penalty

    _LOGGER.debug(
        "Defrost factor: T=%.1f°C, RH=%.0f%% -> multiplier=%.3f (%.0f%% COP loss)",
        outdoor_temp,
        humidity,
        cop_multiplier,
        defrost_penalty * 100,
    )

    return max(0.60, cop_multiplier)  # Minimum 60% efficiency (40% max loss)
