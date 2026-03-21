"""Tests for helpers.py."""

import pytest
from datetime import timezone
from unittest.mock import MagicMock, patch

from custom_components.battery_controller.helpers import (
    clamp,
    safe_float,
    resample_forecast,
    calculate_pv_forecast,
    calculate_consumption_pattern,
    extract_price_forecast_with_interval,
    extract_price_forecast_with_timestamps,
)


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
        from unittest.mock import patch
        from homeassistant.util import dt as dt_util

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
        from unittest.mock import patch
        from homeassistant.util import dt as dt_util

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
        from datetime import timedelta

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
        from datetime import datetime

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
        from datetime import datetime

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
        from datetime import datetime

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
        from datetime import datetime

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
        from datetime import datetime, timedelta

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
        from datetime import datetime, timedelta
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
