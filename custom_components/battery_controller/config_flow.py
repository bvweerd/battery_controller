"""Config flow for Battery Controller integration."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries

try:
    from homeassistant.config_entries import ConfigFlowResult
except ImportError:
    ConfigFlowResult = dict[str, Any]  # type: ignore

from homeassistant.core import callback
from homeassistant.helpers.selector import selector
import voluptuous as vol

try:
    from homeassistant.data_entry_flow import section
except ImportError:
    section = None  # type: ignore[assignment,misc]

from .const import (
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_ELECTRICITY_CONSUMPTION_SENSORS,
    CONF_ELECTRICITY_PRODUCTION_SENSORS,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_PV_PRODUCTION_SENSORS,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_OPTIMIZATION_INTERVAL_MINUTES,
    CONF_POWER_CONSUMPTION_SENSORS,
    CONF_POWER_PRODUCTION_SENSORS,
    CONF_PRICE_SENSOR,
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_EFFICIENCY,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_PV_EFFICIENCY_FACTOR,
    CONF_PV_EXTRA_ARRAYS,
    CONF_PV_ORIENTATION,
    CONF_PV_PEAK_POWER_KWP,
    CONF_PV_TILT,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_TIME_STEP_MINUTES,
    CONF_ZERO_GRID_ENABLED,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_FIXED_FEED_IN_PRICE,
    DEFAULT_MAX_CHARGE_POWER_KW,
    DEFAULT_MAX_DISCHARGE_POWER_KW,
    DEFAULT_OPTIMIZATION_INTERVAL_MINUTES,
    DEFAULT_PV_DC_COUPLED,
    DEFAULT_PV_DC_EFFICIENCY,
    DEFAULT_PV_DC_PEAK_POWER_KWP,
    DEFAULT_PV_EFFICIENCY_FACTOR,
    DEFAULT_PV_ORIENTATION,
    DEFAULT_PV_PEAK_POWER_KWP,
    DEFAULT_PV_TILT,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    DEFAULT_TIME_STEP_MINUTES,
    DEFAULT_ZERO_GRID_ENABLED,
    DOMAIN,
)


def _build_main_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the main config form schema (without extra PV arrays).

    When ``defaults`` is provided (options flow), current values are pre-filled.
    When ``defaults`` is None (initial config), sensible defaults are used.
    """
    d = defaults or {}

    battery_schema = vol.Schema(
        {
            vol.Required(
                CONF_CAPACITY_KWH,
                default=d.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH),
                description={"suggested_value": d.get(CONF_CAPACITY_KWH)},
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_CHARGE_POWER_KW,
                default=d.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW),
                description={"suggested_value": d.get(CONF_MAX_CHARGE_POWER_KW)},
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_DISCHARGE_POWER_KW,
                default=d.get(
                    CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW
                ),
                description={"suggested_value": d.get(CONF_MAX_DISCHARGE_POWER_KW)},
            ): vol.Coerce(float),
            vol.Required(
                CONF_ROUND_TRIP_EFFICIENCY,
                default=d.get(
                    CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY
                ),
                description={"suggested_value": d.get(CONF_ROUND_TRIP_EFFICIENCY)},
            ): vol.Coerce(float),
        }
    )

    sensors_schema = vol.Schema(
        {
            vol.Required(
                CONF_PRICE_SENSOR,
                description={"suggested_value": d.get(CONF_PRICE_SENSOR)},
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                CONF_BATTERY_SOC_SENSOR,
                description={"suggested_value": d.get(CONF_BATTERY_SOC_SENSOR)},
            ): selector(
                {
                    "entity": {
                        "filter": [
                            {"domain": "sensor", "device_class": "battery"},
                            {"domain": "sensor", "device_class": "energy"},
                            {"domain": "number"},
                        ]
                    }
                }
            ),
        }
    )

    pv_schema = vol.Schema(
        {
            vol.Optional(
                CONF_PV_PEAK_POWER_KWP,
                default=d.get(CONF_PV_PEAK_POWER_KWP, DEFAULT_PV_PEAK_POWER_KWP),
                description={"suggested_value": d.get(CONF_PV_PEAK_POWER_KWP)},
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_ORIENTATION,
                default=d.get(CONF_PV_ORIENTATION, DEFAULT_PV_ORIENTATION),
                description={"suggested_value": d.get(CONF_PV_ORIENTATION)},
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_TILT,
                default=d.get(CONF_PV_TILT, DEFAULT_PV_TILT),
                description={"suggested_value": d.get(CONF_PV_TILT)},
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_EFFICIENCY_FACTOR,
                default=d.get(CONF_PV_EFFICIENCY_FACTOR, DEFAULT_PV_EFFICIENCY_FACTOR),
                description={"suggested_value": d.get(CONF_PV_EFFICIENCY_FACTOR)},
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_DC_COUPLED,
                default=d.get(CONF_PV_DC_COUPLED, DEFAULT_PV_DC_COUPLED),
                description={"suggested_value": d.get(CONF_PV_DC_COUPLED)},
            ): bool,
            vol.Optional(
                CONF_PV_DC_PEAK_POWER_KWP,
                default=d.get(CONF_PV_DC_PEAK_POWER_KWP, DEFAULT_PV_DC_PEAK_POWER_KWP),
                description={"suggested_value": d.get(CONF_PV_DC_PEAK_POWER_KWP)},
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_DC_EFFICIENCY,
                default=d.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY),
                description={"suggested_value": d.get(CONF_PV_DC_EFFICIENCY)},
            ): vol.Coerce(float),
        }
    )

    energy_selector = selector(
        {"entity": {"domain": "sensor", "device_class": "energy", "multiple": True}}
    )

    power_selector = selector(
        {"entity": {"domain": "sensor", "device_class": "power", "multiple": True}}
    )

    optional_sensors_schema = vol.Schema(
        {
            vol.Optional(
                CONF_FEED_IN_PRICE_SENSOR,
                description={"suggested_value": d.get(CONF_FEED_IN_PRICE_SENSOR)},
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                CONF_BATTERY_POWER_SENSOR,
                description={"suggested_value": d.get(CONF_BATTERY_POWER_SENSOR)},
            ): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
            vol.Optional(
                CONF_POWER_CONSUMPTION_SENSORS,
                description={"suggested_value": d.get(CONF_POWER_CONSUMPTION_SENSORS)},
            ): power_selector,
            vol.Optional(
                CONF_POWER_PRODUCTION_SENSORS,
                description={"suggested_value": d.get(CONF_POWER_PRODUCTION_SENSORS)},
            ): power_selector,
            vol.Optional(
                CONF_ELECTRICITY_CONSUMPTION_SENSORS,
                description={
                    "suggested_value": d.get(CONF_ELECTRICITY_CONSUMPTION_SENSORS)
                },
            ): energy_selector,
            vol.Optional(
                CONF_ELECTRICITY_PRODUCTION_SENSORS,
                description={
                    "suggested_value": d.get(CONF_ELECTRICITY_PRODUCTION_SENSORS)
                },
            ): energy_selector,
            vol.Optional(
                CONF_PV_PRODUCTION_SENSORS,
                description={"suggested_value": d.get(CONF_PV_PRODUCTION_SENSORS)},
            ): energy_selector,
        }
    )

    advanced_schema = vol.Schema(
        {
            vol.Optional(
                CONF_TIME_STEP_MINUTES,
                default=d.get(CONF_TIME_STEP_MINUTES, DEFAULT_TIME_STEP_MINUTES),
                description={"suggested_value": d.get(CONF_TIME_STEP_MINUTES)},
            ): vol.Coerce(int),
            vol.Optional(
                CONF_OPTIMIZATION_INTERVAL_MINUTES,
                default=d.get(
                    CONF_OPTIMIZATION_INTERVAL_MINUTES,
                    DEFAULT_OPTIMIZATION_INTERVAL_MINUTES,
                ),
                description={
                    "suggested_value": d.get(CONF_OPTIMIZATION_INTERVAL_MINUTES)
                },
            ): vol.Coerce(int),
            vol.Optional(
                CONF_FIXED_FEED_IN_PRICE,
                default=d.get(CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE),
                description={"suggested_value": d.get(CONF_FIXED_FEED_IN_PRICE)},
            ): vol.Coerce(float),
            vol.Optional(
                CONF_ZERO_GRID_ENABLED,
                default=d.get(CONF_ZERO_GRID_ENABLED, DEFAULT_ZERO_GRID_ENABLED),
                description={"suggested_value": d.get(CONF_ZERO_GRID_ENABLED)},
            ): bool,
        }
    )

    if section is not None:
        fields: dict[Any, Any] = {
            vol.Required("battery"): section(battery_schema, {"collapsed": False}),
            vol.Required("sensors"): section(sensors_schema, {"collapsed": False}),
            vol.Optional("pv"): section(pv_schema, {"collapsed": True}),
            vol.Optional("optional_sensors"): section(
                optional_sensors_schema, {"collapsed": True}
            ),
            vol.Optional("advanced"): section(advanced_schema, {"collapsed": True}),
        }
    else:
        # Fallback for older HA: flatten all fields into one form
        fields = {}
        for sub in [
            battery_schema,
            sensors_schema,
            pv_schema,
            optional_sensors_schema,
            advanced_schema,
        ]:
            fields.update(sub.schema)

    return vol.Schema(fields)


