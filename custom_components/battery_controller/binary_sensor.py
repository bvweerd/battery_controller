"""Binary sensor platform for Battery Controller integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OptimizationCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Controller binary sensors from a config entry."""
    data = entry.runtime_data
    if data is None:
        return
    optimization_coordinator = data.optimization_coordinator
    device = data.device

    async_add_entities(
        [
            PVCurtailmentSensor(optimization_coordinator, device, entry),
            UseMaxPowerSensor(optimization_coordinator, device, entry),
        ]
    )


class BatteryControllerBinarySensor(
    CoordinatorEntity[OptimizationCoordinator], BinarySensorEntity
):
    """Base class for Battery Controller binary sensors."""

    _attr_has_entity_name = True
    coordinator: OptimizationCoordinator

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        key: str,
    ):
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_device_info = device
        self._attr_unique_id = f"{entry.entry_id}_{key}"


class PVCurtailmentSensor(BatteryControllerBinarySensor):
    """Suggests curtailing PV production while the feed-in price is negative.

    Purely price-based: ON when the current feed-in price < 0 (exporting costs
    money), OFF otherwise. Battery state is deliberately not consulted — the
    sensor signals the price condition; whether curtailment is executed (and
    how the battery is used during it) is left to the user's automation and
    the controller's own PV-curtailed handling.
    """

    _attr_translation_key = "pv_curtailment"
    _attr_name = "PV Curtailment Suggested"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, device, entry, "pv_curtailment")

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None

        feed_in_price = self.coordinator.data.get("current_feed_in_price", 0.0)
        return bool(feed_in_price < 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        feed_in_price = self.coordinator.data.get("current_feed_in_price")
        battery_state = self.coordinator.data.get("battery_state")
        control_action = self.coordinator.data.get("control_action", {})
        attrs: dict[str, Any] = {
            "current_feed_in_price": feed_in_price,
        }
        if battery_state is not None:
            attrs["battery_soc_percent"] = round(battery_state.soc_percent, 1)
            attrs["battery_power_kw"] = round(battery_state.power_kw, 3)
        setpoint_w = control_action.get("target_power_w", 0.0)
        if setpoint_w is not None:
            attrs["charge_setpoint_w"] = round(setpoint_w, 0)
        return attrs


class UseMaxPowerSensor(BatteryControllerBinarySensor):
    """Suggests using maximum power when the grid consumption price is negative.

    When you are paid to consume electricity, it makes sense to run all
    flexible loads and charge the battery at full rate.

    ON when: current grid buy price < 0.
    """

    _attr_translation_key = "use_max_power"
    _attr_name = "Use Maximum Power Suggested"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, device, entry, "use_max_power")

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        buy_price = float(self.coordinator.data.get("current_price", 0.0))
        return buy_price < 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {
            "current_buy_price": self.coordinator.data.get("current_price"),
        }
