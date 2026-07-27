"""Tests for forecast_models.py."""

import logging
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.battery_controller.forecast_models import (
    PVForecastModel,
    ConsumptionForecastModel,
    NetLoadForecast,
    PriceForecastModel,
)


class TestPVForecastModel:
    """Tests for PVForecastModel."""

    def test_basic_forecast(self):
        model = PVForecastModel(peak_power_kwp=5.0, efficiency_factor=0.85)
        forecast = model.forecast_from_radiation([0, 200, 500, 800, 1000, 500, 0])
        assert len(forecast) == 7
        assert forecast[0] == 0.0  # Night
        assert forecast[-1] == 0.0  # Night
        assert forecast[4] > forecast[2]  # Noon > morning

    def test_zero_peak_power(self):
        model = PVForecastModel(peak_power_kwp=0.0)
        forecast = model.forecast_from_radiation([500, 800])
        assert forecast == [0.0, 0.0]


class TestConsumptionForecastModel:
    """Tests for ConsumptionForecastModel."""

    def test_default_pattern_forecast(self):
        hass = MagicMock()
        model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)
        forecast = model.forecast(hours=24)
        assert len(forecast) == 24
        assert all(v > 0 for v in forecast)

    def test_current_consumption_from_pattern(self):
        """Current consumption uses learned hourly pattern."""
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.electricity_consumed_tariff_1"],
            base_consumption_kw=0.5,
        )
        # Inject a learned pattern
        model._hourly_pattern = {(h, d): 0.8 for h in range(24) for d in range(7)}
        result = model.get_current_consumption()
        assert result == pytest.approx(0.8)

    def test_current_consumption_fallback(self):
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=[],
            base_consumption_kw=0.5,
        )
        result = model.get_current_consumption()
        assert result > 0  # Should use default pattern fallback

    def test_accepts_pv_production_sensors_and_entry_id(self):
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            pv_production_sensors=["sensor.pv_total"],
            entry_id="test_entry_123",
        )
        assert model.pv_production_sensors == ["sensor.pv_total"]
        assert model._entry_id == "test_entry_123"

    def test_pv_production_sensors_defaults_to_empty(self):
        hass = MagicMock()
        model = ConsumptionForecastModel(hass=hass)
        assert model.pv_production_sensors == []
        assert model._entry_id is None


class TestNetLoadForecast:
    """Tests for NetLoadForecast."""

    def test_net_load_calculation(self):
        hass = MagicMock()
        pv_model = PVForecastModel(peak_power_kwp=5.0, efficiency_factor=0.85)
        consumption_model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)

        net_model = NetLoadForecast(pv_model, consumption_model)

        # Midday radiation -> PV production
        radiation = [0, 200, 500, 800, 500, 200, 0]
        pv, consumption, net_load = net_model.forecast(radiation)

        assert len(pv) == 7
        assert len(consumption) == 7
        assert len(net_load) == 7

        # Net load = consumption - PV
        for p, c, n in zip(pv, consumption, net_load):
            assert n == pytest.approx(c - p)

    def test_net_load_surplus(self):
        """With large PV, net load should be negative (export)."""
        hass = MagicMock()
        pv_model = PVForecastModel(peak_power_kwp=10.0, efficiency_factor=0.85)
        consumption_model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.3)

        net_model = NetLoadForecast(pv_model, consumption_model)

        # High radiation
        radiation = [1000] * 5
        pv, consumption, net_load = net_model.forecast(radiation)

        # Large PV should create negative net load (surplus)
        assert any(n < 0 for n in net_load)

    def test_empty_radiation(self):
        hass = MagicMock()
        pv_model = PVForecastModel(peak_power_kwp=5.0)
        consumption_model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)

        net_model = NetLoadForecast(pv_model, consumption_model)
        pv, consumption, net_load = net_model.forecast([], hours=4)

        assert len(pv) == 4
        assert len(consumption) == 4
        # No PV -> net load = consumption
        for p, c, n in zip(pv, consumption, net_load):
            assert p == 0.0
            assert n == pytest.approx(c)


