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
    section = None  # type: ignore[assignment]

from .const import (
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_ELECTRICITY_CONSUMPTION_SENSORS,
    CONF_ELECTRICITY_PRODUCTION_SENSORS,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_OPTIMIZATION_INTERVAL_MINUTES,
    CONF_PRICE_SENSOR,
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_EFFICIENCY,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_PV_EFFICIENCY_FACTOR,
    CONF_PV_ORIENTATION,
    CONF_PV_PEAK_POWER_KWP,
    CONF_PV_TILT,
    CONF_PV2_ORIENTATION,
    CONF_PV2_PEAK_POWER_KWP,
    CONF_PV2_TILT,
    CONF_PV3_ORIENTATION,
    CONF_PV3_PEAK_POWER_KWP,
    CONF_PV3_TILT,
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
    DEFAULT_PV2_ORIENTATION,
    DEFAULT_PV2_PEAK_POWER_KWP,
    DEFAULT_PV2_TILT,
    DEFAULT_PV3_ORIENTATION,
    DEFAULT_PV3_PEAK_POWER_KWP,
    DEFAULT_PV3_TILT,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    DEFAULT_TIME_STEP_MINUTES,
    DEFAULT_ZERO_GRID_ENABLED,
    DOMAIN,
)


def _opt_entity(key: str, val: Any) -> vol.Optional:
    """Create vol.Optional, omitting default when None to avoid selector validation."""
    if val:
        return vol.Optional(key, default=val)
    return vol.Optional(key)


def _req_entity(key: str, val: Any) -> vol.Required:
    """Create vol.Required, omitting default when None to avoid selector validation."""
    if val:
        return vol.Required(key, default=val)
    return vol.Required(key)


