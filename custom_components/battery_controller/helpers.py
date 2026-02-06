"""Helper functions for the Battery Controller integration."""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.core import State
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def _normalize_price_value(value: Any) -> float | None:
    """Normalize a raw price value to a float if possible."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("price")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _detect_interval_from_entries(entries: Any) -> int:
    """Detect the interval in minutes from a list of price entries with timestamps.

    Returns 60 (hourly) if interval cannot be determined.
    """
    if not isinstance(entries, (list, tuple)) or len(entries) < 2:
        return 60

    timestamps = []
    for entry in entries[:3]:  # Check first 3 entries
        if isinstance(entry, dict):
            start = entry.get("start") or entry.get("from") or entry.get("time")
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

    Supports various price sensor formats:
    - forecast_prices attribute (hourly)
    - net_prices_today/tomorrow (with interval detection)
    - raw_today/raw_tomorrow
    - today/tomorrow
    - forecast attribute

    Returns:
        Tuple of (prices list, interval in minutes)
    """
    now = dt_util.utcnow()

    # First check for forecast_prices (assumed hourly)
    forecast_attr = state.attributes.get("forecast_prices")
    if isinstance(forecast_attr, (list, tuple)):
        forecast: list[float] = []
        for entry in forecast_attr:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return forecast, 60

    # Check for net_prices_today/tomorrow with interval detection
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
                start = entry.get("start") or entry.get("from") or entry.get("time")
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

    # Fallback to generic forecast
    generic_forecast = state.attributes.get("forecast")
    if isinstance(generic_forecast, (list, tuple)):
        forecast = []
        for entry in generic_forecast:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return forecast, 60

    # Try raw_today/raw_tomorrow
    hour = now.hour
    forecast = []

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
        return forecast, 60

    # Try today/tomorrow
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

    # Last resort: use current state value
    try:
        price = float(state.state)
    except (TypeError, ValueError):
        return [], 60

    return [price], 60


def extract_price_forecast(state: State) -> list[float]:
    """Extract price forecast from a Home Assistant price state."""
    prices, _ = extract_price_forecast_with_interval(state)
    return prices


def resample_forecast(
    forecast: list[float],
    source_interval_minutes: int,
    target_interval_minutes: int,
) -> list[float]:
    """Resample a forecast to a different time interval.

    Args:
        forecast: Source forecast values
        source_interval_minutes: Source interval in minutes
        target_interval_minutes: Target interval in minutes

    Returns:
        Resampled forecast
    """
    if source_interval_minutes == target_interval_minutes:
        return forecast

    if not forecast:
        return []

    # Calculate total duration in minutes
    total_duration = len(forecast) * source_interval_minutes
    target_steps = total_duration // target_interval_minutes

    resampled = []
    for i in range(target_steps):
        target_start = i * target_interval_minutes
        target_end = (i + 1) * target_interval_minutes

        # Find overlapping source intervals
        values = []
        weights = []

        for j, value in enumerate(forecast):
            source_start = j * source_interval_minutes
            source_end = (j + 1) * source_interval_minutes

            # Calculate overlap
            overlap_start = max(target_start, source_start)
            overlap_end = min(target_end, source_end)
            overlap = max(0, overlap_end - overlap_start)

            if overlap > 0:
                values.append(value)
                weights.append(overlap)

        if values:
            # Weighted average
            total_weight = sum(weights)
            weighted_sum = sum(v * w for v, w in zip(values, weights))
            resampled.append(weighted_sum / total_weight)

    return resampled


def clamp(value: float, min_value: float, max_value: float) -> float:
    """Clamp a value between min and max."""
    return max(min_value, min(max_value, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    if value is None:
        return default
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def get_sensor_value(
    hass: Any,
    entity_id: str | None,
    default: float = 0.0,
) -> float:
    """Get a sensor value from Home Assistant."""
    if not entity_id:
        return default

    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return default

    return safe_float(state.state, default)


def calculate_pv_forecast(
    solar_radiation_wm2: list[float],
    peak_power_kwp: float,
    orientation_deg: float = 180,  # 180 = south
    tilt_deg: float = 35,
    efficiency_factor: float = 0.85,
) -> list[float]:
    """Calculate PV production forecast from solar radiation.

    Simple PV model:
    P_pv = G * A * eta * orientation_factor * tilt_factor

    Where:
    - G = solar radiation (W/m2)
    - A * eta ≈ peak_power_kwp / 1000 (normalized)
    - orientation_factor = cos(orientation - sun_azimuth)
    - tilt_factor = based on sun elevation

    Args:
        solar_radiation_wm2: Solar radiation forecast in W/m2
        peak_power_kwp: PV system peak power in kWp
        orientation_deg: Panel orientation in degrees (180 = south)
        tilt_deg: Panel tilt angle in degrees
        efficiency_factor: System efficiency factor (inverter, cables, etc.)

    Returns:
        PV production forecast in kW
    """
    if peak_power_kwp <= 0:
        return [0.0] * len(solar_radiation_wm2)

    # Simplified orientation factor (south = 1.0, east/west = 0.65)
    orientation_factor = 1.0
    if orientation_deg < 135 or orientation_deg > 225:
        # Not facing south
        deviation = min(abs(orientation_deg - 180), abs(orientation_deg - 180 + 360))
        orientation_factor = max(0.5, 1.0 - deviation / 180)

    # Simplified tilt factor (35 degrees optimal for Netherlands)
    tilt_factor = 1.0 - abs(tilt_deg - 35) * 0.01

    forecast = []
    for radiation in solar_radiation_wm2:
        # Power = radiation * peak_power / STC_radiation * factors
        # STC radiation = 1000 W/m2
        power_kw = (
            radiation
            / 1000
            * peak_power_kwp
            * orientation_factor
            * tilt_factor
            * efficiency_factor
        )
        forecast.append(max(0.0, power_kw))

    return forecast


def calculate_consumption_pattern(
    hour_of_day: int,
    day_of_week: int,
    base_consumption_kw: float = 0.5,
) -> float:
    """Calculate expected consumption based on time patterns.

    Default Dutch household pattern (3500 kWh/year = ~0.4 kW average).

    Args:
        hour_of_day: Hour of day (0-23)
        day_of_week: Day of week (0=Monday, 6=Sunday)
        base_consumption_kw: Base consumption level in kW

    Returns:
        Expected consumption in kW
    """
    # Hourly pattern (relative to base)
    hourly_pattern = [
        0.5,  # 00:00
        0.4,  # 01:00
        0.4,  # 02:00
        0.4,  # 03:00
        0.4,  # 04:00
        0.5,  # 05:00
        0.8,  # 06:00
        1.2,  # 07:00
        1.3,  # 08:00
        1.0,  # 09:00
        0.9,  # 10:00
        0.9,  # 11:00
        1.1,  # 12:00
        1.0,  # 13:00
        0.9,  # 14:00
        0.9,  # 15:00
        1.0,  # 16:00
        1.4,  # 17:00
        1.6,  # 18:00
        1.5,  # 19:00
        1.3,  # 20:00
        1.1,  # 21:00
        0.9,  # 22:00
        0.7,  # 23:00
    ]

    # Weekend factor (slightly different pattern)
    weekend_factor = 1.1 if day_of_week >= 5 else 1.0

    return base_consumption_kw * hourly_pattern[hour_of_day] * weekend_factor