class TestAsyncUpdatePattern:
    """Tests for ConsumptionForecastModel.async_update_pattern PV correction layers."""

    # 2024-01-01 is a Monday (weekday=0), hour=10 → key=(10, 0)
    _TS = "2024-01-01T10:00:00"

    def _base_stats(self, consumption_kwh: float, production_kwh: float) -> dict:
        return {
            "sensor.consumption": [{"start": self._TS, "change": consumption_kwh}],
            "sensor.production": [{"start": self._TS, "change": production_kwh}],
        }

    async def test_layer1_adds_back_pv_production(self):
        """Layer 1: pv_production_sensors stats are added back to correct double-counting."""
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
            production_sensors=["sensor.production"],
            pv_production_sensors=["sensor.pv_total"],
        )
        # net = 2.0 - 1.5 = 0.5 kWh; after correction +1.5 → 2.0 (gross consumption)
        base_stats = self._base_stats(2.0, 1.5)
        pv_stats = {"sensor.pv_total": [{"start": self._TS, "change": 1.5}]}

        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            side_effect=[base_stats, pv_stats]
        )

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert (10, 0) in model._hourly_pattern
        assert model._hourly_pattern[(10, 0)] == pytest.approx(2.0)

    async def test_layer2_uses_entity_registry_fallback(self):
        """Layer 2: own pv_forecast entity used when pv_production_sensors absent."""
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
            production_sensors=["sensor.production"],
            entry_id="myentry",
        )
        base_stats = self._base_stats(2.0, 1.5)
        pv_forecast_entity = "sensor.battery_controller_pv_forecast"
        pv_hist_stats = {pv_forecast_entity: [{"start": self._TS, "mean": 1.5}]}

        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            side_effect=[base_stats, pv_hist_stats]
        )
        mock_ent_reg = MagicMock()
        mock_ent_reg.async_get_entity_id = MagicMock(return_value=pv_forecast_entity)

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                return_value=mock_ent_reg,
            ),
        ):
            await model.async_update_pattern()

        assert (10, 0) in model._hourly_pattern
        assert model._hourly_pattern[(10, 0)] == pytest.approx(2.0)

    async def test_layer3_warning_when_no_correction(self, caplog):
        """Layer 3: warning logged when production_sensors present but no correction."""
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
            production_sensors=["sensor.production"],
            # No pv_production_sensors, no entry_id
        )
        base_stats = self._base_stats(2.0, 1.5)
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=base_stats)

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            caplog.at_level(
                logging.WARNING,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "double-counting" in caplog.text

    async def test_no_warning_without_production_sensors(self, caplog):
        """No double-counting warning when production_sensors not configured."""
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        base_stats = {"sensor.consumption": [{"start": self._TS, "change": 2.0}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=base_stats)

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            caplog.at_level(
                logging.WARNING,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "double-counting" not in caplog.text

    async def test_datetime_start_field_handled_consumption(self):
        """_ts_and_value handles datetime objects (not just strings) as start.

        Patterns are stored in local time so they align with the local-time
        forecast generated in ConsumptionForecastModel.forecast().
        """
        from datetime import datetime, timezone

        from homeassistant.util import dt as dt_util

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        dt_start = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        expected_local = dt_util.as_local(dt_start)
        expected_key = (expected_local.hour, expected_local.weekday())

        base_stats = {"sensor.consumption": [{"start": dt_start, "change": 3.0}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=base_stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert expected_key in model._hourly_pattern
        assert model._hourly_pattern[expected_key] == pytest.approx(3.0)


class TestPriceForecastModelBins:
    """Unit tests for PriceForecastModel bin classification."""

    def test_ghi_bins(self):
        assert PriceForecastModel._ghi_bin(0.0) == 0  # dark/night
        assert PriceForecastModel._ghi_bin(49.9) == 0
        assert PriceForecastModel._ghi_bin(50.0) == 1  # overcast
        assert PriceForecastModel._ghi_bin(199.9) == 1
        assert PriceForecastModel._ghi_bin(200.0) == 2  # partial cloud
        assert PriceForecastModel._ghi_bin(499.9) == 2
        assert PriceForecastModel._ghi_bin(500.0) == 3  # bright sun
        assert PriceForecastModel._ghi_bin(1000.0) == 3

    def test_wind_bins(self):
        assert PriceForecastModel._wind_bin(0.0) == 0  # calm
        assert PriceForecastModel._wind_bin(3.9) == 0
        assert PriceForecastModel._wind_bin(4.0) == 1  # moderate
        assert PriceForecastModel._wind_bin(7.9) == 1
        assert PriceForecastModel._wind_bin(8.0) == 2  # strong
        assert PriceForecastModel._wind_bin(15.0) == 2


class TestPriceForecastModelInit:
    """Tests for PriceForecastModel initial state."""

    def test_has_data_false_initially(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        assert model.has_data() is False

    def test_forecast_returns_default_when_no_data(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        result = model.forecast(hours=3)
        assert len(result) == 3
        # Default is 0.20 EUR/kWh when no data
        assert all(v == pytest.approx(0.20) for v in result)

    def test_forecast_length_matches_hours(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        assert len(model.forecast(hours=24)) == 24
        assert len(model.forecast(hours=48)) == 48


class TestPriceForecastModelPatternUpdate:
    """Tests for PriceForecastModel.async_update_pattern."""

    # 2024-01-01 10:00 UTC (Monday).  Tests compute the local-time key dynamically
    # so they pass regardless of the test-runner's timezone.
    _TS = "2024-01-01T10:00:00+00:00"
    _DT = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

    def _make_price_stats(self, price: float, ts=None) -> dict:
        ts = ts or self._TS
        return {"sensor.price": [{"start": ts, "mean": price}]}

    async def test_no_statistics_leaves_model_empty(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value={})

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert model.has_data() is False

    async def test_price_only_builds_simple_pattern(self):
        hass = MagicMock()
        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.price", entry_id=None
        )
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            return_value=self._make_price_stats(0.25)
        )

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        from homeassistant.util import dt as dt_util

        expected_local = dt_util.as_local(self._DT)
        expected_key = (expected_local.hour, expected_local.weekday())

        assert model.has_data() is True
        assert expected_key in model._simple_pattern
        assert 0.25 in model._simple_pattern[expected_key]
        assert model._overall_avg == pytest.approx(0.25)

    async def test_price_with_weather_builds_weather_pattern(self):
        hass = MagicMock()
        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.price", entry_id="eid"
        )
        price_stats = self._make_price_stats(0.15)
        # GHI=600 → bin 3 (bright), wind=10 → bin 2 (strong)
        weather_stats = {
            "sensor.bc_ghi": [{"start": self._TS, "mean": 600.0}],
            "sensor.bc_wind": [{"start": self._TS, "mean": 10.0}],
        }
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            side_effect=[price_stats, weather_stats]
        )
        mock_ent_reg = MagicMock()
        mock_ent_reg.async_get_entity_id = MagicMock(
            side_effect=lambda platform, domain, uid: (
                "sensor.bc_ghi" if uid.endswith("_ghi") else "sensor.bc_wind"
            )
        )

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                return_value=mock_ent_reg,
            ),
        ):
            await model.async_update_pattern()

        from homeassistant.util import dt as dt_util

        expected_local = dt_util.as_local(self._DT)
        expected_key = (expected_local.hour, expected_local.weekday(), 3, 2)

        assert model.has_data() is True
        assert expected_key in model._weather_pattern
        assert 0.15 in model._weather_pattern[expected_key]

    async def test_datetime_start_field_handled(self):
        hass = MagicMock()
        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.price", entry_id=None
        )
        price_stats = {"sensor.price": [{"start": self._DT, "mean": 0.18}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=price_stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        from homeassistant.util import dt as dt_util

        expected_local = dt_util.as_local(self._DT)
        expected_key = (expected_local.hour, expected_local.weekday())
        assert expected_key in model._simple_pattern
        assert 0.18 in model._simple_pattern[expected_key]

    async def test_recorder_import_error_handled_gracefully(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            side_effect=ImportError,
        ):
            # Must not raise
            await model.async_update_pattern()

        assert model.has_data() is False


class TestPriceForecastModelForecast:
    """Tests for PriceForecastModel.forecast() fallback hierarchy."""

    def _model_with_data(self) -> PriceForecastModel:
        """Return a model with injected simple and weather patterns."""
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        # Populate simple pattern: hour=10, dow=0 → avg 0.30
        model._simple_pattern = {(10, 0): [0.28, 0.32]}
        # Weather pattern: hour=10, dow=0, ghi_bin=3, wind_bin=2 → avg 0.10 (windy+sunny = cheap)
        model._weather_pattern = {(10, 0, 3, 2): [0.08, 0.12]}
        model._overall_avg = 0.25
        return model

    def test_uses_weather_pattern_when_available(self):
        model = self._model_with_data()
        # GHI=600 → bin 3, wind=10 → bin 2 → weather pattern [0.08, 0.12]
        # avg=0.10, std=0.02, below overall(0.25) → 0.10 - 0.5*0.02 = 0.09
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
            ghi_forecast=[600.0],
            wind_forecast=[10.0],
        )
        assert result[0] == pytest.approx(0.09)

    def test_falls_back_to_simple_when_weather_bin_sparse(self):
        model = self._model_with_data()
        # GHI=10 → bin 0, wind=1 → bin 0 → no weather pattern for (10,0,0,0) → simple
        # simple [0.28, 0.32]: avg=0.30, std=0.02, above overall(0.25) → 0.30 + 0.5*0.02 = 0.31
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
            ghi_forecast=[10.0],
            wind_forecast=[1.0],
        )
        assert result[0] == pytest.approx(0.31)

    def test_falls_back_to_overall_avg_when_no_pattern(self):
        model = self._model_with_data()
        # hour=15 has no (15, 0) entry → falls back to overall avg (0.25), no std correction
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc),
        )
        assert result[0] == pytest.approx(0.25)

    def test_no_weather_args_uses_simple_pattern(self):
        model = self._model_with_data()
        # No GHI/wind provided → skips weather lookup → uses simple pattern
        # simple [0.28, 0.32]: avg=0.30, std=0.02, above overall(0.25) → 0.30 + 0.5*0.02 = 0.31
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        assert result[0] == pytest.approx(0.31)

    def test_forecast_24_hours(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        model._overall_avg = 0.20
        result = model.forecast(hours=24)
        assert len(result) == 24
        assert all(v == pytest.approx(0.20) for v in result)


class TestHorizonExtension:
    """Tests for the horizon extension logic (resample + PriceForecastModel.forecast)."""

    def test_extension_fills_missing_hours(self):
        """Simulates a 14-hour live forecast being extended to 24 hours."""
        from custom_components.battery_controller.helpers import resample_forecast

        live_prices = [0.20] * 14  # 14 hourly prices (e.g. 10:00 – 23:00)
        time_step = 60
        min_horizon_steps = 24 * 60 // time_step  # 24

        resampled_prices = resample_forecast(live_prices, 60, time_step)
        assert len(resampled_prices) == 14

        # Simulate the model extension
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        model._overall_avg = 0.18

        steps_needed = min_horizon_steps - len(resampled_prices)  # 10
        hours_for_model = (steps_needed * time_step + 59) // 60  # 10

        extension = model.forecast(hours=hours_for_model)
        resampled_extension = resample_forecast(extension, 60, time_step)

        result = resampled_prices + resampled_extension[:steps_needed]
        assert len(result) == 24
        assert result[:14] == pytest.approx([0.20] * 14)
        assert result[14:] == pytest.approx([0.18] * 10)

    def test_extension_with_15min_timestep(self):
        """Extension works correctly with 15-minute time steps."""
        from custom_components.battery_controller.helpers import resample_forecast

        # 14 hourly prices → 56 steps at 15 min
        live_prices = [0.20] * 14
        time_step = 15
        min_horizon_steps = 24 * 60 // time_step  # 96

        resampled_prices = resample_forecast(live_prices, 60, time_step)
        assert len(resampled_prices) == 56

        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        model._overall_avg = 0.15

        steps_needed = min_horizon_steps - len(resampled_prices)  # 40
        hours_for_model = (steps_needed * time_step + 59) // 60  # 10

        extension = model.forecast(hours=hours_for_model)
        resampled_extension = resample_forecast(extension, 60, time_step)

        result = resampled_prices + resampled_extension[:steps_needed]
        assert len(result) == 96

    def test_no_extension_when_full_horizon(self):
        """No extension when live prices already cover 24 hours."""
        from custom_components.battery_controller.helpers import resample_forecast

        live_prices = [0.20] * 24
        time_step = 60
        min_horizon_steps = 24

        resampled_prices = resample_forecast(live_prices, 60, time_step)
        assert len(resampled_prices) >= min_horizon_steps
        # Extension block should be skipped (condition is False)


# ---------------------------------------------------------------------------
# Additional coverage: missing branches in forecast_models.py
# ---------------------------------------------------------------------------


class TestPVForecastModelTemperatureDerating:
    """Cover temperature derating in PVForecastModel.forecast_from_radiation (101-110)."""

    def test_temperature_derating_applied(self):
        """Temperature derating reduces output at high temperature."""
        from custom_components.battery_controller.forecast_models import PVForecastModel

        model = PVForecastModel(
            peak_power_kwp=5.0,
            orientation_deg=180.0,
            tilt_deg=35.0,
            efficiency_factor=0.85,
        )
        radiation = [1000.0] * 4  # strong radiation
        # At 35°C (10°C above STC 25°C), expect slight derating
        temps = [35.0] * 4

        result_with_temp = model.forecast_from_radiation(
            radiation, temperature_forecast=temps
        )
        result_no_temp = model.forecast_from_radiation(
            radiation, temperature_forecast=None
        )

        # With temperature, output should be slightly lower due to derating
        assert all(r_t <= r_n for r_t, r_n in zip(result_with_temp, result_no_temp))

    def test_temperature_forecast_shorter_than_radiation(self):
        """When temperature_forecast is shorter, remaining entries use base_forecast."""
        from custom_components.battery_controller.forecast_models import PVForecastModel

        model = PVForecastModel(peak_power_kwp=5.0)
        radiation = [1000.0] * 4
        temps = [35.0, 35.0]  # only 2 temps for 4 radiation entries

        result = model.forecast_from_radiation(radiation, temperature_forecast=temps)
        # Should have 4 entries (line 109: else: result.append(pv_kw))
        assert len(result) == 4


class TestConsumptionForecastModelPatterns:
    """Cover forecast() with _seasonal_pattern and _hourly_pattern (lines 372, 374)."""

    def test_seasonal_pattern_used_in_forecast(self, hass):
        """When seasonal pattern is available, it's used first (line 372)."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
            _get_season,
        )
        from datetime import datetime

        model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)
        # Populate seasonal pattern
        dt_now = datetime(2024, 6, 15, 10, 0, 0)
        season = _get_season(dt_now.month)
        model._seasonal_pattern[(10, dt_now.weekday(), season)] = 2.5

        result = model.forecast(hours=1, start_time=dt_now)
        assert result[0] == pytest.approx(2.5)

    def test_hourly_pattern_used_when_no_seasonal(self, hass):
        """When no seasonal but hourly pattern available, hourly is used (line 374)."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from datetime import datetime

        model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)
        dt_now = datetime(2024, 6, 15, 10, 0, 0)
        model._hourly_pattern[(10, dt_now.weekday())] = 1.8

        result = model.forecast(hours=1, start_time=dt_now)
        assert result[0] == pytest.approx(1.8)


