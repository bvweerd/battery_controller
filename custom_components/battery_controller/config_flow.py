"""Config flow for Battery Controller integration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult, SubentryFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector
import voluptuous as vol

from .const import (
    BATTERY_SUBENTRY_TYPE,
    CONF_BATTERY_ENERGY_CHARGED_SENSOR,
    CONF_BATTERY_ENERGY_DISCHARGED_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_NAME,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_DEGRADATION_COST_PER_CYCLE,
    CONF_ELECTRICITY_CONSUMPTION_SENSORS,
    CONF_ELECTRICITY_PRODUCTION_SENSORS,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_PV_PRODUCTION_SENSORS,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT,
    CONF_HIGH_SOC_MAX_CHARGE_KW,
    CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
    CONF_LOW_SOC_MAX_DISCHARGE_KW,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MAX_SOC_PERCENT,
    CONF_MIN_PRICE_SPREAD,
    CONF_MIN_SOC_PERCENT,
    CONF_POWER_CONSUMPTION_SENSORS,
    CONF_POWER_PRODUCTION_SENSORS,
    CONF_CHARGE_EFFICIENCY_CURVE,
    CONF_DISCHARGE_EFFICIENCY_CURVE,
    CONF_PRICE_SENSOR,
    CONF_PV_DC_EFFICIENCY,
    CONF_PV_FORECAST_SENSORS,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_MAX_GRID_POWER_KW,
    CONF_ZERO_GRID_DEADBAND_W,
    CONF_ZERO_GRID_ENABLED,
    CONF_ZERO_GRID_RESPONSE_TIME_S,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_FIXED_FEED_IN_PRICE,
    DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT,
    DEFAULT_HIGH_SOC_MAX_CHARGE_KW,
    DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
    DEFAULT_LOW_SOC_MAX_DISCHARGE_KW,
    DEFAULT_MAX_CHARGE_POWER_KW,
    DEFAULT_MAX_DISCHARGE_POWER_KW,
    DEFAULT_MAX_SOC_PERCENT,
    DEFAULT_MIN_SOC_PERCENT,
    DEFAULT_CHARGE_EFFICIENCY_CURVE,
    DEFAULT_DISCHARGE_EFFICIENCY_CURVE,
    DEFAULT_MAX_GRID_POWER_KW,
    DEFAULT_PV_DC_EFFICIENCY,
    DEFAULT_PV_ORIENTATION_DEG,
    DEFAULT_PV_TILT_DEG,
    DEFAULT_ZERO_GRID_ENABLED,
    DEFAULT_ZERO_GRID_RESPONSE_TIME_S,
    DOMAIN,
    PV_SUBENTRY_TYPE,
)
from .efficiency_curve import parse_efficiency_curve


def _build_battery_subentry_schema(
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build schema for a single battery subentry (add or edit)."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_NAME,
                description={"suggested_value": d.get(CONF_NAME)},
            ): str,
            vol.Required(
                CONF_CAPACITY_KWH,
                default=d.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH),
                description={"suggested_value": d.get(CONF_CAPACITY_KWH)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1000.0)),
            vol.Required(
                CONF_MAX_CHARGE_POWER_KW,
                default=d.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW),
                description={"suggested_value": d.get(CONF_MAX_CHARGE_POWER_KW)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1000.0)),
            vol.Required(
                CONF_MAX_DISCHARGE_POWER_KW,
                default=d.get(
                    CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW
                ),
                description={"suggested_value": d.get(CONF_MAX_DISCHARGE_POWER_KW)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1000.0)),
            vol.Required(
                CONF_CHARGE_EFFICIENCY_CURVE,
                default=d.get(
                    CONF_CHARGE_EFFICIENCY_CURVE, DEFAULT_CHARGE_EFFICIENCY_CURVE
                ),
                description={"suggested_value": d.get(CONF_CHARGE_EFFICIENCY_CURVE)},
            ): str,
            vol.Required(
                CONF_DISCHARGE_EFFICIENCY_CURVE,
                default=d.get(
                    CONF_DISCHARGE_EFFICIENCY_CURVE, DEFAULT_DISCHARGE_EFFICIENCY_CURVE
                ),
                description={"suggested_value": d.get(CONF_DISCHARGE_EFFICIENCY_CURVE)},
            ): str,
            vol.Required(
                CONF_MIN_SOC_PERCENT,
                default=d.get(CONF_MIN_SOC_PERCENT, DEFAULT_MIN_SOC_PERCENT),
                description={"suggested_value": d.get(CONF_MIN_SOC_PERCENT)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Required(
                CONF_MAX_SOC_PERCENT,
                default=d.get(CONF_MAX_SOC_PERCENT, DEFAULT_MAX_SOC_PERCENT),
                description={"suggested_value": d.get(CONF_MAX_SOC_PERCENT)},
            ): vol.All(vol.Coerce(float), vol.Range(min=50.0, max=100.0)),
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
            vol.Optional(
                CONF_BATTERY_POWER_SENSOR,
                description={"suggested_value": d.get(CONF_BATTERY_POWER_SENSOR)},
            ): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
            vol.Optional(
                CONF_BATTERY_ENERGY_CHARGED_SENSOR,
                description={
                    "suggested_value": d.get(CONF_BATTERY_ENERGY_CHARGED_SENSOR)
                },
            ): selector({"entity": {"domain": "sensor", "device_class": "energy"}}),
            vol.Optional(
                CONF_BATTERY_ENERGY_DISCHARGED_SENSOR,
                description={
                    "suggested_value": d.get(CONF_BATTERY_ENERGY_DISCHARGED_SENSOR)
                },
            ): selector({"entity": {"domain": "sensor", "device_class": "energy"}}),
            vol.Optional(
                CONF_PV_DC_EFFICIENCY,
                default=d.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY),
                description={"suggested_value": d.get(CONF_PV_DC_EFFICIENCY)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=1.0)),
            vol.Optional(
                CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT,
                description={
                    "suggested_value": d.get(CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT)
                },
            ): vol.All(vol.Coerce(float), vol.Range(min=50.0, max=100.0)),
            vol.Optional(
                CONF_HIGH_SOC_MAX_CHARGE_KW,
                description={"suggested_value": d.get(CONF_HIGH_SOC_MAX_CHARGE_KW)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1000.0)),
            vol.Optional(
                CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
                description={
                    "suggested_value": d.get(CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT)
                },
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional(
                CONF_LOW_SOC_MAX_DISCHARGE_KW,
                description={"suggested_value": d.get(CONF_LOW_SOC_MAX_DISCHARGE_KW)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1000.0)),
        }
    )


def _validate_battery_subentry(user_input: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise battery subentry user input."""
    schema = _build_battery_subentry_schema()
    validated = schema(user_input)
    if validated[CONF_MIN_SOC_PERCENT] >= validated[CONF_MAX_SOC_PERCENT]:
        raise vol.Invalid("min_soc_percent must be less than max_soc_percent")
    max_charge_kw = float(validated[CONF_MAX_CHARGE_POWER_KW])
    max_discharge_kw = float(validated[CONF_MAX_DISCHARGE_POWER_KW])
    try:
        parse_efficiency_curve(validated[CONF_CHARGE_EFFICIENCY_CURVE], max_charge_kw)
    except (ValueError, TypeError) as exc:
        raise vol.Invalid(str(exc), path=[CONF_CHARGE_EFFICIENCY_CURVE]) from exc
    try:
        parse_efficiency_curve(
            validated[CONF_DISCHARGE_EFFICIENCY_CURVE], max_discharge_kw
        )
    except (ValueError, TypeError) as exc:
        raise vol.Invalid(str(exc), path=[CONF_DISCHARGE_EFFICIENCY_CURVE]) from exc
    if (
        CONF_HIGH_SOC_MAX_CHARGE_KW in validated
        and validated[CONF_HIGH_SOC_MAX_CHARGE_KW] > validated[CONF_MAX_CHARGE_POWER_KW]
    ):
        raise vol.Invalid(
            "high_soc_max_charge_kw must not exceed max_charge_power_kw",
            path=[CONF_HIGH_SOC_MAX_CHARGE_KW],
        )
    if (
        CONF_LOW_SOC_MAX_DISCHARGE_KW in validated
        and validated[CONF_LOW_SOC_MAX_DISCHARGE_KW]
        > validated[CONF_MAX_DISCHARGE_POWER_KW]
    ):
        raise vol.Invalid(
            "low_soc_max_discharge_kw must not exceed max_discharge_power_kw",
            path=[CONF_LOW_SOC_MAX_DISCHARGE_KW],
        )
    result: dict[str, Any] = {}
    if name := validated.get(CONF_NAME, "").strip():
        result[CONF_NAME] = name
    result.update(
        {
            CONF_CAPACITY_KWH: float(validated[CONF_CAPACITY_KWH]),
            CONF_MAX_CHARGE_POWER_KW: float(validated[CONF_MAX_CHARGE_POWER_KW]),
            CONF_MAX_DISCHARGE_POWER_KW: float(validated[CONF_MAX_DISCHARGE_POWER_KW]),
            CONF_CHARGE_EFFICIENCY_CURVE: str(validated[CONF_CHARGE_EFFICIENCY_CURVE]),
            CONF_DISCHARGE_EFFICIENCY_CURVE: str(
                validated[CONF_DISCHARGE_EFFICIENCY_CURVE]
            ),
            CONF_MIN_SOC_PERCENT: float(validated[CONF_MIN_SOC_PERCENT]),
            CONF_MAX_SOC_PERCENT: float(validated[CONF_MAX_SOC_PERCENT]),
            CONF_BATTERY_SOC_SENSOR: validated[CONF_BATTERY_SOC_SENSOR],
            CONF_PV_DC_EFFICIENCY: float(
                validated.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY)
            ),
        }
    )
    for optional_sensor in (
        CONF_BATTERY_POWER_SENSOR,
        CONF_BATTERY_ENERGY_CHARGED_SENSOR,
        CONF_BATTERY_ENERGY_DISCHARGED_SENSOR,
    ):
        if validated.get(optional_sensor):
            result[optional_sensor] = validated[optional_sensor]
    # SoC-dependent derating: only store when explicitly provided
    for key, default in (
        (CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT, DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT),
        (CONF_HIGH_SOC_MAX_CHARGE_KW, DEFAULT_HIGH_SOC_MAX_CHARGE_KW),
        (CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT, DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT),
        (CONF_LOW_SOC_MAX_DISCHARGE_KW, DEFAULT_LOW_SOC_MAX_DISCHARGE_KW),
    ):
        if key in validated:
            result[key] = float(validated[key])
    return result


