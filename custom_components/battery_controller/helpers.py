"""Helper functions for the Battery Controller integration."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import State
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# The two state strings Home Assistant uses for "this entity has no reading".
UNAVAILABLE_STATES = ("unknown", "unavailable")


def state_has_value(state: State | None) -> bool:
    """Return whether a state object exists and carries a real reading."""
    return state is not None and state.state not in UNAVAILABLE_STATES


def usable_state(hass: Any, entity_id: str | None) -> State | None:
    """Look up an entity, returning None unless it carries a real reading."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return state if state_has_value(state) else None


def battery_energy_sensor_ids(
    battery_subentries: Iterable[tuple[str, dict[str, Any]]], key: str
) -> list[str]:
    """Collect one battery energy counter per subentry that has it configured.

    Deduplicated: where a single inverter reports one counter for several packs,
    the same entity may legitimately be selected on more than one subentry, and
    counting it twice would double the measured throughput.
    """
    seen: list[str] = []
    for _subentry_id, data in battery_subentries:
        entity_id = data.get(key)
        if entity_id and entity_id not in seen:
            seen.append(entity_id)
    return seen


def _normalize_price_value(value: Any) -> float | None:
    """Normalize a raw price value to a float if possible."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("price")

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


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
            elif isinstance(start, datetime):
                timestamps.append(dt_util.as_utc(start))

    if len(timestamps) >= 2:
        delta = timestamps[1] - timestamps[0]
        minutes = int(delta.total_seconds() / 60)
        if minutes in (15, 30, 60):
            return minutes

    return 60


def _first_entry_has_timestamp(entries: Any) -> bool:
    """Return True when the first entry is a dict carrying a start timestamp."""
    if not isinstance(entries, (list, tuple)) or not entries:
        return False
    first = entries[0]
    return isinstance(first, dict) and bool(
        first.get("start") or first.get("from") or first.get("time")
    )


def _skip_index_since_local_midnight(now_local: datetime, interval_minutes: int) -> int:
    """Return the number of fully elapsed price periods since local midnight.

    Uses true elapsed time (now - midnight) rather than wall-clock
    hour*60+minute: daily price arrays (raw_today/today) contain one entry per
    period since midnight, so on DST transition days (23- or 25-hour days)
    wall-clock arithmetic points at the wrong entry.
    """
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Subtract via POSIX timestamps: naive subtraction of two datetimes that
    # share the same tzinfo ignores UTC-offset changes, which is exactly the
    # DST case this helper needs to handle.
    elapsed_min = (now_local.timestamp() - midnight.timestamp()) / 60
    return int(elapsed_min // interval_minutes)


# A per-kWh price never reaches 5 EUR and a per-MWh price practically always
# passes it, so the magnitude settles the unit whenever the sensor does not.
MWH_MAGNITUDE_THRESHOLD = 5.0


def price_unit_scale(
    state: State | None, samples: Sequence[float] | None = None
) -> float:
    """Return the factor converting a sensor's prices to EUR/kWh.

    The sensor's ``unit_of_measurement`` decides whenever it has one: per-MWh
    units (e.g. the OMIE integration's €/MWh) yield 0.001, anything else 1.0.
    Sensors publishing no unit at all — templates, and any sensor read before
    its attributes exist — are judged on the magnitude of ``samples`` instead.

    Every price path must reach the same verdict. The live forecast and the
    learned historical pattern are spliced into one horizon for the same DP, so
    a unit rule applied to one but not the other puts the two halves a factor
    1000 apart: the model-priced tail then dominates every decision and inflates
    the reported baseline beyond recognition.
    """
    unit = ""
    if state is not None:
        unit = str(state.attributes.get("unit_of_measurement") or "").lower()
    if unit:
        return 0.001 if "mwh" in unit else 1.0
    if samples and any(abs(value) > MWH_MAGNITUDE_THRESHOLD for value in samples):
        return 0.001
    return 1.0


def _extract_hours_dict_forecast(
    state: State, now: datetime
) -> tuple[list[float], list[datetime], int] | None:
    """Extract prices from OMIE-style hour-keyed dict attributes.

    The OMIE integration (hass_omie) exposes ``today_hours`` and
    ``tomorrow_hours``: dicts mapping period-start datetimes (or ISO strings)
    to prices in €/MWh. ``tomorrow_hours`` is None before publication and
    values are None while provisional — both are skipped.

    Values are returned in the sensor's own unit; the caller converts them to
    EUR/kWh, so that every extraction path is scaled by the same rule.

    Returns:
        (prices, start_times_utc, interval_minutes), or None when the sensor has
        no hour-dict attributes with usable data.
    """
    entries: dict[datetime, float] = {}
    found_attr = False
    for attr_key in ("today_hours", "tomorrow_hours"):
        hours = state.attributes.get(attr_key)
        if not isinstance(hours, dict):
            continue
        found_attr = True
        for raw_ts, raw_price in hours.items():
            price = _normalize_price_value(raw_price)
            if price is None:
                continue
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, str):
                parsed = dt_util.parse_datetime(raw_ts)
                if parsed is None:
                    continue
                ts = parsed
            else:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt_util.UTC)
            entries[dt_util.as_utc(ts)] = price

    if not found_attr or not entries:
        return None

    sorted_ts = sorted(entries)
    interval = 60
    if len(sorted_ts) >= 2:
        minutes = int((sorted_ts[1] - sorted_ts[0]).total_seconds() / 60)
        if minutes in (15, 30, 60):
            interval = minutes

    prices: list[float] = []
    start_times: list[datetime] = []
    for ts in sorted_ts:
        if ts + timedelta(minutes=interval) <= now:
            continue
        prices.append(entries[ts])
        start_times.append(ts)

    if not prices:
        return None
    return prices, start_times, interval


def extract_price_forecast_with_interval(state: State) -> tuple[list[float], int]:
    """Extract price forecast and detected interval from a Home Assistant price state.

    Convenience wrapper around :func:`extract_price_forecast_with_timestamps`
    for callers that do not need the per-period start times; see that function
    for the supported sensor formats and their priority order.

    Returns:
        Tuple of (prices list, interval in minutes)
    """
    prices, _start_times, interval = extract_price_forecast_with_timestamps(state)
    return prices, interval


def extract_price_forecast(state: State) -> list[float]:
    """Extract price forecast from a Home Assistant price state."""
    prices, _ = extract_price_forecast_with_interval(state)
    return prices


def synthesize_timestamps(
    now: datetime, interval_minutes: int, count: int
) -> list[datetime]:
    """Synthesize UTC start timestamps for price entries without explicit timestamps.

    Floors 'now' to the nearest interval boundary and generates 'count' timestamps.
    """
    total_minutes = now.hour * 60 + now.minute
    floored_minutes = (total_minutes // interval_minutes) * interval_minutes
    floor_dt = now.replace(
        hour=floored_minutes // 60,
        minute=floored_minutes % 60,
        second=0,
        microsecond=0,
    )
    return [floor_dt + timedelta(minutes=i * interval_minutes) for i in range(count)]


def _fill_missing_timestamps(
    timestamps: list[datetime | None], interval_minutes: int, now: datetime
) -> list[datetime]:
    """Fill None entries in a timestamp list using surrounding real timestamps."""
    anchor_idx = next((i for i, ts in enumerate(timestamps) if ts is not None), None)
    if anchor_idx is None:
        return synthesize_timestamps(now, interval_minutes, len(timestamps))
    anchor = timestamps[anchor_idx]
    if anchor is None:  # unreachable; explicit so it also holds under python -O
        return synthesize_timestamps(now, interval_minutes, len(timestamps))
    return [
        ts
        if ts is not None
        else anchor + timedelta(minutes=(i - anchor_idx) * interval_minutes)
        for i, ts in enumerate(timestamps)
    ]


def extract_price_forecast_with_timestamps(
    state: State,
) -> tuple[list[float], list[datetime], int]:
    """Extract a price forecast in EUR/kWh with UTC start times from a price state.

    Thin wrapper that converts whatever :func:`_extract_price_forecast_raw`
    found into EUR/kWh. Scaling lives here rather than in the individual
    formats so that a €/MWh sensor is treated identically no matter which
    attribute it publishes — including the bare state value, which used to be
    handed to the optimizer unscaled.

    Returns:
        Tuple of (prices in EUR/kWh, start_times_utc, interval_minutes)
    """
    prices, start_times, interval = _extract_price_forecast_raw(state)
    scale = price_unit_scale(state, prices)
    if scale != 1.0:
        prices = [price * scale for price in prices]
    return _drop_elapsed_periods(prices, start_times, interval)


def _drop_elapsed_periods(
    prices: list[float],
    start_times: list[datetime],
    interval_minutes: int,
) -> tuple[list[float], list[datetime], int]:
    """Drop price periods that have already ended.

    Each individual format skips elapsed entries as it reads them, but only for
    the attribute it treats as "today" — an integration that has rolled over at
    midnight, so that what it still publishes as tomorrow is now today, hands
    back a forecast that starts hours in the past. So does a back-filled
    timestamp, which is extrapolated from the first entry that carried one and
    can therefore land before it.

    Enforcing it once here, where every caller comes through, is what makes the
    guarantee hold: the optimizer builds its whole step grid from these
    timestamps, and a past entry silently moves the horizon into history —
    planning against yesterday's prices, resampling PV and consumption onto
    windows their forecasts do not cover (which flattens them to a single
    repeated value), and pinning the period start so the boundary and
    mid-period triggers stop firing altogether (issue #187).

    Everything elapsed is dropped rather than only a leading run of it, since
    no caller has any use for a period that is over. An empty result is left
    empty: the coordinator already falls back to the historical price model,
    which is the honest answer for a sensor whose every period has passed.
    """
    now = dt_util.utcnow()
    period = timedelta(minutes=interval_minutes)
    kept = [(price, ts) for price, ts in zip(prices, start_times) if ts + period > now]
    if len(kept) == len(prices):
        return prices, start_times, interval_minutes
    return [price for price, _ in kept], [ts for _, ts in kept], interval_minutes


def _extract_price_forecast_raw(
    state: State,
) -> tuple[list[float], list[datetime], int]:
    """Extract a price forecast in the sensor's own unit, with UTC start times.

    Priority order (highest to lowest):
    1. net_prices_today/tomorrow  — timestamp-based skip, interval auto-detected
    2. raw_today/raw_tomorrow WITH per-entry timestamps — timestamp-based skip
    2.5 today_hours/tomorrow_hours (OMIE) — hour-keyed dicts
    3. forecast_prices             — no skip-past, interval from timestamps or 60 min
    4. forecast (generic)          — no skip-past, interval from timestamps or 60 min
    5. raw_today/raw_tomorrow WITHOUT timestamps — index-based skip
    6. today/tomorrow              — index-based skip
    7. current state value         — last resort

    Timestamp-bearing formats (1 & 2) are preferred because they allow accurate
    interval detection and correct exclusion of elapsed price periods.
    Timestamps are synthesized for formats that carry no explicit start times.

    Returns:
        Tuple of (prices, start_times_utc, interval_minutes)
    """
    now = dt_util.utcnow()

    # Pre-compute raw_today/raw_tomorrow once; used in priorities 2 and 5.
    raw_today_list = state.attributes.get("raw_today")
    raw_today_list = raw_today_list if isinstance(raw_today_list, list) else []
    raw_tomorrow_list = state.attributes.get("raw_tomorrow")
    raw_tomorrow_list = raw_tomorrow_list if isinstance(raw_tomorrow_list, list) else []
    _raw_ref = raw_today_list or raw_tomorrow_list
    _raw_first_has_ts = bool(
        _raw_ref
        and isinstance(_raw_ref[0], dict)
        and (
            _raw_ref[0].get("start")
            or _raw_ref[0].get("from")
            or _raw_ref[0].get("time")
        )
    )

    # Priority 1: net_prices_today/tomorrow — these carry per-entry timestamps
    interval_forecast: list[float] = []
    interval_timestamps: list[datetime | None] = []
    detected_interval = 60

    def _extend_with_timestamps(entries: Any, *, skip_past: bool = False) -> bool:
        nonlocal detected_interval
        if not isinstance(entries, (list, tuple)):
            return False

        interval = _detect_interval_from_entries(entries)
        if interval != 60:
            detected_interval = interval

        added = False
        for entry in entries:
            ts: datetime | None = None
            if isinstance(entry, dict):
                start = entry.get("start") or entry.get("from") or entry.get("time")
                if isinstance(start, str):
                    parsed = dt_util.parse_datetime(start)
                    if parsed is not None:
                        ts = dt_util.as_utc(parsed)
                elif isinstance(start, datetime):
                    ts = dt_util.as_utc(start)
                if ts is not None and (
                    skip_past and ts + timedelta(minutes=detected_interval) <= now
                ):
                    continue

            price = _normalize_price_value(entry)
            if price is not None:
                interval_forecast.append(price)
                interval_timestamps.append(ts)
                added = True
        return added

    _extend_with_timestamps(state.attributes.get("net_prices_today"), skip_past=True)
    _extend_with_timestamps(state.attributes.get("net_prices_tomorrow"))

    if interval_forecast:
        filled = _fill_missing_timestamps(interval_timestamps, detected_interval, now)
        return interval_forecast, filled, detected_interval

    # Priority 2: raw_today/raw_tomorrow WITH timestamps (before forecast_prices so
    # that sensors providing both attributes use the timestamp-bearing format, which
    # gives the correct sub-hourly interval instead of defaulting to 60 min).
    if _raw_ref and _raw_first_has_ts:
        interval_forecast = []
        interval_timestamps = []
        detected_interval = 60
        _extend_with_timestamps(raw_today_list, skip_past=True)
        _extend_with_timestamps(raw_tomorrow_list)
        if interval_forecast:
            filled = _fill_missing_timestamps(
                interval_timestamps, detected_interval, now
            )
            return interval_forecast, filled, detected_interval

    # Priority 2.5: OMIE-style hour-keyed dicts (today_hours/tomorrow_hours)
    hours_result = _extract_hours_dict_forecast(state, now)
    if hours_result is not None:
        return hours_result

    # Priority 3: forecast_prices — skip elapsed periods and keep real
    # timestamps when entries carry them; plain value lists are taken as-is
    forecast_attr = state.attributes.get("forecast_prices")
    if isinstance(forecast_attr, (list, tuple)):
        if _first_entry_has_timestamp(forecast_attr):
            interval_forecast = []
            interval_timestamps = []
            detected_interval = 60
            _extend_with_timestamps(forecast_attr, skip_past=True)
            if interval_forecast:
                filled = _fill_missing_timestamps(
                    interval_timestamps, detected_interval, now
                )
                return interval_forecast, filled, detected_interval
        interval = _detect_interval_from_entries(forecast_attr)
        forecast: list[float] = []
        for entry in forecast_attr:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return (
                forecast,
                synthesize_timestamps(now, interval, len(forecast)),
                interval,
            )

    # Priority 4: Generic forecast — same timestamp-aware skip
    generic_forecast = state.attributes.get("forecast")
    if isinstance(generic_forecast, (list, tuple)):
        if _first_entry_has_timestamp(generic_forecast):
            interval_forecast = []
            interval_timestamps = []
            detected_interval = 60
            _extend_with_timestamps(generic_forecast, skip_past=True)
            if interval_forecast:
                filled = _fill_missing_timestamps(
                    interval_timestamps, detected_interval, now
                )
                return interval_forecast, filled, detected_interval
        interval = _detect_interval_from_entries(generic_forecast)
        forecast = []
        for entry in generic_forecast:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return (
                forecast,
                synthesize_timestamps(now, interval, len(forecast)),
                interval,
            )

    # Priority 5: raw_today/raw_tomorrow WITHOUT timestamps — index-based skip
    if _raw_ref and not _raw_first_has_ts:
        now_local = dt_util.now()
        raw_interval = _detect_interval_from_entries(_raw_ref)
        skip_index = _skip_index_since_local_midnight(now_local, raw_interval)
        forecast = []
        for entry in raw_today_list[skip_index:]:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        for entry in raw_tomorrow_list:
            price = _normalize_price_value(entry)
            if price is not None:
                forecast.append(price)
        if forecast:
            return (
                forecast,
                synthesize_timestamps(now, raw_interval, len(forecast)),
                raw_interval,
            )

    # today/tomorrow — use interval-aware skip index
    now_local = dt_util.now()
    today_attr = state.attributes.get("today")
    tomorrow_attr = state.attributes.get("tomorrow")
    interval = _detect_interval_from_entries(today_attr)
    if interval == 60:
        interval = _detect_interval_from_entries(tomorrow_attr)
    skip_index = _skip_index_since_local_midnight(now_local, interval)
    combined: list[Any] = []
    if isinstance(today_attr, list):
        combined.extend(today_attr[skip_index:])
    if isinstance(tomorrow_attr, list):
        combined.extend(tomorrow_attr)
    forecast = []
    for entry in combined:
        price = _normalize_price_value(entry)
        if price is not None:
            forecast.append(price)
    if forecast:
        return forecast, synthesize_timestamps(now, interval, len(forecast)), interval

    # Last resort: current state value
    try:
        price = float(state.state)
    except (TypeError, ValueError):
        return [], [], 60
    return [price], synthesize_timestamps(now, 60, 1), 60


def compute_step_durations_hours(
    start_times: list[datetime],
    interval_minutes: int,
    now: datetime,
) -> list[float]:
    """Compute per-step durations aligned to price interval boundaries.

    The first step covers the remaining time until the next price boundary.
    All subsequent steps are full intervals. This synchronizes the DP time
    steps with the actual price periods so no resampling artefacts occur.

    Args:
        start_times: UTC start time for each price period (len >= 1)
        interval_minutes: Native interval of the price sensor in minutes
        now: Current UTC time

    Returns:
        List of step durations in hours, same length as start_times
    """
    full_h = interval_minutes / 60.0
    min_h = 1.0 / 60.0  # Minimum 1-minute step

    if len(start_times) <= 1:
        return [full_h] * len(start_times)

    first_h = (start_times[1] - now).total_seconds() / 3600.0
    first_h = max(min_h, min(first_h, full_h))

    return [first_h] + [full_h] * (len(start_times) - 1)


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


def resample_to_steps(
    values: list[float],
    source_start: datetime,
    source_interval_minutes: float,
    step_starts: list[datetime],
    step_durations_hours: list[float],
) -> list[float]:
    """Project a time-anchored series onto the optimizer's actual step windows.

    ``resample_forecast`` converts between interval lengths but assumes both
    series begin at the same instant. They do not: the forecast pipeline emits
    quarter-hourly values anchored to the current quarter, while DP step k is
    anchored to a price-period boundary and step 0 is the (shortened) remainder
    of the current period. With quarter-hourly prices the two anchors coincide,
    but with hourly prices a run starting at HH:30 shifted the whole PV and
    consumption series 30 minutes late — every step got the production of the
    following half hour, which matters most around the dawn/dusk ramps and the
    midday peak. The same applies to a feed-in sensor whose native interval
    differs from the grid price sensor's.

    Each step takes the duration-weighted average of the source intervals it
    overlaps, so the result is mean-preserving for powers and a proper
    time-average for prices. Steps beyond the end of the source series are not
    produced: the returned list stops at the last step with any overlap, and
    the caller pads the remainder with whatever its own fallback is (zero for
    PV, the last value for consumption).

    Args:
        values: Source series, one value per source interval.
        source_start: UTC start of ``values[0]``.
        source_interval_minutes: Length of each source interval in minutes.
        step_starts: UTC start of each target step.
        step_durations_hours: Length of each target step in hours.

    Returns:
        One value per covered step, in step order.
    """
    if not values or not step_starts or source_interval_minutes <= 0:
        return []

    source_s = float(source_interval_minutes) * 60.0
    # Work in seconds relative to source_start so the arithmetic stays exact
    # for the sub-minute offsets that step 0 can have.
    result: list[float] = []
    total_source_s = len(values) * source_s

    for i, step_start in enumerate(step_starts):
        duration_h = (
            step_durations_hours[i]
            if i < len(step_durations_hours)
            else (step_durations_hours[-1] if step_durations_hours else 0.25)
        )
        window_start = (step_start - source_start).total_seconds()
        window_end = window_start + duration_h * 3600.0
        # Clip to the source range; a step reaching past the end is still
        # produced from the part that is covered.
        clipped_start = max(0.0, window_start)
        clipped_end = min(total_source_s, window_end)
        if clipped_end <= clipped_start:
            break

        first = int(clipped_start // source_s)
        last = min(len(values) - 1, int((clipped_end - 1e-9) // source_s))
        weighted_sum = 0.0
        total_weight = 0.0
        for j in range(first, last + 1):
            overlap = min(clipped_end, (j + 1) * source_s) - max(
                clipped_start, j * source_s
            )
            if overlap > 0:
                weighted_sum += values[j] * overlap
                total_weight += overlap
        if total_weight <= 0:
            break
        result.append(weighted_sum / total_weight)

    return result


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
    state = usable_state(hass, entity_id)
    if state is None:
        return default

    return safe_float(state.state, default)


def extract_pv_forecast_series(states: list[State]) -> list[tuple[datetime, float]]:
    """Extract a PV power forecast series (kW) from PV forecast sensor states.

    Supports the attribute formats of common PV forecast integrations:

    - **Solcast** (HACS ``solcast_solar``): ``detailedForecast`` /
      ``detailedHourly`` attributes — lists of
      ``{"period_start": <datetime|ISO string>, "pv_estimate": <kW>}``
      entries at 30-minute or hourly resolution. Pass both the *Forecast
      Today* and *Forecast Tomorrow* sensors to cover the full horizon.
    - **Volcast** (HACS ``volcast``): ``detailedHourly`` /
      ``detailedForecast`` attributes with ``period_start`` +
      ``power_kw`` (hourly) or ``power_w`` (5-minute) entries.
    - **Generic**: a ``forecast`` attribute with the same entry layout;
      timestamp keys ``period_start``/``datetime``/``start``/``time`` and
      value keys ``pv_estimate``/``pv_power``/``power``/``power_kw`` (kW)
      or ``watts``/``power_w`` (W) are accepted.
    - **Forecast.Solar**: a ``watts`` attribute mapping timestamp → W.

    The native resolution of the source data is preserved: each entry
    becomes one point in the series. Entries sharing the same timestamp
    (e.g. the same hour from both ``detailedForecast`` and
    ``detailedHourly``) are averaged.

    Returns:
        List of (period start UTC, mean PV power in kW), sorted by time.
    """
    buckets: dict[datetime, list[float]] = {}

    def _parse_ts(raw: Any) -> datetime | None:
        if isinstance(raw, datetime):
            ts = raw
        elif isinstance(raw, str):
            parsed = dt_util.parse_datetime(raw)
            if parsed is None:
                return None
            ts = parsed
        else:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.UTC)
        return dt_util.as_utc(ts).replace(second=0, microsecond=0)

    def _entry_power_kw(entry: dict[str, Any]) -> float | None:
        # kW keys (Solcast pv_estimate, Volcast power_kw, generic)
        for key in ("pv_estimate", "pv_power", "power", "power_kw"):
            if key in entry:
                value = _normalize_price_value(entry[key])
                return value if value is None else max(0.0, value)
        # W keys (Forecast.Solar watts, Volcast power_w)
        for key in ("watts", "power_w"):
            if key in entry:
                value = _normalize_price_value(entry[key])
                return value if value is None else max(0.0, value / 1000.0)
        return None

    def _add_entries(entries: Any) -> None:
        if not isinstance(entries, (list, tuple)):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_ts = (
                entry.get("period_start")
                or entry.get("datetime")
                or entry.get("start")
                or entry.get("time")
            )
            ts = _parse_ts(raw_ts)
            power_kw = _entry_power_kw(entry)
            if ts is not None and power_kw is not None:
                buckets.setdefault(ts, []).append(power_kw)

    for state in states:
        if not state_has_value(state):
            continue
        attrs = state.attributes
        for attr_key in ("detailedForecast", "detailedHourly", "forecast"):
            _add_entries(attrs.get(attr_key))
        # Forecast.Solar style: {"watts": {"<timestamp>": <W>, ...}}
        watts = attrs.get("watts")
        if isinstance(watts, dict):
            for raw_ts, raw_w in watts.items():
                ts = _parse_ts(raw_ts)
                value = _normalize_price_value(raw_w)
                if ts is not None and value is not None:
                    buckets.setdefault(ts, []).append(max(0.0, value / 1000.0))

    return sorted((ts, sum(vals) / len(vals)) for ts, vals in buckets.items())


def _solar_position(
    dt_utc: datetime, latitude: float, longitude: float
) -> tuple[float, float]:
    """Return (elevation_deg, azimuth_deg from North clockwise) for the sun.

    Uses Spencer (1971) declination and a simplified equation of time.
    Accuracy: ~0.5° for latitudes 30–70°N, sufficient for hourly PV modelling.
    """
    day_of_year = dt_utc.timetuple().tm_yday
    hour_utc = dt_utc.hour + dt_utc.minute / 60.0

    # Solar declination (degrees)
    declination = 23.45 * math.sin(math.radians(360.0 / 365.0 * (day_of_year - 81)))

    # Equation of time (minutes)
    b_rad = math.radians(360.0 / 365.0 * (day_of_year - 81))
    eot_min = (
        9.87 * math.sin(2 * b_rad) - 7.53 * math.cos(b_rad) - 1.5 * math.sin(b_rad)
    )

    # Solar time and hour angle
    solar_time = hour_utc + longitude / 15.0 + eot_min / 60.0
    hour_angle = (solar_time - 12.0) * 15.0  # degrees; negative = morning

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    ha_rad = math.radians(hour_angle)

    sin_elev = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(
        dec_rad
    ) * math.cos(ha_rad)
    elevation_deg = math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))

    cos_elev = math.cos(math.radians(elevation_deg))
    if cos_elev < 1e-6:
        return elevation_deg, 180.0  # sun at zenith — azimuth undefined

    cos_az = (
        math.sin(dec_rad) - math.sin(math.radians(elevation_deg)) * math.sin(lat_rad)
    ) / (cos_elev * math.cos(lat_rad))
    azimuth_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    if hour_angle > 0:  # afternoon: sun is in the west
        azimuth_deg = 360.0 - azimuth_deg

    return elevation_deg, azimuth_deg


def _poa_irradiance(
    ghi: float,
    dni: float,
    diffuse: float,
    sun_elevation_deg: float,
    sun_azimuth_deg: float,
    tilt_deg: float,
    panel_azimuth_deg: float,
    albedo: float = 0.2,
) -> float:
    """Compute Plane of Array (POA) irradiance using the isotropic diffuse model.

    POA = beam_direct + isotropic_diffuse + ground_reflected

    Args:
        ghi: Global Horizontal Irradiance (W/m²)
        dni: Direct Normal Irradiance (W/m²)
        diffuse: Diffuse Horizontal Irradiance (W/m²)
        sun_elevation_deg: Solar elevation above horizon (degrees)
        sun_azimuth_deg: Solar azimuth from North, clockwise (degrees)
        tilt_deg: Panel tilt from horizontal (degrees)
        panel_azimuth_deg: Panel azimuth from North, clockwise (degrees; 180 = south)
        albedo: Ground reflectance (default 0.2 for grass/concrete)

    Returns:
        POA irradiance in W/m²
    """
    if sun_elevation_deg <= 0:
        return 0.0

    tilt_rad = math.radians(tilt_deg)
    zenith_rad = math.radians(90.0 - sun_elevation_deg)
    delta_az_rad = math.radians(sun_azimuth_deg - panel_azimuth_deg)

    # Angle of incidence on the tilted surface
    cos_aoi = max(
        0.0,
        math.cos(zenith_rad) * math.cos(tilt_rad)
        + math.sin(zenith_rad) * math.sin(tilt_rad) * math.cos(delta_az_rad),
    )

    beam_poa = dni * cos_aoi
    diffuse_poa = diffuse * (1.0 + math.cos(tilt_rad)) / 2.0
    reflected_poa = ghi * albedo * (1.0 - math.cos(tilt_rad)) / 2.0

    return max(0.0, beam_poa + diffuse_poa + reflected_poa)


def calculate_pv_forecast(
    solar_radiation_wm2: list[float],
    peak_power_kwp: float,
    orientation_deg: float = 180,  # 180 = south
    tilt_deg: float = 35,
    efficiency_factor: float = 0.85,
    dni_forecast: list[float] | None = None,
    diffuse_forecast: list[float] | None = None,
    timestamps_utc: list[datetime] | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> list[float]:
    """Calculate PV production forecast from solar radiation.

    When dni_forecast, diffuse_forecast, timestamps_utc, latitude, and longitude
    are all provided, uses a proper Plane-of-Array (POA) transposition model with
    real solar geometry. This correctly accounts for the panel tilt and orientation
    relative to the sun's position, which can increase estimated yield by 30–50%
    compared to using Global Horizontal Irradiance (GHI) alone in winter/spring.

    Falls back to a simplified GHI-based model when any of the above are missing.

    Args:
        solar_radiation_wm2: GHI forecast in W/m²
        peak_power_kwp: PV system peak power in kWp
        orientation_deg: Panel azimuth from North, clockwise (180 = south)
        tilt_deg: Panel tilt from horizontal (degrees)
        efficiency_factor: System efficiency (inverter, wiring, soiling, etc.)
        dni_forecast: Direct Normal Irradiance forecast in W/m² (optional)
        diffuse_forecast: Diffuse Horizontal Irradiance in W/m² (optional)
        timestamps_utc: UTC datetime for each forecast hour (optional)
        latitude: Site latitude in degrees (optional)
        longitude: Site longitude in degrees (optional)

    Returns:
        PV production forecast in kW
    """
    if peak_power_kwp <= 0:
        return [0.0] * len(solar_radiation_wm2)

    use_poa = (
        dni_forecast is not None
        and diffuse_forecast is not None
        and timestamps_utc is not None
        and latitude is not None
        and longitude is not None
    )

    if use_poa:
        assert dni_forecast is not None
        assert diffuse_forecast is not None
        assert timestamps_utc is not None
        assert latitude is not None
        assert longitude is not None
        forecast = []
        for i, ghi in enumerate(solar_radiation_wm2):
            if (
                i >= len(timestamps_utc)
                or i >= len(dni_forecast)
                or i >= len(diffuse_forecast)
            ):
                forecast.append(0.0)
                continue
            # Use midpoint of the hour for representative solar position
            dt_mid = timestamps_utc[i].replace(minute=30, second=0, microsecond=0)
            elev, azim = _solar_position(dt_mid, latitude, longitude)
            poa = _poa_irradiance(
                ghi,
                dni_forecast[i],
                diffuse_forecast[i],
                elev,
                azim,
                tilt_deg,
                orientation_deg,
            )
            power_kw = poa / 1000.0 * peak_power_kwp * efficiency_factor
            forecast.append(max(0.0, power_kw))
        return forecast

    # Fallback: simplified GHI-based model
    orientation_factor = 1.0
    if orientation_deg < 135 or orientation_deg > 225:
        deviation = min(abs(orientation_deg - 180), abs(orientation_deg - 180 + 360))
        orientation_factor = max(0.5, 1.0 - deviation / 180)

    # Clamped: the linear penalty goes negative past 135° of tilt, which would
    # turn the fallback estimate into negative production. The UI cannot reach
    # that, but this function is public and also serves the diagnostics path.
    tilt_factor = max(0.1, 1.0 - abs(tilt_deg - 35) * 0.01)

    forecast = []
    for radiation in solar_radiation_wm2:
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

    This function is a cold-start fallback only.  It is called by
    ConsumptionForecastModel when no historical data has been learned yet from
    the HA recorder (i.e. _hourly_pattern is empty for the requested slot).
    Once the recorder has accumulated enough statistics the learned pattern
    takes over and this function is no longer invoked for those slots.

    The built-in pattern reflects a typical Dutch household
    (≈3500 kWh/year, ~0.4 kW average) with a morning and evening peak.
    Making this user-configurable would add UI complexity for a value that
    is replaced automatically within the first days of operation.

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