class TestConsumptionForecastModelGetCurrentConsumption:
    """Cover get_current_consumption with seasonal pattern (line 395)."""

    def test_seasonal_pattern_used_in_current(self, hass):
        """get_current_consumption uses seasonal pattern when available (line 395)."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
            _get_season,
        )
        from unittest.mock import patch
        from datetime import datetime
        import custom_components.battery_controller.forecast_models as fm_mod

        model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)
        fake_now = datetime(2024, 6, 15, 10, 0, 0)
        season = _get_season(fake_now.month)
        model._seasonal_pattern[(10, fake_now.weekday(), season)] = 3.0

        with patch.object(fm_mod.dt_util, "now", return_value=fake_now):
            result = model.get_current_consumption()

        assert result == pytest.approx(3.0)

    def test_hourly_pattern_used_in_current(self, hass):
        """get_current_consumption uses hourly pattern when seasonal absent (line 397)."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import patch
        from datetime import datetime
        import custom_components.battery_controller.forecast_models as fm_mod

        model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)
        fake_now = datetime(2024, 6, 15, 10, 0, 0)
        model._hourly_pattern[(10, fake_now.weekday())] = 2.2

        with patch.object(fm_mod.dt_util, "now", return_value=fake_now):
            result = model.get_current_consumption()

        assert result == pytest.approx(2.2)