def _battery_subentry_title(data: dict[str, Any]) -> str:
    """Generate a display title for a battery subentry."""
    if name := str(data.get(CONF_NAME, "")).strip():
        return name
    kwh = data.get(CONF_CAPACITY_KWH, 0)
    return f"{kwh} kWh"


def _build_pv_subentry_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build schema for a single PV array subentry (add or edit)."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_NAME,
                description={"suggested_value": d.get(CONF_NAME)},
            ): str,
            vol.Required(
                "peak_power_kwp",
                default=d.get("peak_power_kwp", 1.0),
                description={"suggested_value": d.get("peak_power_kwp")},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.01)),
            vol.Required(
                "orientation",
                default=d.get("orientation", DEFAULT_PV_ORIENTATION_DEG),
                description={"suggested_value": d.get("orientation")},
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=360)),
            vol.Required(
                "tilt",
                default=d.get("tilt", DEFAULT_PV_TILT_DEG),
                description={"suggested_value": d.get("tilt")},
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
            vol.Optional(
                "efficiency_factor",
                default=d.get("efficiency_factor", 0.85),
                description={"suggested_value": d.get("efficiency_factor")},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=1.0)),
            vol.Optional(
                "dc_coupled",
                default=d.get("dc_coupled", False),
                description={"suggested_value": d.get("dc_coupled")},
            ): bool,
            vol.Optional(
                CONF_PV_FORECAST_SENSORS,
                description={"suggested_value": d.get(CONF_PV_FORECAST_SENSORS)},
            ): selector({"entity": {"domain": "sensor", "multiple": True}}),
        }
    )


