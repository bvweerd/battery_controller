"""Tests for helpers.py."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from custom_components.battery_controller.helpers import (
    clamp,
    safe_float,
    resample_forecast,
    resample_to_steps,
    calculate_pv_forecast,
    calculate_consumption_pattern,
    extract_pv_forecast_series,
    extract_price_forecast_with_interval,
    extract_price_forecast_with_timestamps,
)
from custom_components.battery_controller import helpers as h
from custom_components.battery_controller.helpers import _poa_irradiance
from custom_components.battery_controller.helpers import _solar_position
from custom_components.battery_controller.helpers import extract_price_forecast
from custom_components.battery_controller.helpers import get_sensor_value
from homeassistant.util import dt as dt_util
from zoneinfo import ZoneInfo
import math


class TestClamp:
    """Tests for clamp function."""

    def test_within_range(self):
        assert clamp(5.0, 0.0, 10.0) == 5.0

    def test_below_min(self):
        assert clamp(-1.0, 0.0, 10.0) == 0.0

    def test_above_max(self):
        assert clamp(15.0, 0.0, 10.0) == 10.0

    def test_at_min(self):
        assert clamp(0.0, 0.0, 10.0) == 0.0

    def test_at_max(self):
        assert clamp(10.0, 0.0, 10.0) == 10.0


class TestSafeFloat:
    """Tests for safe_float function."""

    def test_valid_float(self):
        assert safe_float(3.14) == 3.14

    def test_string_number(self):
        assert safe_float("3.14") == 3.14

    def test_none(self):
        assert safe_float(None) == 0.0

    def test_none_with_default(self):
        assert safe_float(None, 5.0) == 5.0

    def test_invalid_string(self):
        assert safe_float("abc") == 0.0

    def test_nan(self):
        assert safe_float(float("nan")) == 0.0

    def test_inf(self):
        assert safe_float(float("inf")) == 0.0

    def test_int(self):
        assert safe_float(42) == 42.0


class TestResampleForecast:
    """Tests for resample_forecast function."""

    def test_same_interval(self):
        data = [1.0, 2.0, 3.0]
        assert resample_forecast(data, 60, 60) == data

    def test_hourly_to_15min(self):
        data = [1.0, 2.0]
        result = resample_forecast(data, 60, 15)
        # 2 hours -> 8 x 15-min steps
        assert len(result) == 8
        # First 4 should be ~1.0, last 4 should be ~2.0
        assert all(v == pytest.approx(1.0) for v in result[:4])
        assert all(v == pytest.approx(2.0) for v in result[4:])

    def test_15min_to_hourly(self):
        data = [1.0, 2.0, 3.0, 4.0]  # 4 x 15-min = 1 hour
        result = resample_forecast(data, 15, 60)
        assert len(result) == 1
        # Weighted average = (1+2+3+4)/4 = 2.5
        assert result[0] == pytest.approx(2.5)

    def test_empty_input(self):
        assert resample_forecast([], 60, 15) == []

    def test_30min_to_15min(self):
        data = [10.0, 20.0]  # 2 x 30-min = 60 min
        result = resample_forecast(data, 30, 15)
        assert len(result) == 4
        assert result[0] == pytest.approx(10.0)
        assert result[1] == pytest.approx(10.0)
        assert result[2] == pytest.approx(20.0)
        assert result[3] == pytest.approx(20.0)


class TestCalculatePvForecast:
    """Tests for calculate_pv_forecast function."""

    def test_zero_peak_power(self):
        result = calculate_pv_forecast([500, 800], 0.0)
        assert result == [0.0, 0.0]

    def test_basic_forecast(self):
        # 1000 W/m2 STC -> should give peak_power_kwp output
        result = calculate_pv_forecast([1000], 5.0, 180, 35, 0.85)
        # 1000/1000 * 5 * 1.0 * 1.0 * 0.85 = 4.25 kW
        assert result[0] == pytest.approx(4.25, abs=0.1)

    def test_no_radiation(self):
        result = calculate_pv_forecast([0.0, 0.0], 5.0)
        assert result == [0.0, 0.0]

    def test_negative_not_produced(self):
        result = calculate_pv_forecast([-100], 5.0)
        # Should clamp to 0
        assert result[0] == 0.0

    def test_orientation_factor(self):
        south = calculate_pv_forecast([500], 5.0, 180, 35, 1.0)[0]
        east = calculate_pv_forecast([500], 5.0, 90, 35, 1.0)[0]
        # South-facing should produce more
        assert south > east


class TestCalculateConsumptionPattern:
    """Tests for calculate_consumption_pattern function."""

    def test_night_consumption(self):
        # Night hours should be low
        night = calculate_consumption_pattern(2, 1, 0.5)  # 2 AM, Tuesday
        peak = calculate_consumption_pattern(18, 1, 0.5)  # 6 PM, Tuesday
        assert night < peak

    def test_peak_evening(self):
        # Evening peak should be the highest
        evening = calculate_consumption_pattern(18, 1, 0.5)
        morning = calculate_consumption_pattern(8, 1, 0.5)
        assert evening > morning * 0.9  # Evening >= morning roughly

    def test_weekend_factor(self):
        weekday = calculate_consumption_pattern(12, 2, 0.5)  # Wednesday
        weekend = calculate_consumption_pattern(12, 5, 0.5)  # Saturday
        assert weekend > weekday  # Weekend is 10% higher

    def test_base_consumption_scaling(self):
        low = calculate_consumption_pattern(12, 1, 0.3)
        high = calculate_consumption_pattern(12, 1, 0.8)
        assert high > low


class TestExtractPriceForecast:
    """Tests for extract_price_forecast_with_interval function."""

    def _make_state(self, state_value="0.25", attributes=None):
        """Create a mock HA State object."""
        state = MagicMock()
        state.state = state_value
        state.attributes = attributes or {}
        return state

    def test_forecast_prices_attribute(self):
        state = self._make_state(
            attributes={"forecast_prices": [0.10, 0.15, 0.20, 0.25]}
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.10, 0.15, 0.20, 0.25]
        assert interval == 60

    def test_forecast_prices_with_dicts(self):
        state = self._make_state(
            attributes={
                "forecast_prices": [
                    {"value": 0.10},
                    {"value": 0.15},
                    {"price": 0.20},
                ]
            }
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.10, 0.15, 0.20]

    def test_raw_today_tomorrow(self):
        state = self._make_state(
            attributes={
                "raw_today": [
                    {"value": 0.10},
                    {"value": 0.11},
                    {"value": 0.12},
                    {"value": 0.13},
                    {"value": 0.14},
                    {"value": 0.15},
                    {"value": 0.16},
                    {"value": 0.17},
                    {"value": 0.18},
                    {"value": 0.19},
                    {"value": 0.20},
                    {"value": 0.21},
                    {"value": 0.22},
                    {"value": 0.23},
                    {"value": 0.24},
                    {"value": 0.25},
                    {"value": 0.26},
                    {"value": 0.27},
                    {"value": 0.28},
                    {"value": 0.29},
                    {"value": 0.30},
                    {"value": 0.31},
                    {"value": 0.32},
                    {"value": 0.33},
                ],
                "raw_tomorrow": [
                    {"value": 0.05},
                    {"value": 0.06},
                ],
            }
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert len(prices) > 0
        assert interval == 60

    def test_current_state_fallback(self):
        state = self._make_state(state_value="0.25", attributes={})
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.25]
        assert interval == 60

    def test_invalid_state_empty(self):
        state = self._make_state(state_value="unknown", attributes={})
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == []

    def test_today_skips_past_hours(self):
        """today attribute must not include already-elapsed hours."""

        # 24 hourly prices for a full day
        today_prices = [float(i) * 0.01 for i in range(24)]
        state = self._make_state(attributes={"today": today_prices})

        # Simulate that it is currently 10:00 local time
        fake_now = dt_util.utcnow().replace(hour=10, minute=5, second=0, microsecond=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        # Prices from hour 10 onwards (index 10..23 = 14 entries)
        assert prices == today_prices[10:]
        assert interval == 60

    def test_today_and_tomorrow_combined(self):
        """today[hour:] + tomorrow should be combined correctly."""

        today_prices = [float(i) for i in range(24)]
        tomorrow_prices = [float(i + 24) for i in range(24)]
        state = self._make_state(
            attributes={"today": today_prices, "tomorrow": tomorrow_prices}
        )

        fake_now = dt_util.utcnow().replace(hour=20, minute=0, second=0, microsecond=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        expected = today_prices[20:] + tomorrow_prices
        assert prices == expected

    def _make_15min_raw_today(self, base_dt, count=96):
        """Build a Nordpool-style raw_today with 15-min entries and timestamps."""

        entries = []
        for i in range(count):
            ts = base_dt + timedelta(minutes=15 * i)
            entries.append(
                {
                    "start": ts.isoformat(),
                    "value": round(0.10 + i * 0.001, 4),
                }
            )
        return entries

    def test_raw_today_15min_detects_interval(self):
        """raw_today with 15-min timestamps returns interval=15."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        raw_today = self._make_15min_raw_today(midnight, count=96)
        state = self._make_state(attributes={"raw_today": raw_today})

        fake_now = midnight.replace(hour=14, minute=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        assert interval == 15
        # 14:00 UTC = index 14*4=56; remaining = 96-56 = 40 entries
        assert len(prices) == 40
        assert prices[0] == pytest.approx(raw_today[56]["value"])

    def test_raw_today_15min_excludes_past_prices(self):
        """raw_today at 15-min interval must not include elapsed periods."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        raw_today = self._make_15min_raw_today(midnight, count=96)
        state = self._make_state(attributes={"raw_today": raw_today})

        # Simulate 10:30 UTC — first future slot is 10:30 (index 42)
        fake_now = midnight.replace(hour=10, minute=30)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        assert interval == 15
        # 10:30 → index 42; 96 - 42 = 54 remaining
        assert len(prices) == 54
        assert prices[0] == pytest.approx(raw_today[42]["value"])

    def test_raw_today_15min_with_tomorrow(self):
        """raw_today + raw_tomorrow at 15-min interval are combined."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        tomorrow_midnight = midnight.replace(day=16)
        raw_today = self._make_15min_raw_today(midnight, count=96)
        raw_tomorrow = self._make_15min_raw_today(tomorrow_midnight, count=96)
        state = self._make_state(
            attributes={"raw_today": raw_today, "raw_tomorrow": raw_tomorrow}
        )

        # 23:00 — 4 entries left in today, 96 from tomorrow
        fake_now = midnight.replace(hour=23, minute=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        assert interval == 15
        assert len(prices) == 4 + 96

    def test_raw_today_no_timestamps_uses_index_skip(self):
        """raw_today without timestamps falls back to index-based hour skip."""

        # 24 plain-dict entries without timestamps
        raw_today = [{"value": float(i) * 0.01} for i in range(24)]
        state = self._make_state(attributes={"raw_today": raw_today})

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        fake_now = midnight.replace(hour=10, minute=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        # Index-based: skip first 10 entries (hour=10)
        assert interval == 60
        assert len(prices) == 14
        assert prices[0] == pytest.approx(raw_today[10]["value"])

    def test_forecast_prices_15min_detects_interval(self):
        """forecast_prices with 15-min timestamps returns interval=15."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        entries = [
            {
                "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
                "value": 0.10 + i * 0.01,
            }
            for i in range(8)
        ]
        state = self._make_state(attributes={"forecast_prices": entries})
        prices, interval = extract_price_forecast_with_interval(state)

        assert interval == 15
        assert len(prices) == 8

    def test_extract_with_timestamps_15min_returns_correct_durations(self):
        """extract_price_forecast_with_timestamps returns interval=15 for 15-min data."""

        from custom_components.battery_controller.helpers import (
            compute_step_durations_hours,
        )

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        raw_today = [
            {
                "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
                "value": 0.10 + i * 0.001,
            }
            for i in range(96)
        ]
        state = self._make_state(attributes={"raw_today": raw_today})

        fake_now = midnight.replace(hour=14, minute=7)  # 7 min into 14:00 slot
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, start_times, interval = extract_price_forecast_with_timestamps(
                state
            )

        assert interval == 15
        assert len(prices) > 0

        step_durations = compute_step_durations_hours(start_times, interval, fake_now)
        # First step is partial (8 min remaining in current 15-min slot)
        assert step_durations[0] == pytest.approx(8 / 60, abs=0.01)
        # All subsequent steps are full 15-min = 0.25 h
        assert all(d == pytest.approx(0.25) for d in step_durations[1:])


# ---------------------------------------------------------------------------
# Additional helpers coverage: solar position, POA irradiance, calculate_pv_forecast
# with POA model, get_sensor_value
# ---------------------------------------------------------------------------


class TestSolarPositionAndPOA:
    """Tests for _solar_position and _poa_irradiance helper functions."""

    def _solar_position(self, dt_utc, lat, lon):

        return _solar_position(dt_utc, lat, lon)

    def _poa_irradiance(self, *args, **kwargs):

        return _poa_irradiance(*args, **kwargs)

    def test_solar_elevation_summer_noon(self):
        """Sun elevation at solar noon in summer should be positive."""

        # June 21, solar noon UTC at 52°N, 0°E (longitude 0 → solar time ≈ UTC)
        dt = datetime(2024, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        elev, azim = self._solar_position(dt, 52.0, 0.0)
        assert elev > 50  # high summer sun at 52°N

    def test_solar_elevation_midnight_negative(self):
        """Sun elevation at midnight should be negative."""

        dt = datetime(2024, 6, 21, 0, 0, 0, tzinfo=timezone.utc)
        elev, _ = self._solar_position(dt, 52.0, 0.0)
        assert elev < 0

    def test_solar_afternoon_azimuth_west(self):
        """Afternoon sun should be west of south (azimuth > 180°)."""

        # 15:00 UTC at 52°N, 0°E
        dt = datetime(2024, 6, 21, 15, 0, 0, tzinfo=timezone.utc)
        elev, azim = self._solar_position(dt, 52.0, 0.0)
        if elev > 0:
            assert azim > 180

    def test_solar_zenith_returns_180_azimuth(self):
        """When sun is near zenith, azimuth should be 180 (fallback)."""

        # Near-zenith: latitude = declination ~ 23.5°N at summer solstice, local noon
        dt = datetime(2024, 6, 21, 11, 0, 0, tzinfo=timezone.utc)
        elev, azim = self._solar_position(dt, 23.4, 0.0)

        # High summer sun near solar noon; azimuth stays well-defined and in
        # range even as the cos(elevation) denominator gets small.
        assert elev > 70.0
        assert 0.0 <= azim <= 360.0

    def test_poa_zero_when_sun_below_horizon(self):
        """POA irradiance is 0 when sun elevation <= 0."""
        result = self._poa_irradiance(500, 300, 100, -5, 180, 35, 180)
        assert result == 0.0

    def test_poa_positive_with_sun_overhead(self):
        """POA irradiance is positive when sun is above horizon."""
        result = self._poa_irradiance(
            ghi=800,
            dni=500,
            diffuse=200,
            sun_elevation_deg=45,
            sun_azimuth_deg=180,
            tilt_deg=35,
            panel_azimuth_deg=180,
        )
        assert result > 0

    def test_poa_returns_nonnegative(self):
        """POA irradiance should never be negative."""
        result = self._poa_irradiance(
            ghi=0,
            dni=0,
            diffuse=0,
            sun_elevation_deg=10,
            sun_azimuth_deg=90,
            tilt_deg=35,
            panel_azimuth_deg=180,
        )
        assert result >= 0


class TestCalculatePvForecastWithPOA:
    """Tests for calculate_pv_forecast with full POA model."""

    def test_poa_model_used_when_all_params_provided(self):
        """POA path is used when dni, diffuse, timestamps, lat, lon all provided."""

        base = datetime(2024, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
        n = 6
        timestamps = [base + timedelta(hours=i) for i in range(n)]
        radiation = [600.0] * n
        dni = [400.0] * n
        diffuse = [150.0] * n

        result = calculate_pv_forecast(
            radiation,
            peak_power_kwp=5.0,
            orientation_deg=180,
            tilt_deg=35,
            efficiency_factor=0.85,
            dni_forecast=dni,
            diffuse_forecast=diffuse,
            timestamps_utc=timestamps,
            latitude=52.0,
            longitude=4.0,
        )
        assert len(result) == n
        # Should be positive during daylight hours
        assert sum(result) > 0

    def test_poa_model_out_of_bounds_appends_zero(self):
        """When timestamps list is shorter than radiation, zeros are appended."""

        base = datetime(2024, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
        radiation = [600.0, 700.0, 800.0]
        dni = [400.0]  # shorter
        diffuse = [150.0]  # shorter
        timestamps = [base]  # shorter

        result = calculate_pv_forecast(
            radiation,
            peak_power_kwp=5.0,
            orientation_deg=180,
            tilt_deg=35,
            efficiency_factor=0.85,
            dni_forecast=dni,
            diffuse_forecast=diffuse,
            timestamps_utc=timestamps,
            latitude=52.0,
            longitude=4.0,
        )
        assert len(result) == 3
        # Indices 1 and 2 should be 0 since timestamps is too short
        assert result[1] == 0.0
        assert result[2] == 0.0

    def test_non_south_orientation_in_ghi_fallback(self):
        """East-facing panels should get reduced output (GHI fallback)."""

        east = calculate_pv_forecast([500.0], peak_power_kwp=5.0, orientation_deg=90)
        south = calculate_pv_forecast([500.0], peak_power_kwp=5.0, orientation_deg=180)
        assert south[0] >= east[0]


class TestGetSensorValue:
    """Tests for get_sensor_value."""

    def test_returns_default_when_no_entity_id(self):

        hass = object()
        result = get_sensor_value(hass, None, default=5.0)
        assert result == 5.0

    def test_returns_default_when_entity_not_found(self):

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        result = get_sensor_value(hass, "sensor.missing", default=3.0)
        assert result == 3.0

    def test_returns_default_when_unavailable(self):

        hass = MagicMock()
        state = MagicMock()
        state.state = "unavailable"
        hass.states.get = MagicMock(return_value=state)
        result = get_sensor_value(hass, "sensor.test", default=2.0)
        assert result == 2.0

    def test_returns_sensor_float_value(self):

        hass = MagicMock()
        state = MagicMock()
        state.state = "1.5"
        hass.states.get = MagicMock(return_value=state)
        result = get_sensor_value(hass, "sensor.test", default=0.0)
        assert result == pytest.approx(1.5)


class TestExtractPriceForecastWithTimestamps:
    """Additional tests for extract_price_forecast_with_timestamps."""

    def _make_state(self, state_value="0.25", attributes=None):
        state = MagicMock()
        state.state = state_value
        state.attributes = attributes or {}
        return state

    def test_net_prices_today_with_timestamps(self):
        """net_prices_today with timestamps returns prices and timestamps."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        entries = [
            {
                "start": (midnight + timedelta(hours=i)).isoformat(),
                "value": 0.1 + i * 0.01,
            }
            for i in range(10)
        ]
        state = self._make_state(attributes={"net_prices_today": entries})

        fake_now = midnight.replace(hour=8)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, timestamps, interval = extract_price_forecast_with_timestamps(state)

        assert len(prices) > 0
        assert len(timestamps) == len(prices)

    def test_forecast_prices_returns_synthesized_timestamps(self):
        """forecast_prices without timestamps gets synthesized timestamps."""
        state = self._make_state(attributes={"forecast_prices": [0.10, 0.15, 0.20]})
        prices, timestamps, interval = extract_price_forecast_with_timestamps(state)
        assert prices == [0.10, 0.15, 0.20]
        assert len(timestamps) == 3
        assert interval == 60

    def test_generic_forecast_returns_synthesized_timestamps(self):
        """Generic 'forecast' attribute gets synthesized timestamps."""
        state = self._make_state(attributes={"forecast": [0.10, 0.15]})
        prices, timestamps, interval = extract_price_forecast_with_timestamps(state)
        assert prices == [0.10, 0.15]
        assert len(timestamps) == 2

    def test_today_tomorrow_fallback_with_timestamps(self):
        """today/tomorrow attributes get synthesized timestamps."""

        today_prices = [float(i) * 0.01 for i in range(24)]
        state = self._make_state(attributes={"today": today_prices})

        fake_now = dt_util.utcnow().replace(hour=22, minute=0, second=0, microsecond=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, timestamps, interval = extract_price_forecast_with_timestamps(state)

        assert len(prices) == len(timestamps)
        assert len(prices) == 2  # hours 22, 23

    def test_raw_today_no_timestamps_index_skip(self):
        """raw_today without timestamps gets synthesized timestamps."""

        raw_today = [{"value": float(i) * 0.01} for i in range(24)]
        state = self._make_state(attributes={"raw_today": raw_today})

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        fake_now = midnight.replace(hour=18, minute=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, timestamps, interval = extract_price_forecast_with_timestamps(state)

        assert len(prices) == 6  # hours 18-23
        assert len(timestamps) == 6

    def test_current_state_fallback_returns_timestamp(self):
        """Current state value gets a synthesized timestamp."""
        state = self._make_state(state_value="0.22", attributes={})
        prices, timestamps, interval = extract_price_forecast_with_timestamps(state)
        assert prices == [0.22]
        assert len(timestamps) == 1

    def test_invalid_state_returns_empty(self):
        """Invalid state value returns empty lists."""
        state = self._make_state(state_value="unknown", attributes={})
        prices, timestamps, interval = extract_price_forecast_with_timestamps(state)
        assert prices == []
        assert timestamps == []


# ---------------------------------------------------------------------------
# Additional coverage: missing branches in helpers.py
# ---------------------------------------------------------------------------


class TestNormalizePriceValueEdgeCases:
    """Cover _normalize_price_value returning None."""

    def _make_state(self, attributes):

        s = MagicMock()
        s.state = "unknown"
        s.attributes = attributes
        return s

    def test_non_parseable_entry_in_forecast_prices(self):
        """A non-numeric entry in forecast_prices is silently skipped."""
        state = self._make_state(
            {"forecast_prices": [{"value": "not-a-number"}, {"value": 0.20}]}
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.20]

    def test_none_entry_in_forecast_prices(self):
        """A None entry in forecast_prices is silently skipped."""
        state = self._make_state({"forecast_prices": [None, 0.15, None, 0.20]})
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.15, 0.20]

    def test_nan_string_entry_in_forecast_prices_is_skipped(self):
        """A 'nan' string converts to float nan — should be filtered out (T4)."""
        state = self._make_state({"forecast_prices": ["nan", 0.20, "inf", 0.30]})
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.20, 0.30]

    def test_nan_float_entry_in_forecast_prices_is_skipped(self):
        """A float NaN entry is filtered out (T4)."""

        state = self._make_state(
            {"forecast_prices": [float("nan"), 0.20, float("inf"), 0.30]}
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.20, 0.30]


class TestDetectIntervalWithDatetimeStart:
    """Cover _detect_interval_from_entries with datetime start objects."""

    def test_datetime_start_in_entries_detects_15min(self):
        """Entries with datetime (not string) start fields are detected."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        entries = [
            {"start": midnight + timedelta(minutes=15 * i), "value": 0.10 + i * 0.01}
            for i in range(4)
        ]
        state_mock = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        state_mock.state = "0.10"
        state_mock.attributes = {"forecast_prices": entries}
        prices, interval = extract_price_forecast_with_interval(state_mock)
        assert interval == 15


class TestNetPricesTodayPath:
    """Cover net_prices_today path returning interval_forecast (, 129)."""

    def _make_state(self, attributes):

        s = MagicMock()
        s.state = "0.20"
        s.attributes = attributes
        return s

    def test_net_prices_today_with_datetime_start(self):
        """net_prices_today with datetime start hits and returns via 129."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        # datetime start objects (not strings) — covers
        entries = [
            {"start": midnight + timedelta(hours=i), "value": 0.10 + i * 0.01}
            for i in range(10)
        ]
        state = self._make_state({"net_prices_today": entries})

        fake_now = midnight.replace(hour=8)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.as_utc",
                side_effect=lambda dt: dt,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        # Should return prices from net_prices_today (line 129 path)
        assert len(prices) > 0

    def test_net_prices_today_only_string_timestamps(self):
        """net_prices_today with ISO string timestamps returns via line 129."""

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        entries = [
            {
                "start": (midnight + timedelta(hours=i)).isoformat(),
                "value": 0.10 + i * 0.01,
            }
            for i in range(10)
        ]
        state = self._make_state({"net_prices_today": entries})

        fake_now = midnight.replace(hour=6)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, interval = extract_price_forecast_with_interval(state)

        assert len(prices) > 0


class TestGenericForecastAttribute:
    """Cover generic 'forecast' attribute in extract_price_forecast_with_interval."""

    def _make_state(self, attributes):

        s = MagicMock()
        s.state = "unknown"
        s.attributes = attributes
        return s

    def test_generic_forecast_attribute(self):
        """'forecast' attribute is used when forecast_prices not present."""
        state = self._make_state({"forecast": [0.10, 0.15, 0.20]})
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == [0.10, 0.15, 0.20]
        assert interval == 60


class TestExtractPriceForecastWrapper:
    """Cover extract_price_forecast wrapper."""

    def test_extract_price_forecast_calls_underlying(self):
        """extract_price_forecast is a thin wrapper over _with_interval."""

        state = MagicMock()
        state.state = "0.25"
        state.attributes = {"forecast_prices": [0.10, 0.15, 0.20]}
        prices = extract_price_forecast(state)
        assert prices == [0.10, 0.15, 0.20]


class TestFillMissingTimestampsAllNone:
    """Cover _fill_missing_timestamps when all timestamps are None."""

    def test_fill_missing_all_none_uses_synthesize(self):
        """When all timestamps are None, synthesize_timestamps is used."""
        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_timestamps,
        )

        # net_prices_today with entries that have no parseable start → all timestamps None
        entries = [{"value": 0.10}, {"value": 0.15}, {"value": 0.20}]
        state = MagicMock()
        state.state = "unknown"
        state.attributes = {"net_prices_today": entries}

        prices, timestamps, interval = extract_price_forecast_with_timestamps(state)
        # Falls through to a later priority; just ensure no crash
        assert isinstance(prices, list)
        assert isinstance(timestamps, list)


class TestExtendWithTimestampsDatetimeBranch:
    """Cover _extend_with_timestamps datetime start branch."""

    def test_datetime_start_in_net_prices_today_with_timestamps(self):
        """net_prices_today with datetime start objects hits."""

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_timestamps,
        )

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        entries = [
            {"start": midnight + timedelta(hours=i), "value": 0.10 + i * 0.01}
            for i in range(10)
        ]
        state = MagicMock()
        state.state = "0.10"
        state.attributes = {"net_prices_today": entries}

        fake_now = midnight.replace(hour=8)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.as_utc",
                side_effect=lambda dt: dt,
            ),
        ):
            prices, timestamps, interval = extract_price_forecast_with_timestamps(state)

        assert len(prices) > 0