class TestNetLoadModelForecastPadding:
    """Cover padding in NetLoadForecastModel.forecast when pv_forecast is short (line 722)."""

    def test_pv_forecast_padded_when_shorter_than_hours(self):
        """When pv_model returns fewer entries than hours, zeros are appended (line 722)."""
        from custom_components.battery_controller.forecast_models import (
            NetLoadForecast as NetLoadForecastModel,
        )
        from unittest.mock import MagicMock

        pv_model = MagicMock()
        pv_model.forecast_from_radiation = MagicMock(
            return_value=[1.0, 2.0]
        )  # only 2, but hours=4

        consumption_model = MagicMock()
        consumption_model.forecast = MagicMock(return_value=[0.5] * 4)
        consumption_model.base_consumption_kw = 0.5

        net_model = NetLoadForecastModel(pv_model, consumption_model)
        pv_fc, consumption_fc, net_fc = net_model.forecast([1000.0] * 2, hours=4)

        assert len(pv_fc) == 4
        # Padded with 0.0
        assert pv_fc[2] == 0.0
        assert pv_fc[3] == 0.0


class TestPriceForecastModelNoSensor:
    """Cover PriceForecastModel.async_update_pattern when no sensor_id (line 464)."""

    @pytest.mark.asyncio
    async def test_no_price_sensor_returns_early(self, hass):
        """async_update_pattern returns early when price_sensor_id is None (line 464)."""
        from custom_components.battery_controller.forecast_models import (
            PriceForecastModel,
        )

        model = PriceForecastModel(hass, price_sensor_id=None)

        # Should not raise and should return without doing anything
        await model.async_update_pattern()