def _build_main_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the main config form schema.

    Battery hardware specs and sensors (SoC, power) are configured per-battery
    via battery subentries.  The main form only covers price/consumption sensors
    and advanced scheduling settings.
    """
    d = defaults or {}

    sensors_schema = vol.Schema(
        {
            vol.Required(
                CONF_PRICE_SENSOR,
                description={"suggested_value": d.get(CONF_PRICE_SENSOR)},
            ): selector({"entity": {"domain": "sensor"}}),
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
                CONF_FIXED_FEED_IN_PRICE,
                default=d.get(CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE),
                description={"suggested_value": d.get(CONF_FIXED_FEED_IN_PRICE)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0)),
            vol.Optional(
                CONF_ZERO_GRID_ENABLED,
                default=d.get(CONF_ZERO_GRID_ENABLED, DEFAULT_ZERO_GRID_ENABLED),
                description={"suggested_value": d.get(CONF_ZERO_GRID_ENABLED)},
            ): bool,
            vol.Optional(
                CONF_ZERO_GRID_RESPONSE_TIME_S,
                default=d.get(
                    CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S
                ),
                description={"suggested_value": d.get(CONF_ZERO_GRID_RESPONSE_TIME_S)},
            ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=300.0)),
            vol.Optional(
                CONF_MAX_GRID_POWER_KW,
                default=d.get(CONF_MAX_GRID_POWER_KW, DEFAULT_MAX_GRID_POWER_KW),
                description={"suggested_value": d.get(CONF_MAX_GRID_POWER_KW)},
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1000.0)),
        }
    )

    return vol.Schema(
        {
            vol.Required("sensors"): section(sensors_schema, {"collapsed": False}),
            vol.Optional("optional_sensors"): section(
                optional_sensors_schema, {"collapsed": True}
            ),
            vol.Optional("advanced"): section(advanced_schema, {"collapsed": True}),
        }
    )


def _extract_main_data(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested section data into a flat config dict.

    Battery hardware specs and SoC/power sensors are now in battery subentries,
    not in the main config.
    """
    sensors = user_input.get("sensors", {})
    opt = user_input.get("optional_sensors", {})
    adv = user_input.get("advanced", {})

    def _g(sect: dict[str, Any], key: str, default: Any = None) -> Any:
        """Get from section dict, falling back to top-level (flat layout)."""
        return sect.get(key, user_input.get(key, default))

    return {
        # Required sensors
        CONF_PRICE_SENSOR: _g(sensors, CONF_PRICE_SENSOR),
        # Optional sensors
        CONF_FEED_IN_PRICE_SENSOR: _g(opt, CONF_FEED_IN_PRICE_SENSOR),
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
        CONF_FIXED_FEED_IN_PRICE: float(
            _g(adv, CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE)
        ),
        CONF_ZERO_GRID_ENABLED: bool(
            _g(adv, CONF_ZERO_GRID_ENABLED, DEFAULT_ZERO_GRID_ENABLED)
        ),
        CONF_ZERO_GRID_RESPONSE_TIME_S: float(
            _g(adv, CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S)
        ),
        CONF_MAX_GRID_POWER_KW: float(
            _g(adv, CONF_MAX_GRID_POWER_KW, DEFAULT_MAX_GRID_POWER_KW)
        ),
    }


