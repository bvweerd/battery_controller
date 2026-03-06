"""Constants for the Battery Controller integration."""

from __future__ import annotations

# Domain of the integration
DOMAIN = "battery_controller"
DOMAIN_ABBREVIATION = "BC"

# Supported platforms for this integration
PLATFORMS = ["sensor", "number", "select", "switch", "binary_sensor"]

# Control modes
MODE_ZERO_GRID = "zero_grid"
MODE_FOLLOW_SCHEDULE = "follow_schedule"
MODE_HYBRID = "hybrid"
MODE_MANUAL = "manual"

CONTROL_MODES = [MODE_ZERO_GRID, MODE_FOLLOW_SCHEDULE, MODE_HYBRID, MODE_MANUAL]

# Battery action modes
ACTION_IDLE = "idle"
ACTION_CHARGING = "charging"
ACTION_DISCHARGING = "discharging"

# Configuration keys - Battery specifications
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_USABLE_CAPACITY_KWH = "usable_capacity_kwh"
CONF_MAX_CHARGE_POWER_KW = "max_charge_power_kw"
CONF_MAX_DISCHARGE_POWER_KW = "max_discharge_power_kw"
CONF_ROUND_TRIP_EFFICIENCY = "round_trip_efficiency"
CONF_MIN_SOC_PERCENT = "min_soc_percent"
CONF_MAX_SOC_PERCENT = "max_soc_percent"

# Configuration keys - PV system (array 1)
CONF_PV_PEAK_POWER_KWP = "pv_peak_power_kwp"
CONF_PV_ORIENTATION = "pv_orientation"
CONF_PV_TILT = "pv_tilt"
CONF_PV_EFFICIENCY_FACTOR = "pv_efficiency_factor"

# Configuration keys - Extra PV arrays (dynamic list of dicts)
# Each dict has: peak_power_kwp, orientation, tilt
CONF_PV_EXTRA_ARRAYS = "pv_extra_arrays"

# Configuration keys - DC-coupled PV (PV direct on battery inverter)
# When PV is DC-coupled to the battery, PV power goes directly to the
# battery without AC conversion. This is common with hybrid inverters
# (SolarEdge, Huawei, GoodWe, Victron, etc.)
CONF_PV_DC_COUPLED = "pv_dc_coupled"
CONF_PV_DC_PEAK_POWER_KWP = "pv_dc_peak_power_kwp"
CONF_PV_DC_EFFICIENCY = "pv_dc_efficiency"

# Configuration keys - Sensors
CONF_PRICE_SENSOR = "price_sensor"
CONF_FEED_IN_PRICE_SENSOR = "feed_in_price_sensor"
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_BATTERY_POWER_SENSOR = "battery_power_sensor"
CONF_ELECTRICITY_CONSUMPTION_SENSORS = "electricity_consumption_sensors"
CONF_ELECTRICITY_PRODUCTION_SENSORS = "electricity_production_sensors"
# kWh total-energy sensors from PV inverters (used to reconstruct gross consumption)
CONF_PV_PRODUCTION_SENSORS = "pv_production_sensors"
CONF_POWER_CONSUMPTION_SENSORS = "power_consumption_sensors"
CONF_POWER_PRODUCTION_SENSORS = "power_production_sensors"

# Configuration keys - Advanced settings
CONF_TIME_STEP_MINUTES = "time_step_minutes"
CONF_OPTIMIZATION_INTERVAL_MINUTES = "optimization_interval_minutes"
CONF_DEGRADATION_COST_PER_KWH = "degradation_cost_per_kwh"
CONF_MIN_PRICE_SPREAD = "min_price_spread"

# Configuration keys - Zero grid control
CONF_ZERO_GRID_ENABLED = "zero_grid_enabled"
CONF_ZERO_GRID_DEADBAND_W = "zero_grid_deadband_w"
CONF_ZERO_GRID_RESPONSE_TIME_S = "zero_grid_response_time_s"
CONF_ZERO_GRID_PRIORITY = "zero_grid_priority"

# Configuration keys - Control mode (persisted)
CONF_CONTROL_MODE = "control_mode"

# Configuration keys - Fixed prices (fallback)
CONF_FIXED_FEED_IN_PRICE = "fixed_feed_in_price"

# Default values - Battery specifications
DEFAULT_CAPACITY_KWH = 10.0
DEFAULT_MAX_CHARGE_POWER_KW = 5.0
DEFAULT_MAX_DISCHARGE_POWER_KW = 5.0
DEFAULT_ROUND_TRIP_EFFICIENCY = 0.90
DEFAULT_MIN_SOC_PERCENT = 10.0
DEFAULT_MAX_SOC_PERCENT = 90.0

# Default values - PV system (array 1)
DEFAULT_PV_PEAK_POWER_KWP = 0.0
DEFAULT_PV_ORIENTATION = 180  # South
DEFAULT_PV_TILT = 35  # Typical for Netherlands
DEFAULT_PV_EFFICIENCY_FACTOR = 0.85


# Default values - DC-coupled PV
DEFAULT_PV_DC_COUPLED = False
DEFAULT_PV_DC_PEAK_POWER_KWP = 0.0
# DC-coupled efficiency is higher: no DC->AC->DC round trip
# Typically ~97% for MPPT + charge controller vs ~85% for AC-coupled
DEFAULT_PV_DC_EFFICIENCY = 0.97

# Default values - Advanced settings
DEFAULT_TIME_STEP_MINUTES = 15
DEFAULT_OPTIMIZATION_INTERVAL_MINUTES = 15
DEFAULT_DEGRADATION_COST_PER_KWH = 0.03  # EUR/kWh throughput
DEFAULT_MIN_PRICE_SPREAD = 0.05  # EUR/kWh minimum spread for arbitrage

# Default values - Zero grid control
DEFAULT_ZERO_GRID_ENABLED = True
DEFAULT_ZERO_GRID_DEADBAND_W = 50.0
DEFAULT_ZERO_GRID_RESPONSE_TIME_S = 10.0
DEFAULT_ZERO_GRID_PRIORITY = "schedule"

# Default values - Fixed prices
DEFAULT_FIXED_FEED_IN_PRICE = 0.07  # EUR/kWh

# Default values - Control mode
DEFAULT_CONTROL_MODE = "hybrid"

# Battery degradation model constants
# Typical Li-ion battery: ~6000 cycles at 80% DoD
# Replacement cost: ~500 EUR/kWh
# Cost per cycle: 500 / 6000 = 0.083 EUR/kWh capacity
# Cost per kWh throughput: 0.083 / (2 * 0.8) = 0.052 EUR/kWh
BATTERY_LIFECYCLE_CYCLES = 6000
BATTERY_REPLACEMENT_COST_PER_KWH = 500  # EUR
BATTERY_DOD_FACTOR = 0.8

# SoC discretization for DP (in Wh steps)
SOC_RESOLUTION_WH = 25  # 25 Wh steps → aligns with 100W×0.25h per action

# Time constants
HOURS_PER_DAY = 24
MINUTES_PER_HOUR = 60
SECONDS_PER_MINUTE = 60