# ---------------------------------------------------------------------------
# Extra coverage: missing lines
# ---------------------------------------------------------------------------


class TestConsumptionForecastModelNoSensors:
    """Cover async_update_pattern early return when no sensors (line 170)."""

    async def test_no_sensors_returns_early(self):
        """Returns immediately when no consumption/production sensors configured."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import MagicMock

        hass = MagicMock()
        model = ConsumptionForecastModel(hass=hass, base_consumption_kw=0.5)
        # No sensors at all → should return without touching recorder
        await model.async_update_pattern()
        # Pattern should remain empty (no data was loaded)
        assert model._hourly_pattern == {}


class TestConsumptionForecastModelEmptyStats:
    """Cover async_update_pattern debug log when stats empty (lines 192-194)."""

    async def test_empty_stats_returns_early(self, caplog):
        """When recorder returns empty stats, logs debug and returns (lines 192-194)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value={})

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "No statistics found" in caplog.text
        assert model._hourly_pattern == {}


class TestConsumptionForecastModelTsAndValueNone:
    """Cover _ts_and_value returning None when value is None (line 205)."""

    async def test_none_change_skipped(self):
        """Stats entries with None change field are silently skipped (line 205)."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        # 'change' is None → _ts_and_value returns None → entry skipped
        stats = {
            "sensor.consumption": [{"start": "2024-01-01T10:00:00", "change": None}]
        }
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        # Skipped → no hourly pattern
        assert model._hourly_pattern == {}


class TestConsumptionForecastModelSeasonalMinSamples:
    """Cover seasonal bucket only stored when >= _SEASONAL_MIN_SAMPLES (line 330)."""

    async def test_seasonal_bucket_stored_with_enough_samples(self):
        """When enough samples, seasonal pattern is populated (line 330)."""
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        # Use multiple timestamps in the same (hour, weekday, season) bucket
        # 2024-01-01, 2024-01-08, 2024-01-15 are all Mondays in winter, hour=10
        stats = {
            "sensor.consumption": [
                {"start": f"2024-01-{d:02d}T10:00:00", "change": 1.0}
                for d in (1, 8, 15)  # 3 Mondays in January
            ]
        }
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        # At least some seasonal data should be populated
        assert len(model._seasonal_pattern) > 0


class TestConsumptionForecastModelImportError:
    """Cover ImportError branch in async_update_pattern (lines 340-341)."""

    async def test_import_error_handled_gracefully(self, caplog):
        """ImportError for recorder is caught and logged as debug (lines 340-341)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import patch

        hass = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                side_effect=ImportError("no recorder"),
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "Recorder not available" in caplog.text


