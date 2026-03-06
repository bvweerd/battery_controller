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
            "homeassistant.components.recorder.get_instance",
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
                "homeassistant.components.recorder.get_instance",
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
                "homeassistant.components.recorder.get_instance",
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
                "homeassistant.components.recorder.get_instance",
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
        """_ts_and_value handles datetime objects (not just strings) as start."""
        from datetime import datetime, timezone

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        dt_start = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        base_stats = {"sensor.consumption": [{"start": dt_start, "change": 3.0}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=base_stats)

        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert (10, 0) in model._hourly_pattern
        assert model._hourly_pattern[(10, 0)] == pytest.approx(3.0)


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

    # 2024-01-01 is a Monday (weekday=0), hour=10 → key=(10, 0)
    _TS = "2024-01-01T10:00:00"
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
            "homeassistant.components.recorder.get_instance",
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
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert model.has_data() is True
        assert (10, 0) in model._simple_pattern
        assert 0.25 in model._simple_pattern[(10, 0)]
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
                "homeassistant.components.recorder.get_instance",
                return_value=mock_instance,
            ),
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                return_value=mock_ent_reg,
            ),
        ):
            await model.async_update_pattern()

        assert model.has_data() is True
        # Weather key: (hour=10, dow=0, ghi_bin=3, wind_bin=2)
        assert (10, 0, 3, 2) in model._weather_pattern
        assert 0.15 in model._weather_pattern[(10, 0, 3, 2)]

    async def test_datetime_start_field_handled(self):
        hass = MagicMock()
        model = PriceForecastModel(
            hass=hass, price_sensor_id="sensor.price", entry_id=None
        )
        price_stats = {"sensor.price": [{"start": self._DT, "mean": 0.18}]}
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=price_stats)

        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert (10, 0) in model._simple_pattern
        assert 0.18 in model._simple_pattern[(10, 0)]

    async def test_recorder_import_error_handled_gracefully(self):
        hass = MagicMock()
        model = PriceForecastModel(hass=hass, price_sensor_id="sensor.price")

        with patch(
            "homeassistant.components.recorder.get_instance",
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
        # GHI=600 → bin 3, wind=10 → bin 2 → should use weather pattern (avg 0.10)
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
            ghi_forecast=[600.0],
            wind_forecast=[10.0],
        )
        assert result[0] == pytest.approx(0.10)

    def test_falls_back_to_simple_when_weather_bin_sparse(self):
        model = self._model_with_data()
        # GHI=10 → bin 0, wind=1 → bin 0 → no weather pattern for (10,0,0,0) → simple
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
            ghi_forecast=[10.0],
            wind_forecast=[1.0],
        )
        assert result[0] == pytest.approx(0.30)

    def test_falls_back_to_overall_avg_when_no_pattern(self):
        model = self._model_with_data()
        # hour=15 has no (15, 0) entry → falls back to overall avg (0.25)
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc),
        )
        assert result[0] == pytest.approx(0.25)

    def test_no_weather_args_uses_simple_pattern(self):
        model = self._model_with_data()
        # No GHI/wind provided → skips weather lookup → uses simple pattern
        result = model.forecast(
            hours=1,
            start_time=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        assert result[0] == pytest.approx(0.30)

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