class BatteryControllerBatterySubentryFlow(config_entries.ConfigSubentryFlow):
    """Flow for adding or editing a battery subentry."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a new battery."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = _validate_battery_subentry(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_battery_input"
            else:
                return self.async_create_entry(
                    title=_battery_subentry_title(data),
                    data=data,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_build_battery_subentry_schema(),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing battery."""
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        current_data = dict(subentry.data)

        if user_input is not None:
            try:
                data = _validate_battery_subentry(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_battery_input"
            else:
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=_battery_subentry_title(data),
                    data=data,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_battery_subentry_schema(current_data),
            errors=errors,
        )


class BatteryControllerPVSubentryFlow(config_entries.ConfigSubentryFlow):
    """Flow for adding or editing a PV array subentry."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a new PV array."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = _validate_pv_subentry(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_pv_input"
            else:
                return self.async_create_entry(
                    title=_pv_subentry_title(data),
                    data=data,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_build_pv_subentry_schema(),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing PV array."""
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        current_data = dict(subentry.data)

        if user_input is not None:
            try:
                data = _validate_pv_subentry(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_pv_input"
            else:
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=_pv_subentry_title(data),
                    data=data,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_pv_subentry_schema(current_data),
            errors=errors,
        )


def _validate_pv_subentry(user_input: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise PV subentry user input."""
    schema = _build_pv_subentry_schema()
    validated = schema(user_input)
    result: dict[str, Any] = {}
    if name := validated.get(CONF_NAME, "").strip():
        result[CONF_NAME] = name
    result.update(
        {
            "peak_power_kwp": float(validated["peak_power_kwp"]),
            "orientation": float(validated["orientation"]),
            "tilt": float(validated["tilt"]),
            "efficiency_factor": float(validated.get("efficiency_factor", 0.85)),
            "dc_coupled": bool(validated.get("dc_coupled", False)),
        }
    )
    if validated.get(CONF_PV_FORECAST_SENSORS):
        result[CONF_PV_FORECAST_SENSORS] = list(validated[CONF_PV_FORECAST_SENSORS])
    return result


def _pv_subentry_title(data: dict[str, Any]) -> str:
    """Generate a display title for a PV subentry."""
    if name := str(data.get(CONF_NAME, "")).strip():
        return name
    kwp = data["peak_power_kwp"]
    coupling = "DC" if data["dc_coupled"] else "AC"
    return f"{kwp} kWp {coupling}"


async def _test_api_connection(hass: HomeAssistant) -> str | None:
    """Test reachability of open-meteo.com. Returns an error key or None on success."""
    session = async_get_clientsession(hass)
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(
        {
            "latitude": hass.config.latitude,
            "longitude": hass.config.longitude,
            "hourly": "shortwave_radiation",
            "forecast_days": "1",
        }
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return "cannot_connect"
    except (aiohttp.ClientError, TimeoutError):
        return "cannot_connect"
    return None


# Battery-specific keys that lived in the main entry data before v4 moved
# batteries into subentries.  Used by the v<4 migration step below.
_LEGACY_MAIN_BATTERY_KEYS = (
    CONF_CAPACITY_KWH,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_MIN_SOC_PERCENT,
    CONF_MAX_SOC_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_PV_DC_EFFICIENCY,
)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: config_entries.ConfigEntry
) -> bool:
    """Migrate old entry versions to current schema."""
    import math as _math
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentry

    if config_entry.version > 5:
        # Downgrade from a future version is not supported.
        return False

    if config_entry.version < 4:
        # v1-v3 → v4: battery specs lived in the main entry data; move them
        # into a battery subentry so the v4 subentry layout applies.
        data = dict(config_entry.data)
        battery_data = {
            key: data.pop(key) for key in _LEGACY_MAIN_BATTERY_KEYS if key in data
        }
        has_battery_subentry = any(
            sub.subentry_type == BATTERY_SUBENTRY_TYPE
            for sub in config_entry.subentries.values()
        )
        if battery_data and not has_battery_subentry:
            hass.config_entries.async_add_subentry(
                config_entry,
                ConfigSubentry(
                    subentry_type=BATTERY_SUBENTRY_TYPE,
                    title=_battery_subentry_title(battery_data),
                    data=MappingProxyType(battery_data),
                    unique_id=None,
                ),
            )
        hass.config_entries.async_update_entry(config_entry, data=data, version=4)

    if config_entry.version == 4:
        # v4 → v5: replace round_trip_efficiency scalar with per-direction curve strings
        for subentry in config_entry.subentries.values():
            data = dict(subentry.data)
            if (
                CONF_ROUND_TRIP_EFFICIENCY in data
                and CONF_CHARGE_EFFICIENCY_CURVE not in data
            ):
                rte = float(data.pop(CONF_ROUND_TRIP_EFFICIENCY))
                sqrt_rte_str = f"{_math.sqrt(rte):.6f}"
                data[CONF_CHARGE_EFFICIENCY_CURVE] = sqrt_rte_str
                data[CONF_DISCHARGE_EFFICIENCY_CURVE] = sqrt_rte_str
                hass.config_entries.async_update_subentry(
                    config_entry,
                    subentry,
                    data=data,
                )

        hass.config_entries.async_update_entry(
            config_entry,
            version=5,
        )

    return True


class BatteryControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Battery Controller."""

    VERSION = 5

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return supported subentry types."""
        return {
            BATTERY_SUBENTRY_TYPE: BatteryControllerBatterySubentryFlow,
            PV_SUBENTRY_TYPE: BatteryControllerPVSubentryFlow,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            data = _extract_main_data(user_input)
            if not data.get(CONF_PRICE_SENSOR):
                errors["base"] = "missing_required"
            else:
                error = await _test_api_connection(self.hass)
                if error:
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title="Battery Controller", data=data
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_main_schema(),
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
        """Handle reconfiguration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _extract_main_data(user_input)
            if not data.get(CONF_PRICE_SENSOR):
                errors["base"] = "missing_required"
            else:
                # Preserve number entity values managed outside the config flow
                for key in (
                    CONF_DEGRADATION_COST_PER_CYCLE,
                    CONF_MIN_PRICE_SPREAD,
                    CONF_ZERO_GRID_DEADBAND_W,
                ):
                    if key in self.config_entry.options:
                        data.setdefault(key, self.config_entry.options[key])
                return self.async_create_entry(title="", data=data)

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