class TestConsumptionForecastModelGenericException:
    """Cover generic Exception branch in async_update_pattern (lines 342-343)."""

    async def test_generic_exception_handled(self, caplog):
        """Generic exception during pattern update is caught and logged (lines 342-343)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            side_effect=RuntimeError("unexpected failure")
        )

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            caplog.at_level(
                logging.WARNING,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "Failed to update consumption pattern" in caplog.text


class TestPriceForecastModelStatDtNone:
    """Cover _stat_dt returning None when start is None (line 509)."""

    async def test_stat_with_none_start_skipped(self):
        """Stats with start=None are skipped in the price model (line 509)."""
        from custom_components.battery_controller.forecast_models import (
            PriceForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        # Entry with start=None should be silently ignored
        stats = {"sensor.price": [{"start": None, "mean": 0.25}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        # No valid data parsed → model has no data
        assert model.has_data() is False


class TestPriceForecastModelStatDtNumeric:
    """Cover _stat_dt unix timestamp branch (lines 513-516)."""

    async def test_unix_timestamp_start_handled(self):
        """Stats with unix timestamp as start are parsed correctly (lines 513-516)."""
        from datetime import datetime, timezone
        from custom_components.battery_controller.forecast_models import (
            PriceForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        # 2024-01-01T10:00:00 UTC as unix timestamp
        ts = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        stats = {"sensor.price": [{"start": ts, "mean": 0.22}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert model.has_data() is True


class TestPriceForecastModelImportError:
    """Cover ImportError in price model async_update_pattern (lines 605-606)."""

    async def test_import_error_handled(self, caplog):
        """ImportError for recorder is caught and logged as debug (lines 605-606)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            PriceForecastModel,
        )
        from unittest.mock import patch

        hass = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                side_effect=ImportError("no recorder"),
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "Recorder not available" in caplog.text