def _build_pv_array_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build schema for adding/editing a single extra PV array."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                "peak_power_kwp",
                default=d.get("peak_power_kwp", 0.0),
                description={"suggested_value": d.get("peak_power_kwp")},
            ): vol.Coerce(float),
            vol.Required(
                "orientation",
                default=d.get("orientation", 180),
                description={"suggested_value": d.get("orientation")},
            ): vol.Coerce(float),
            vol.Required(
                "tilt",
                default=d.get("tilt", 35),
                description={"suggested_value": d.get("tilt")},
            ): vol.Coerce(float),
            vol.Optional(
                "dc_coupled",
                default=d.get("dc_coupled", False),
                description={"suggested_value": d.get("dc_coupled")},
            ): bool,
        }
    )


def _extract_main_data(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested section data into a flat config dict (without extra PV arrays)."""
    battery = user_input.get("battery", {})
    sensors = user_input.get("sensors", {})
    pv = user_input.get("pv", {})
    opt = user_input.get("optional_sensors", {})
    adv = user_input.get("advanced", {})

    def _g(sect: dict[str, Any], key: str, default: Any = None) -> Any:
        """Get from section dict, falling back to top-level (flat layout)."""
        return sect.get(key, user_input.get(key, default))

    return {
        # Battery
        CONF_CAPACITY_KWH: float(_g(battery, CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)),
        CONF_MAX_CHARGE_POWER_KW: float(
            _g(battery, CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW)
        ),
        CONF_MAX_DISCHARGE_POWER_KW: float(
            _g(battery, CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW)
        ),
        CONF_ROUND_TRIP_EFFICIENCY: float(
            _g(battery, CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY)
        ),
        # PV (primary)
        CONF_PV_PEAK_POWER_KWP: float(
            _g(pv, CONF_PV_PEAK_POWER_KWP, DEFAULT_PV_PEAK_POWER_KWP)
        ),
        CONF_PV_ORIENTATION: float(_g(pv, CONF_PV_ORIENTATION, DEFAULT_PV_ORIENTATION)),
        CONF_PV_TILT: float(_g(pv, CONF_PV_TILT, DEFAULT_PV_TILT)),
        CONF_PV_EFFICIENCY_FACTOR: float(
            _g(pv, CONF_PV_EFFICIENCY_FACTOR, DEFAULT_PV_EFFICIENCY_FACTOR)
        ),
        CONF_PV_DC_COUPLED: bool(_g(pv, CONF_PV_DC_COUPLED, DEFAULT_PV_DC_COUPLED)),
        CONF_PV_DC_PEAK_POWER_KWP: float(
            _g(pv, CONF_PV_DC_PEAK_POWER_KWP, DEFAULT_PV_DC_PEAK_POWER_KWP)
        ),
        CONF_PV_DC_EFFICIENCY: float(
            _g(pv, CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY)
        ),
        # Required sensors
        CONF_PRICE_SENSOR: _g(sensors, CONF_PRICE_SENSOR),
        CONF_BATTERY_SOC_SENSOR: _g(sensors, CONF_BATTERY_SOC_SENSOR),
        # Optional sensors
        CONF_FEED_IN_PRICE_SENSOR: _g(opt, CONF_FEED_IN_PRICE_SENSOR),
        CONF_BATTERY_POWER_SENSOR: _g(opt, CONF_BATTERY_POWER_SENSOR),
        CONF_POWER_CONSUMPTION_SENSORS: _g(opt, CONF_POWER_CONSUMPTION_SENSORS, []),
        CONF_POWER_PRODUCTION_SENSORS: _g(opt, CONF_POWER_PRODUCTION_SENSORS, []),
        CONF_ELECTRICITY_CONSUMPTION_SENSORS: _g(
            opt, CONF_ELECTRICITY_CONSUMPTION_SENSORS, []
        ),
        CONF_ELECTRICITY_PRODUCTION_SENSORS: _g(
            opt, CONF_ELECTRICITY_PRODUCTION_SENSORS, []
        ),
        CONF_PV_PRODUCTION_SENSORS: _g(opt, CONF_PV_PRODUCTION_SENSORS, []),
        # Advanced
        CONF_TIME_STEP_MINUTES: int(
            _g(adv, CONF_TIME_STEP_MINUTES, DEFAULT_TIME_STEP_MINUTES)
        ),
        CONF_OPTIMIZATION_INTERVAL_MINUTES: int(
            _g(
                adv,
                CONF_OPTIMIZATION_INTERVAL_MINUTES,
                DEFAULT_OPTIMIZATION_INTERVAL_MINUTES,
            )
        ),
        CONF_FIXED_FEED_IN_PRICE: float(
            _g(adv, CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE)
        ),
        CONF_ZERO_GRID_ENABLED: bool(
            _g(adv, CONF_ZERO_GRID_ENABLED, DEFAULT_ZERO_GRID_ENABLED)
        ),
    }


def _pv_array_description(arr: dict[str, Any], index: int) -> str:
    """Build a human-readable description for a PV array."""
    kwp = arr.get("peak_power_kwp", 0)
    orient = arr.get("orientation", 180)
    return f"PV Array {index} ({kwp} kWp, {orient}°)"


class BatteryControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Battery Controller."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize config flow."""
        self._data: dict[str, Any] = {}
        self._pv_extra_arrays: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup - main form with sections."""
        await self.async_set_unique_id(DOMAIN)
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            data = _extract_main_data(user_input)
            if not data.get(CONF_PRICE_SENSOR) or not data.get(CONF_BATTERY_SOC_SENSOR):
                errors["base"] = "missing_required"
            else:
                self._data = data
                self._pv_extra_arrays = []
                return await self.async_step_pv_menu()

        return self.async_show_form(
            step_id="user",
            data_schema=_build_main_schema(),
            errors=errors,
        )

    async def async_step_pv_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show menu to add extra PV arrays or finish."""
        menu_options = ["add_pv_array", "finish_setup"]
        if self._pv_extra_arrays:
            menu_options.insert(1, "remove_pv_array")

        return self.async_show_menu(
            step_id="pv_menu",
            menu_options=menu_options,
            description_placeholders={
                "pv_count": str(len(self._pv_extra_arrays)),
            },
        )

    async def async_step_add_pv_array(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an extra PV array."""
        if user_input is not None:
            self._pv_extra_arrays.append(
                {
                    "peak_power_kwp": float(user_input["peak_power_kwp"]),
                    "orientation": float(user_input["orientation"]),
                    "tilt": float(user_input["tilt"]),
                    "dc_coupled": bool(user_input.get("dc_coupled", False)),
                }
            )
            return await self.async_step_pv_menu()

        return self.async_show_form(
            step_id="add_pv_array",
            data_schema=_build_pv_array_schema(),
            description_placeholders={
                "array_number": str(len(self._pv_extra_arrays) + 2),
            },
        )

    async def async_step_remove_pv_array(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove the last extra PV array."""
        if self._pv_extra_arrays:
            self._pv_extra_arrays.pop()
        return await self.async_step_pv_menu()

    async def async_step_finish_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish the config flow and create the entry."""
        self._data[CONF_PV_EXTRA_ARRAYS] = self._pv_extra_arrays
        return self.async_create_entry(title="Battery Controller", data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return BatteryControllerOptionsFlowHandler()


class BatteryControllerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Battery Controller."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._data: dict[str, Any] = {}
        self._pv_extra_arrays: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration - main form with sections."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _extract_main_data(user_input)
            if not data.get(CONF_PRICE_SENSOR) or not data.get(CONF_BATTERY_SOC_SENSOR):
                errors["base"] = "missing_required"
            else:
                self._data = data
                # Preserve existing extra arrays for editing
                existing = {**self.config_entry.data, **self.config_entry.options}
                self._pv_extra_arrays = list(existing.get(CONF_PV_EXTRA_ARRAYS, []))
                return await self.async_step_pv_menu()

        # Build defaults from existing config
        defaults: dict[str, Any] = {}
        for key, val in self.config_entry.data.items():
            defaults[key] = val
        for key, val in self.config_entry.options.items():
            defaults[key] = val

        return self.async_show_form(
            step_id="init",
            data_schema=_build_main_schema(defaults),
            errors=errors,
        )

    async def async_step_pv_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show menu to manage extra PV arrays."""
        menu_options = ["add_pv_array", "finish_setup"]
        if self._pv_extra_arrays:
            menu_options.insert(1, "remove_pv_array")

        return self.async_show_menu(
            step_id="pv_menu",
            menu_options=menu_options,
            description_placeholders={
                "pv_count": str(len(self._pv_extra_arrays)),
            },
        )

    async def async_step_add_pv_array(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an extra PV array."""
        if user_input is not None:
            self._pv_extra_arrays.append(
                {
                    "peak_power_kwp": float(user_input["peak_power_kwp"]),
                    "orientation": float(user_input["orientation"]),
                    "tilt": float(user_input["tilt"]),
                    "dc_coupled": bool(user_input.get("dc_coupled", False)),
                }
            )
            return await self.async_step_pv_menu()

        return self.async_show_form(
            step_id="add_pv_array",
            data_schema=_build_pv_array_schema(),
            description_placeholders={
                "array_number": str(len(self._pv_extra_arrays) + 2),
            },
        )

    async def async_step_remove_pv_array(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove the last extra PV array."""
        if self._pv_extra_arrays:
            self._pv_extra_arrays.pop()
        return await self.async_step_pv_menu()

    async def async_step_finish_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish the options flow."""
        self._data[CONF_PV_EXTRA_ARRAYS] = self._pv_extra_arrays
        return self.async_create_entry(title="", data=self._data)
