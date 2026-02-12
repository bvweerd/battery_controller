"""Tests for forecast_models.py."""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.battery_controller.forecast_models import (
    PVForecastModel,
    ConsumptionForecastModel,
    NetLoadForecast,
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

    async def test_datetime_start_field_handled(self):
        """_ts_and_value handles datetime objects (not just strings) as start."""
        from datetime import datetime, timezone

        hass = MagicMock()
        model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=["sensor.consumption"],
        )
        dt_start = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        base_stats = {
            "sensor.consumption": [{"start": dt_start, "change": 3.0}]
        }
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(return_value=base_stats)

        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=mock_instance,
        ):
            await model.async_update_pattern()

        assert (10, 0) in model._hourly_pattern
        assert model._hourly_pattern[(10, 0)] == pytest.approx(3.0)