class TestPriceForecastModelGenericException:
    """Cover generic Exception in price model async_update_pattern (lines 607-608)."""

    async def test_generic_exception_handled(self, caplog):
        """Generic exception is caught and logged as warning (lines 607-608)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            PriceForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            side_effect=RuntimeError("db error")
        )

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            caplog.at_level(
                logging.WARNING,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "Failed to update price pattern" in caplog.text


class TestPriceForecastModelEntityRegistryException:
    """Cover Exception in entity registry lookup in price model (lines 490-491)."""

    async def test_entity_registry_exception_handled(self, caplog):
        """Exception resolving weather sensor IDs is caught (lines 490-491)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            PriceForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.price", entry_id="myentry"
        )
        _DT = "2024-01-01T10:00:00+00:00"
        price_stats = {"sensor.price": [{"start": _DT, "mean": 0.20}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=price_stats)

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                side_effect=RuntimeError("registry unavailable"),
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "Could not resolve weather sensor IDs" in caplog.text


class TestConsumptionForecastModelLayer2Exception:
    """Cover exception in layer 2 pv forecast lookup (lines 291-292)."""

    async def test_layer2_exception_caught_and_logged(self, caplog):
        """Exception during entity registry pv_forecast lookup is caught (lines 291-292)."""
        import logging
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
            production_sensors=["sensor.production"],
            entry_id="myentry",
            # No pv_production_sensors → layer 2 path
        )
        _TS = "2024-01-01T10:00:00"
        base_stats = {
            "sensor.consumption": [{"start": _TS, "change": 2.0}],
            "sensor.production": [{"start": _TS, "change": 1.5}],
        }
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=base_stats)

        with (
            patch(
                "homeassistant.components.recorder.util.get_instance",
                return_value=mock_instance,
            ),
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                side_effect=RuntimeError("registry error in layer 2"),
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="custom_components.battery_controller.forecast_models",
            ),
        ):
            await model.async_update_pattern()

        assert "Could not apply PV correction from forecast sensor" in caplog.text


