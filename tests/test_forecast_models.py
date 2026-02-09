"""Tests for forecast_models.py."""

import pytest
from unittest.mock import MagicMock

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
