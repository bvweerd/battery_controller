"""Heat loss sensor using heat calculation coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ...entity import BaseUtilitySensor


class CoordinatorHeatLossSensor(CoordinatorEntity, BaseUtilitySensor):
    """Heat loss sensor using heat calculation coordinator."""

    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self, coordinator, name: str, unique_id: str, icon: str, device: DeviceInfo
    ):
        """Initialize the sensor."""
        CoordinatorEntity.__init__(self, coordinator)
        BaseUtilitySensor.__init__(
            self,
            name=name,
            unique_id=unique_id,
            unit="kW",
            device_class=None,
            icon=icon,
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_should_poll = False

    @property
    def native_value(self):
        """Return heat loss."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("heat_loss")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast and diagnostic attributes."""
        if not self.coordinator.data:
            return {}

        from ...const import (
            CONF_AREA_M2,
            CONF_ENERGY_LABEL,
            CONF_VENTILATION_TYPE,
            CONF_CEILING_HEIGHT,
            DEFAULT_VENTILATION_TYPE,
            DEFAULT_CEILING_HEIGHT,
            VENTILATION_TYPES,
            calculate_htc_from_energy_label,
            calculate_ventilation_htc,
        )

        config = self.coordinator.config
        area_m2 = config.get(CONF_AREA_M2, 0)
        energy_label = config.get(CONF_ENERGY_LABEL, "C")
        ventilation_type = config.get(CONF_VENTILATION_TYPE, DEFAULT_VENTILATION_TYPE)
        ceiling_height = config.get(CONF_CEILING_HEIGHT, DEFAULT_CEILING_HEIGHT)

        htc = calculate_htc_from_energy_label(
            energy_label,
            area_m2,
            ventilation_type=ventilation_type,
            ceiling_height=ceiling_height,
        )
        h_t = calculate_htc_from_energy_label(
            energy_label, area_m2, ventilation_type="none", ceiling_height=2.5
        ) - calculate_ventilation_htc(area_m2, "none", 2.5)
        h_v = calculate_ventilation_htc(area_m2, ventilation_type, ceiling_height)

        vent_data = VENTILATION_TYPES.get(ventilation_type, {})
        vent_name = vent_data.get("name_en", ventilation_type)
        volume = area_m2 * ceiling_height
        ach = vent_data.get("ach", 1.0)

        return {
            "forecast": self.coordinator.data.get("heat_loss_forecast", []),
            "forecast_time_base": 60,
            "htc_total_w_per_k": round(htc, 1),
            "htc_transmission_w_per_k": round(h_t, 1),
            "htc_ventilation_w_per_k": round(h_v, 1),
            "transmission_percentage": round(h_t / htc * 100, 1) if htc > 0 else 0,
            "ventilation_percentage": round(h_v / htc * 100, 1) if htc > 0 else 0,
            "energy_label": energy_label,
            "ventilation_type": ventilation_type,
            "ventilation_type_name": vent_name,
            "ceiling_height_m": ceiling_height,
            "building_volume_m3": round(volume, 1),
            "air_changes_per_hour": ach,
            "calculation_method": "HTC from energy label (NTA 8800) + ISO 13789 ventilation",
        }