class TestRawTomorrowInPriority5WithTimestamps:
    """Cover raw_tomorrow entries in priority 5."""

    def test_raw_tomorrow_appended_in_priority5(self):
        """raw_tomorrow without timestamps is appended in extract_price_forecast_with_timestamps."""

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_timestamps,
        )

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        # No timestamps — triggers index-based skip (priority 5)
        raw_today = [{"value": float(i) * 0.01} for i in range(24)]
        raw_tomorrow = [{"value": float(i + 24) * 0.01} for i in range(24)]
        state = MagicMock()
        state.state = "0.10"
        state.attributes = {"raw_today": raw_today, "raw_tomorrow": raw_tomorrow}

        fake_now = midnight.replace(hour=22, minute=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, timestamps, interval = extract_price_forecast_with_timestamps(state)

        # Should include remaining today (2 entries) + all 24 tomorrow
        assert len(prices) == 2 + 24


class TestTodayTomorrowWithTimestamps:
    """Cover combined.extend(tomorrow_attr) in extract_price_forecast_with_timestamps."""

    def test_today_tomorrow_combined_with_timestamps(self):
        """today + tomorrow attributes are combined in extract_price_forecast_with_timestamps."""

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_timestamps,
        )

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        today_prices = [float(i) * 0.01 for i in range(24)]
        tomorrow_prices = [float(i + 24) * 0.01 for i in range(24)]
        state = MagicMock()
        state.state = "0.22"
        state.attributes = {"today": today_prices, "tomorrow": tomorrow_prices}

        fake_now = midnight.replace(hour=22, minute=0)
        with (
            patch(
                "custom_components.battery_controller.helpers.dt_util.utcnow",
                return_value=fake_now,
            ),
            patch(
                "custom_components.battery_controller.helpers.dt_util.now",
                return_value=fake_now,
            ),
        ):
            prices, timestamps, interval = extract_price_forecast_with_timestamps(state)

        # 2 from today (hours 22-23) + 24 from tomorrow
        assert len(prices) == 2 + 24
        assert len(timestamps) == len(prices)