def _build_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build a single-form schema with collapsible sections.

    When ``defaults`` is provided (options flow), current values are pre-filled.
    When ``defaults`` is None (initial config), sensible defaults are used.
    """
    d = defaults or {}

    battery_schema = vol.Schema(
        {
            vol.Required(
                CONF_CAPACITY_KWH,
                default=d.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_CHARGE_POWER_KW,
                default=d.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_DISCHARGE_POWER_KW,
                default=d.get(
                    CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW
                ),
            ): vol.Coerce(float),
            vol.Required(
                CONF_ROUND_TRIP_EFFICIENCY,
                default=d.get(
                    CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY
                ),
            ): vol.Coerce(float),
        }
    )

    sensors_schema = vol.Schema(
        {
            _req_entity(
                CONF_PRICE_SENSOR, d.get(CONF_PRICE_SENSOR)
            ): selector({"entity": {"domain": "sensor"}}),
            _req_entity(
                CONF_BATTERY_SOC_SENSOR, d.get(CONF_BATTERY_SOC_SENSOR)
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
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_ORIENTATION,
                default=d.get(CONF_PV_ORIENTATION, DEFAULT_PV_ORIENTATION),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_TILT,
                default=d.get(CONF_PV_TILT, DEFAULT_PV_TILT),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_EFFICIENCY_FACTOR,
                default=d.get(CONF_PV_EFFICIENCY_FACTOR, DEFAULT_PV_EFFICIENCY_FACTOR),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_DC_COUPLED,
                default=d.get(CONF_PV_DC_COUPLED, DEFAULT_PV_DC_COUPLED),
            ): bool,
            vol.Optional(
                CONF_PV_DC_PEAK_POWER_KWP,
                default=d.get(
                    CONF_PV_DC_PEAK_POWER_KWP, DEFAULT_PV_DC_PEAK_POWER_KWP
                ),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV_DC_EFFICIENCY,
                default=d.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY),
            ): vol.Coerce(float),
        }
    )

    pv2_schema = vol.Schema(
        {
            vol.Optional(
                CONF_PV2_PEAK_POWER_KWP,
                default=d.get(CONF_PV2_PEAK_POWER_KWP, DEFAULT_PV2_PEAK_POWER_KWP),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV2_ORIENTATION,
                default=d.get(CONF_PV2_ORIENTATION, DEFAULT_PV2_ORIENTATION),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV2_TILT,
                default=d.get(CONF_PV2_TILT, DEFAULT_PV2_TILT),
            ): vol.Coerce(float),
        }
    )

    pv3_schema = vol.Schema(
        {
            vol.Optional(
                CONF_PV3_PEAK_POWER_KWP,
                default=d.get(CONF_PV3_PEAK_POWER_KWP, DEFAULT_PV3_PEAK_POWER_KWP),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV3_ORIENTATION,
                default=d.get(CONF_PV3_ORIENTATION, DEFAULT_PV3_ORIENTATION),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PV3_TILT,
                default=d.get(CONF_PV3_TILT, DEFAULT_PV3_TILT),
            ): vol.Coerce(float),
        }
    )

    energy_selector = selector(
        {"entity": {"domain": "sensor", "device_class": "energy", "multiple": True}}
    )
    consumption_sensors = d.get(CONF_ELECTRICITY_CONSUMPTION_SENSORS, [])
    production_sensors = d.get(CONF_ELECTRICITY_PRODUCTION_SENSORS, [])

    optional_sensors_schema = vol.Schema(
        {
            _opt_entity(
                CONF_FEED_IN_PRICE_SENSOR, d.get(CONF_FEED_IN_PRICE_SENSOR)
            ): selector({"entity": {"domain": "sensor"}}),
            _opt_entity(
                CONF_BATTERY_POWER_SENSOR, d.get(CONF_BATTERY_POWER_SENSOR)
            ): selector(
                {"entity": {"domain": "sensor", "device_class": "power"}}
            ),
            _opt_entity(
                CONF_ELECTRICITY_CONSUMPTION_SENSORS, consumption_sensors or None
            ): energy_selector,
            _opt_entity(
                CONF_ELECTRICITY_PRODUCTION_SENSORS, production_sensors or None
            ): energy_selector,
        }
    )

    advanced_schema = vol.Schema(
        {
            vol.Optional(
                CONF_TIME_STEP_MINUTES,
                default=d.get(CONF_TIME_STEP_MINUTES, DEFAULT_TIME_STEP_MINUTES),
            ): vol.Coerce(int),
            vol.Optional(
                CONF_OPTIMIZATION_INTERVAL_MINUTES,
                default=d.get(
                    CONF_OPTIMIZATION_INTERVAL_MINUTES,
                    DEFAULT_OPTIMIZATION_INTERVAL_MINUTES,
                ),
            ): vol.Coerce(int),
            vol.Optional(
                CONF_FIXED_FEED_IN_PRICE,
                default=d.get(CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_ZERO_GRID_ENABLED,
                default=d.get(CONF_ZERO_GRID_ENABLED, DEFAULT_ZERO_GRID_ENABLED),
            ): bool,
        }
    )

    if section is not None:
        fields: dict[Any, Any] = {
            vol.Required("battery"): section(
                battery_schema, {"collapsed": False}
            ),
            vol.Required("sensors"): section(
                sensors_schema, {"collapsed": False}
            ),
            vol.Optional("pv"): section(pv_schema, {"collapsed": True}),
            vol.Optional("pv2"): section(pv2_schema, {"collapsed": True}),
            vol.Optional("pv3"): section(pv3_schema, {"collapsed": True}),
            vol.Optional("optional_sensors"): section(
                optional_sensors_schema, {"collapsed": True}
            ),
            vol.Optional("advanced"): section(
                advanced_schema, {"collapsed": True}
            ),
        }
    else:
        # Fallback for older HA: flatten all fields into one form
        fields = {}
        for sub in [
            battery_schema,
            sensors_schema,
            pv_schema,
            pv2_schema,
            pv3_schema,
            optional_sensors_schema,
            advanced_schema,
        ]:
            fields.update(sub.schema)

    return vol.Schema(fields)


def _extract_data(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested section data into a flat config dict."""
    battery = user_input.get("battery", {})
    sensors = user_input.get("sensors", {})
    pv = user_input.get("pv", {})
    pv2 = user_input.get("pv2", {})
    pv3 = user_input.get("pv3", {})
    opt = user_input.get("optional_sensors", {})
    adv = user_input.get("advanced", {})

    def _g(sect: dict[str, Any], key: str, default: Any = None) -> Any:
        """Get from section dict, falling back to top-level (flat layout)."""
        return sect.get(key, user_input.get(key, default))

    return {
        # Battery
        CONF_CAPACITY_KWH: float(
            _g(battery, CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)
        ),
        CONF_MAX_CHARGE_POWER_KW: float(
            _g(battery, CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW)
        ),
        CONF_MAX_DISCHARGE_POWER_KW: float(
            _g(battery, CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW)
        ),
        CONF_ROUND_TRIP_EFFICIENCY: float(
            _g(battery, CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY)
        ),
        # PV
        CONF_PV_PEAK_POWER_KWP: float(
            _g(pv, CONF_PV_PEAK_POWER_KWP, DEFAULT_PV_PEAK_POWER_KWP)
        ),
        CONF_PV_ORIENTATION: float(
            _g(pv, CONF_PV_ORIENTATION, DEFAULT_PV_ORIENTATION)
        ),
        CONF_PV_TILT: float(_g(pv, CONF_PV_TILT, DEFAULT_PV_TILT)),
        CONF_PV_EFFICIENCY_FACTOR: float(
            _g(pv, CONF_PV_EFFICIENCY_FACTOR, DEFAULT_PV_EFFICIENCY_FACTOR)
        ),
        CONF_PV_DC_COUPLED: bool(
            _g(pv, CONF_PV_DC_COUPLED, DEFAULT_PV_DC_COUPLED)
        ),
        CONF_PV_DC_PEAK_POWER_KWP: float(
            _g(pv, CONF_PV_DC_PEAK_POWER_KWP, DEFAULT_PV_DC_PEAK_POWER_KWP)
        ),
        CONF_PV_DC_EFFICIENCY: float(
            _g(pv, CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY)
        ),
        # PV array 2
        CONF_PV2_PEAK_POWER_KWP: float(
            _g(pv2, CONF_PV2_PEAK_POWER_KWP, DEFAULT_PV2_PEAK_POWER_KWP)
        ),
        CONF_PV2_ORIENTATION: float(
            _g(pv2, CONF_PV2_ORIENTATION, DEFAULT_PV2_ORIENTATION)
        ),
        CONF_PV2_TILT: float(_g(pv2, CONF_PV2_TILT, DEFAULT_PV2_TILT)),
        # PV array 3
        CONF_PV3_PEAK_POWER_KWP: float(
            _g(pv3, CONF_PV3_PEAK_POWER_KWP, DEFAULT_PV3_PEAK_POWER_KWP)
        ),
        CONF_PV3_ORIENTATION: float(
            _g(pv3, CONF_PV3_ORIENTATION, DEFAULT_PV3_ORIENTATION)
        ),
        CONF_PV3_TILT: float(_g(pv3, CONF_PV3_TILT, DEFAULT_PV3_TILT)),
        # Required sensors
        CONF_PRICE_SENSOR: _g(sensors, CONF_PRICE_SENSOR),
        CONF_BATTERY_SOC_SENSOR: _g(sensors, CONF_BATTERY_SOC_SENSOR),
        # Optional sensors
        CONF_FEED_IN_PRICE_SENSOR: _g(opt, CONF_FEED_IN_PRICE_SENSOR),
        CONF_BATTERY_POWER_SENSOR: _g(opt, CONF_BATTERY_POWER_SENSOR),
        CONF_ELECTRICITY_CONSUMPTION_SENSORS: _g(
            opt, CONF_ELECTRICITY_CONSUMPTION_SENSORS, []
        ),
        CONF_ELECTRICITY_PRODUCTION_SENSORS: _g(
            opt, CONF_ELECTRICITY_PRODUCTION_SENSORS, []
        ),
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


class BatteryControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Battery Controller."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup — single form with sections."""
        await self.async_set_unique_id(DOMAIN)
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            data = _extract_data(user_input)
            if not data.get(CONF_PRICE_SENSOR) or not data.get(
                CONF_BATTERY_SOC_SENSOR
            ):
                errors["base"] = "missing_required"
            else:
                return self.async_create_entry(
                    title="Battery Controller", data=data
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return BatteryControllerOptionsFlowHandler()


class BatteryControllerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Battery Controller."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration — single form with sections."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _extract_data(user_input)
            if not data.get(CONF_PRICE_SENSOR) or not data.get(
                CONF_BATTERY_SOC_SENSOR
            ):
                errors["base"] = "missing_required"
            else:
                return self.async_create_entry(title="", data=data)

        # Build defaults from existing config
        defaults: dict[str, Any] = {}
        for key, val in self.config_entry.data.items():
            defaults[key] = val
        for key, val in self.config_entry.options.items():
            defaults[key] = val

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults),
            errors=errors,
        )