class TestConsumptionForecastModelNullDatetimeParse:
    """Cover dt_util.parse_datetime returning None for unknown timestamp (line 314)."""

    async def test_unparseable_timestamp_skipped(self):
        """Stats with a timestamp that parse_datetime can't parse are skipped (line 314).

        When start=None: ts_key = str(None or '') = '' → parse_datetime('') = None →
        line 314: if dt is None: continue
        """
        from custom_components.battery_controller.forecast_models import (
            ConsumptionForecastModel,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        # start=None + change=1.0: _ts_and_value returns ("", 1.0)
        # hourly_net[""] = 1.0, but parse_datetime("") = None → line 314 continue
        stats = {
            "sensor.consumption": [
                {"start": None, "change": 1.0},
            ]
        }
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        # The empty ts_key fails parse_datetime → skipped → no hourly_pattern populated
        assert model._hourly_pattern == {}


class TestNetLoadForecastConsumptionPadded:
    """Cover padding of consumption_forecast in NetLoadForecast (line 722)."""

    def test_consumption_padded_when_shorter_than_hours(self):
        """When consumption_model returns fewer entries than hours, base value is appended."""
        from custom_components.battery_controller.forecast_models import (
            NetLoadForecast as NetLoadForecastModel,
        )
        from unittest.mock import MagicMock

        pv_model = MagicMock()
        pv_model.forecast_from_radiation = MagicMock(return_value=[0.0] * 4)

        consumption_model = MagicMock()
        consumption_model.forecast = MagicMock(return_value=[0.5, 0.5])  # only 2
        consumption_model.base_consumption_kw = 0.5

        net_model = NetLoadForecastModel(pv_model, consumption_model)
        pv_fc, consumption_fc, net_fc = net_model.forecast([0.0] * 4, hours=4)

        assert len(consumption_fc) == 4
        # Padded with base_consumption_kw
        assert consumption_fc[2] == pytest.approx(0.5)
        assert consumption_fc[3] == pytest.approx(0.5)


class TestPriceForecastModelUnitScaling:
    """Recorder prices in €/MWh (e.g. OMIE) are scaled to EUR/kWh."""

    _TS = "2024-01-01T10:00:00+00:00"

    async def test_mwh_unit_scales_learned_prices(self):
        hass = MagicMock()
        live_state = MagicMock()
        live_state.attributes = {"unit_of_measurement": "€/MWh"}
        hass.states.get = MagicMock(return_value=live_state)

        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.omie_spot_price_pt", entry_id=None
        )
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            return_value={
                "sensor.omie_spot_price_pt": [{"start": self._TS, "mean": 85.0}]
            }
        )

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert model.has_data() is True
        assert model._overall_avg == pytest.approx(0.085)

    async def test_kwh_unit_not_scaled(self):
        hass = MagicMock()
        live_state = MagicMock()
        live_state.attributes = {"unit_of_measurement": "EUR/kWh"}
        hass.states.get = MagicMock(return_value=live_state)

        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.price", entry_id=None
        )
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            return_value={"sensor.price": [{"start": self._TS, "mean": 0.25}]}
        )

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert model._overall_avg == pytest.approx(0.25)


class TestAsyncUpdatePatternStartFormats:
    """The recorder's "start" field must be handled in every form it takes.

    Current Home Assistant returns statistics rows with "start" as a Unix
    timestamp (float); older versions used a datetime or an ISO string.
    A float used to be stringified to "1704106800.0" and parsed back with
    parse_datetime(), which returns None — silently dropping every bucket
    and leaving the learned pattern empty while statistics were present.
    """

    # 2024-01-01 10:00 UTC (a Monday)
    _DT = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def _expected_key():
        from homeassistant.util import dt as dt_util

        local = dt_util.as_local(TestAsyncUpdatePatternStartFormats._DT)
        return (local.hour, local.weekday())

    async def _run(self, start_value):
        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass, consumption_sensors=["sensor.consumption"]
        )
        stats = {"sensor.consumption": [{"start": start_value, "change": 2.5}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=stats)

        with patch(
            "homeassistant.components.recorder.util.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()
        return model

    async def test_float_unix_timestamp_start(self):
        """Regression: float timestamps must not be dropped."""
        model = await self._run(self._DT.timestamp())
        assert model._hourly_pattern, "pattern must not be empty for float timestamps"
        assert model._hourly_pattern[self._expected_key()] == pytest.approx(2.5)

    async def test_int_unix_timestamp_start(self):
        model = await self._run(int(self._DT.timestamp()))
        assert model._hourly_pattern[self._expected_key()] == pytest.approx(2.5)

    async def test_datetime_start(self):
        model = await self._run(self._DT)
        assert model._hourly_pattern[self._expected_key()] == pytest.approx(2.5)

    async def test_iso_string_start(self):
        model = await self._run(self._DT.isoformat())
        assert model._hourly_pattern[self._expected_key()] == pytest.approx(2.5)

    async def test_unparseable_start_is_skipped(self):
        """Garbage timestamps are dropped without raising."""
        model = await self._run("not-a-timestamp")
        assert model._hourly_pattern == {}