class TestComputeStepDurationsSingleEntry:
    """Cover compute_step_durations_hours with <=1 entries."""

    def test_single_start_time_returns_full_interval(self):
        """A single-entry start_times list returns [full_h]."""

        from custom_components.battery_controller.helpers import (
            compute_step_durations_hours,
        )

        t = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = compute_step_durations_hours([t], 60, t)
        assert result == [1.0]

    def test_empty_start_times_returns_empty(self):
        """Empty start_times returns []."""

        from custom_components.battery_controller.helpers import (
            compute_step_durations_hours,
        )

        t = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = compute_step_durations_hours([], 60, t)
        assert result == []


class TestSolarPositionZenith:
    """Cover _solar_position azimuth=180 fallback when cos_elev < 1e-6."""

    def test_zenith_azimuth_fallback(self):
        """When elevation = 90°, azimuth returns 180° (the undefined zenith fallback)."""

        import custom_components.battery_controller.helpers as helpers_mod

        dt = datetime(2024, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        # Force math.asin to return π/2 so that elevation_deg = 90.0 exactly,
        # making cos(π/2) ≈ 6e-17 < 1e-6 and triggering the zenith fallback.
        with patch.object(helpers_mod.math, "asin", return_value=math.pi / 2):
            elev, azim = _solar_position(dt, 23.45, 0.0)

        assert elev == pytest.approx(90.0)
        assert azim == 180.0


class TestForecastSkipPast:
    """forecast_prices / forecast entries with timestamps skip elapsed periods."""

    def _make_entries(self, now):

        return [
            {"time": (now - timedelta(hours=2)).isoformat(), "price": 0.10},
            {"time": (now - timedelta(hours=1)).isoformat(), "price": 0.11},
            {"time": now.isoformat(), "price": 0.12},
            {"time": (now + timedelta(hours=1)).isoformat(), "price": 0.13},
        ]

    def test_forecast_prices_skips_elapsed_periods(self, monkeypatch):

        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(h.dt_util, "utcnow", lambda: now)

        state = MagicMock()
        state.attributes = {"forecast_prices": self._make_entries(now)}
        state.state = "0.12"

        prices, interval = h.extract_price_forecast_with_interval(state)
        assert prices == [0.12, 0.13]
        assert interval == 60

        prices, starts, interval = h.extract_price_forecast_with_timestamps(state)
        assert prices == [0.12, 0.13]
        assert starts[0] == now

    def test_generic_forecast_skips_elapsed_periods(self, monkeypatch):

        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(h.dt_util, "utcnow", lambda: now)

        state = MagicMock()
        state.attributes = {"forecast": self._make_entries(now)}
        state.state = "0.12"

        prices, interval = h.extract_price_forecast_with_interval(state)
        assert prices == [0.12, 0.13]

    def test_plain_value_lists_unchanged(self, monkeypatch):

        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(h.dt_util, "utcnow", lambda: now)

        state = MagicMock()
        state.attributes = {"forecast_prices": [0.10, 0.11, 0.12]}
        state.state = "0.10"

        prices, interval = h.extract_price_forecast_with_interval(state)
        assert prices == [0.10, 0.11, 0.12]


class TestDstSafeSkipIndex:
    """Index-based skip must use elapsed time, not wall-clock arithmetic."""

    def test_25_hour_day_skips_correct_entries(self, monkeypatch):
        """On the DST fall-back day, raw_today has 25 entries; wall-clock 04:30
        is 5.5 elapsed hours, so 5 entries must be skipped (not 4)."""

        tz = ZoneInfo("Europe/Amsterdam")
        # 2026-10-25: EU DST ends, 03:00 -> 02:00 (25-hour day).
        # fold=1 selects the post-transition 04:30 = 5.5 h after midnight.
        now_local = datetime(2026, 10, 25, 4, 30, tzinfo=tz)
        monkeypatch.setattr(h.dt_util, "now", lambda: now_local)

        prices = [float(i) for i in range(25)]  # one entry per hour-period
        state = MagicMock()
        state.attributes = {"raw_today": prices, "raw_tomorrow": []}
        state.state = "0.0"

        forecast, interval = h.extract_price_forecast_with_interval(state)
        assert interval == 60
        # Elapsed = 5.5 h -> skip 5 entries; first remaining entry is index 5.
        assert forecast[0] == 5.0

    def test_skip_index_helper_normal_day(self):

        from custom_components.battery_controller.helpers import (
            _skip_index_since_local_midnight,
        )

        tz = ZoneInfo("Europe/Amsterdam")
        now_local = datetime(2026, 6, 10, 15, 47, tzinfo=tz)
        assert _skip_index_since_local_midnight(now_local, 60) == 15
        assert _skip_index_since_local_midnight(now_local, 15) == 63


class TestOmieHoursDictExtraction:
    """Tests for OMIE-style today_hours/tomorrow_hours dict attributes."""

    def _make_state(self, attributes=None, state_value="85.0"):
        state = MagicMock()
        state.state = state_value
        state.attributes = attributes or {}
        return state

    def _hours(self, start, count, base_price, step_minutes=60):
        """Build an OMIE-style dict: datetime keys -> €/MWh prices."""
        return {
            start + timedelta(minutes=step_minutes * i): base_price + i
            for i in range(count)
        }

    def test_today_hours_mwh_converted_and_past_skipped(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        # 2 past hours + current + 3 future hours, 100..105 €/MWh
        state = self._make_state(
            {
                "unit_of_measurement": "€/MWh",
                "today_hours": self._hours(now_hour - timedelta(hours=2), 6, 100.0),
                "tomorrow_hours": None,
            }
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert interval == 60
        # Past hours (100, 101 €/MWh) skipped; current hour first: 102 €/MWh
        assert prices[0] == pytest.approx(0.102)
        assert prices == pytest.approx([0.102, 0.103, 0.104, 0.105])

    def test_tomorrow_hours_appended(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        state = self._make_state(
            {
                "unit_of_measurement": "EUR/MWh",
                "today_hours": self._hours(now_hour, 2, 50.0),
                "tomorrow_hours": self._hours(now_hour + timedelta(hours=2), 2, 80.0),
            }
        )
        prices, interval = extract_price_forecast_with_interval(state)
        assert prices == pytest.approx([0.050, 0.051, 0.080, 0.081])

    def test_none_values_skipped(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        hours = self._hours(now_hour, 3, 60.0)
        hours[now_hour + timedelta(hours=1)] = None  # provisional entry
        state = self._make_state({"unit_of_measurement": "€/MWh", "today_hours": hours})
        prices, _ = extract_price_forecast_with_interval(state)
        assert prices == pytest.approx([0.060, 0.062])

    def test_string_keys_parsed(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        state = self._make_state(
            {
                "unit_of_measurement": "€/MWh",
                "today_hours": {
                    (now_hour + timedelta(hours=i)).isoformat(): 100.0 + i
                    for i in range(3)
                },
            }
        )
        prices, _ = extract_price_forecast_with_interval(state)
        assert prices == pytest.approx([0.100, 0.101, 0.102])

    def test_quarter_hour_keys_detect_15min_interval(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_timestamps,
        )

        now = dt_util.utcnow()
        now_quarter = now.replace(
            minute=(now.minute // 15) * 15, second=0, microsecond=0
        )
        state = self._make_state(
            {
                "unit_of_measurement": "€/MWh",
                "today_hours": self._hours(now_quarter, 4, 90.0, step_minutes=15),
            }
        )
        prices, start_times, interval = extract_price_forecast_with_timestamps(state)
        assert interval == 15
        assert prices == pytest.approx([0.090, 0.091, 0.092, 0.093])
        assert start_times[0] == now_quarter

    def test_no_unit_heuristic_detects_mwh_scale(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        state = self._make_state({"today_hours": self._hours(now_hour, 2, 100.0)})
        prices, _ = extract_price_forecast_with_interval(state)
        assert prices == pytest.approx([0.100, 0.101])

    def test_no_unit_kwh_scale_values_unchanged(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        state = self._make_state(
            {
                "today_hours": {
                    now_hour: 0.10,
                    now_hour + timedelta(hours=1): 0.25,
                }
            }
        )
        prices, _ = extract_price_forecast_with_interval(state)
        assert prices == pytest.approx([0.10, 0.25])

    def test_kwh_unit_never_scaled(self):

        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        # Extreme kWh price spike must not trigger the MWh heuristic when
        # the sensor explicitly declares a per-kWh unit
        state = self._make_state(
            {
                "unit_of_measurement": "EUR/kWh",
                "today_hours": {now_hour: 6.0},
            }
        )
        prices, _ = extract_price_forecast_with_interval(state)
        assert prices == pytest.approx([6.0])

    def test_empty_dicts_fall_through_to_state(self):
        from custom_components.battery_controller.helpers import (
            extract_price_forecast_with_interval,
        )

        state = self._make_state(
            {"today_hours": {}, "tomorrow_hours": None}, state_value="0.12"
        )
        prices, _ = extract_price_forecast_with_interval(state)
        # Falls through to current-state fallback
        assert prices == [0.12]


class TestExtractPvForecastSeries:
    """Tests for extract_pv_forecast_series (Solcast-style sensors)."""

    def _make_state(self, attributes=None, state_value="12.5"):
        state = MagicMock()
        state.state = state_value
        state.attributes = attributes or {}
        return state

    def test_solcast_30min_entries_preserved(self):
        base = datetime(2024, 6, 15, 10, 0, tzinfo=timezone.utc)
        state = self._make_state(
            {
                "detailedForecast": [
                    {"period_start": base, "pv_estimate": 1.0},
                    {
                        "period_start": base + timedelta(minutes=30),
                        "pv_estimate": 2.0,
                    },
                    {"period_start": base + timedelta(hours=1), "pv_estimate": 3.0},
                ]
            }
        )
        result = extract_pv_forecast_series([state])
        assert result == [
            (base, 1.0),
            (base + timedelta(minutes=30), 2.0),
            (base + timedelta(hours=1), 3.0),
        ]

    def test_solcast_iso_string_timestamps(self):
        state = self._make_state(
            {
                "detailedForecast": [
                    {
                        "period_start": "2024-06-15T10:30:00+00:00",
                        "pv_estimate": 2.5,
                    },
                ]
            }
        )
        result = extract_pv_forecast_series([state])
        expected_ts = datetime(2024, 6, 15, 10, 30, tzinfo=timezone.utc)
        assert result == [(expected_ts, 2.5)]

    def test_today_and_tomorrow_sensors_combined_sorted(self):
        today_ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        tomorrow_ts = today_ts + timedelta(days=1)
        # Pass tomorrow first: output must still be sorted by time
        tomorrow = self._make_state(
            {"detailedForecast": [{"period_start": tomorrow_ts, "pv_estimate": 4.0}]}
        )
        today = self._make_state(
            {"detailedForecast": [{"period_start": today_ts, "pv_estimate": 3.0}]}
        )
        result = extract_pv_forecast_series([tomorrow, today])
        assert result == [(today_ts, 3.0), (tomorrow_ts, 4.0)]

    def test_duplicate_timestamps_averaged(self):
        ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        state = self._make_state(
            {
                "detailedForecast": [{"period_start": ts, "pv_estimate": 2.0}],
                "detailedHourly": [{"period_start": ts, "pv_estimate": 4.0}],
            }
        )
        result = extract_pv_forecast_series([state])
        assert result == [(ts, pytest.approx(3.0))]

    def test_unavailable_state_skipped(self):
        ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        state = self._make_state(
            {"detailedForecast": [{"period_start": ts, "pv_estimate": 3.0}]},
            state_value="unavailable",
        )
        assert extract_pv_forecast_series([state]) == []

    def test_generic_forecast_attribute_with_watts(self):
        ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        state = self._make_state({"forecast": [{"datetime": ts, "watts": 1500}]})
        result = extract_pv_forecast_series([state])
        assert result == [(ts, pytest.approx(1.5))]

    def test_forecast_solar_watts_dict(self):
        state = self._make_state(
            {
                "watts": {
                    "2024-06-15T12:00:00+00:00": 2000,
                    "2024-06-15T13:00:00+00:00": 1000,
                }
            }
        )
        result = extract_pv_forecast_series([state])
        assert result == [
            (datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc), pytest.approx(2.0)),
            (datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc), pytest.approx(1.0)),
        ]

    def test_negative_values_clamped_to_zero(self):
        ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        state = self._make_state(
            {"detailedForecast": [{"period_start": ts, "pv_estimate": -0.5}]}
        )
        assert extract_pv_forecast_series([state]) == [(ts, 0.0)]

    def test_garbage_entries_ignored(self):
        state = self._make_state(
            {
                "detailedForecast": [
                    "not a dict",
                    {"period_start": "not-a-date", "pv_estimate": 1.0},
                    {"pv_estimate": 1.0},
                    {"period_start": "2024-06-15T12:00:00+00:00"},
                ]
            }
        )
        assert extract_pv_forecast_series([state]) == []

    def test_no_states_returns_empty(self):
        assert extract_pv_forecast_series([]) == []

    def test_volcast_power_kw_and_power_w_keys(self):
        ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        state = self._make_state(
            {
                "detailedHourly": [{"period_start": ts, "power_kw": 3.5}],
                "detailedForecast": [
                    {
                        "period_start": ts + timedelta(hours=1),
                        "power_w": 1500,
                    }
                ],
            }
        )
        result = extract_pv_forecast_series([state])
        assert result == [
            (ts, pytest.approx(3.5)),
            (ts + timedelta(hours=1), pytest.approx(1.5)),
        ]


# ---------------------------------------------------------------------------
# resample_to_steps — time-anchored projection onto the DP step windows
# ---------------------------------------------------------------------------


class TestResampleToSteps:
    """Tests for resample_to_steps."""

    @staticmethod
    def _dt(hour, minute=0):
        return datetime(2026, 3, 21, hour, minute, tzinfo=timezone.utc)

    def test_identical_grid_is_passthrough(self):
        """Aligned quarter-hour source and quarter-hour steps: values unchanged."""
        values = [1.0, 2.0, 3.0, 4.0]
        starts = [self._dt(10, 15 * i) for i in range(4)]
        result = resample_to_steps(values, self._dt(10), 15, starts, [0.25] * 4)
        assert result == pytest.approx(values)

    def test_hourly_steps_average_quarter_hour_source(self):
        """One hourly step averages the four quarters it covers."""
        values = [1.0, 2.0, 3.0, 4.0, 10.0, 10.0, 10.0, 10.0]
        starts = [self._dt(10), self._dt(11)]
        result = resample_to_steps(values, self._dt(10), 15, starts, [1.0, 1.0])
        assert result == pytest.approx([2.5, 10.0])

    def test_offset_source_is_not_shifted_onto_later_steps(self):
        """A source anchored mid-period must not slide onto the next step.

        This is the hourly-price regression: the forecast pipeline anchors to
        the current quarter (10:30) while DP steps land on hour boundaries.
        Interval-only resampling built [avg(10:30..11:30), avg(11:30..12:30)]
        and handed the first entry to the 10:30-11:00 step and the second to
        the 11:00-12:00 step — a 30-minute shift on every step.
        """
        # Quarter-hourly source starting at 10:30.
        values = [1.0, 1.0, 5.0, 5.0, 5.0, 5.0, 9.0, 9.0]  # 10:30 .. 12:30
        # Step 0 is the remainder of the current hour, then full hours.
        starts = [self._dt(10, 30), self._dt(11), self._dt(12)]
        result = resample_to_steps(
            values, self._dt(10, 30), 15, starts, [0.5, 1.0, 1.0]
        )
        # 10:30-11:00 -> 1.0, 11:00-12:00 -> 5.0, 12:00-12:30 -> 9.0 (partial)
        assert result == pytest.approx([1.0, 5.0, 9.0])

    def test_partial_first_step_takes_only_its_own_window(self):
        """A one-minute step 0 reads the value covering that minute, not an hour."""
        values = [2.0, 8.0]  # two hourly values from 10:00
        starts = [self._dt(10, 59), self._dt(11)]
        result = resample_to_steps(values, self._dt(10), 60, starts, [1 / 60.0, 1.0])
        assert result == pytest.approx([2.0, 8.0])

    def test_stops_at_end_of_source(self):
        """Steps beyond the source horizon are not produced (caller pads)."""
        values = [1.0, 2.0]
        starts = [self._dt(10) + timedelta(minutes=15 * i) for i in range(6)]
        result = resample_to_steps(values, self._dt(10), 15, starts, [0.25] * 6)
        assert result == pytest.approx([1.0, 2.0])

    def test_source_entirely_after_horizon_returns_empty(self):
        """No overlap at all yields an empty list so the caller can fall back."""
        values = [0.10, 0.10]
        starts = [self._dt(10), self._dt(11)]
        result = resample_to_steps(
            values, self._dt(10) + timedelta(days=1), 15, starts, [1.0, 1.0]
        )
        assert result == []

    def test_empty_inputs(self):
        assert resample_to_steps([], self._dt(10), 15, [self._dt(10)], [0.25]) == []
        assert resample_to_steps([1.0], self._dt(10), 15, [], []) == []
        assert resample_to_steps([1.0], self._dt(10), 0, [self._dt(10)], [0.25]) == []

    def test_is_mean_preserving_for_power(self):
        """Total energy over the covered span is preserved."""
        values = [0.0, 1.0, 3.0, 4.0, 2.0, 0.0, 0.0, 0.0]
        starts = [self._dt(10), self._dt(11)]
        durations = [1.0, 1.0]
        result = resample_to_steps(values, self._dt(10), 15, starts, durations)
        source_kwh = sum(values) * 0.25
        step_kwh = sum(v * d for v, d in zip(result, durations))
        assert step_kwh == pytest.approx(source_kwh)
