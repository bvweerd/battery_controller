"""Constants for the Battery Controller integration."""

from __future__ import annotations

from homeassistant.const import Platform

# Domain of the integration
DOMAIN = "battery_controller"

# Supported platforms for this integration
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
]

# Control modes
MODE_ZERO_GRID = "zero_grid"
MODE_FOLLOW_SCHEDULE = "follow_schedule"
MODE_HYBRID = "hybrid"
# Hybrid+ behaves like hybrid, but consults the price forecast (via the DP
# shadow price) before storing PV surplus: when the battery can be filled more
# cheaply later (e.g. a midday PV peak at low prices), the surplus is exported
# at the current feed-in price instead of charged.
MODE_HYBRID_PLUS = "hybrid_plus"
MODE_MANUAL = "manual"

CONTROL_MODES = [
    MODE_ZERO_GRID,
    MODE_FOLLOW_SCHEDULE,
    MODE_HYBRID,
    MODE_HYBRID_PLUS,
    MODE_MANUAL,
]

# Battery action modes
ACTION_IDLE = "idle"
ACTION_CHARGING = "charging"
ACTION_DISCHARGING = "discharging"

# Configuration keys - General
CONF_NAME = "name"

# Configuration keys - Battery specifications
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_USABLE_CAPACITY_KWH = "usable_capacity_kwh"
CONF_MAX_CHARGE_POWER_KW = "max_charge_power_kw"
CONF_MAX_DISCHARGE_POWER_KW = "max_discharge_power_kw"
CONF_ROUND_TRIP_EFFICIENCY = "round_trip_efficiency"
CONF_CHARGE_EFFICIENCY_CURVE = "charge_efficiency_curve"
CONF_DISCHARGE_EFFICIENCY_CURVE = "discharge_efficiency_curve"
CONF_MIN_SOC_PERCENT = "min_soc_percent"
CONF_MAX_SOC_PERCENT = "max_soc_percent"

# Configuration keys - SoC-dependent power derating
# Some batteries (e.g. Marstek Venus A) reduce charge/discharge power near SoC extremes.
# Above high_soc_charge_threshold the BMS caps charge power to high_soc_max_charge_kw.
# Below low_soc_discharge_threshold the BMS caps discharge power to low_soc_max_discharge_kw.
# Defaults of 100 / 0 % effectively disable derating (thresholds are never reached).
CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT = "high_soc_charge_threshold_pct"
CONF_HIGH_SOC_MAX_CHARGE_KW = "high_soc_max_charge_kw"
CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT = "low_soc_discharge_threshold_pct"
CONF_LOW_SOC_MAX_DISCHARGE_KW = "low_soc_max_discharge_kw"

# Subentry types
PV_SUBENTRY_TYPE = "pv_array"
BATTERY_SUBENTRY_TYPE = "battery"

# Configuration keys - PV array (subentry): external PV forecast sensors.
# When set, the PV forecast is read from these sensors (e.g. the Solcast
# integration's "Forecast Today"/"Forecast Tomorrow" sensors) at their
# native resolution instead of the internal radiation-based model. The
# internal model remains the fallback for steps not covered by sensor data.
CONF_PV_FORECAST_SENSORS = "pv_forecast_sensors"

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
# kWh total-energy counters per battery subentry. Grid charging passes the grid
# meter, so without these it is reconstructed as household load. Where one
# inverter reports a single counter for several packs, set it on one of them:
# the totals stay correct, only per-pack use of the figure is unavailable.
CONF_BATTERY_ENERGY_CHARGED_SENSOR = "battery_energy_charged_sensor"
CONF_BATTERY_ENERGY_DISCHARGED_SENSOR = "battery_energy_discharged_sensor"
# Household load is derived, not configured: gross = import - export + PV
# + discharge - charge. Each field below is one physical measurement.
CONF_GRID_IMPORT_SENSORS = "grid_import_sensors"
CONF_GRID_EXPORT_SENSORS = "grid_export_sensors"
# Optional override: a meter between inverter and house measures gross load
# directly. More accurate than summing five meters, and the only workable
# source when the component set is incomplete (e.g. DC-coupled PV with no DC
# counter). When set, the reconstruction is skipped entirely.
CONF_GROSS_LOAD_SENSORS = "gross_load_sensors"

# Legacy keys, retained for the v5 -> v6 migration only.
CONF_ELECTRICITY_CONSUMPTION_SENSORS = "electricity_consumption_sensors"
CONF_ELECTRICITY_PRODUCTION_SENSORS = "electricity_production_sensors"
# kWh total-energy sensors from PV inverters (used to reconstruct gross consumption)
CONF_PV_PRODUCTION_SENSORS = "pv_production_sensors"
# Configuration keys - Battery subentry
CONF_POWER_CONSUMPTION_SENSORS = "power_consumption_sensors"
CONF_POWER_PRODUCTION_SENSORS = "power_production_sensors"

# Configuration keys - Advanced settings
CONF_TIME_STEP_MINUTES = "time_step_minutes"
CONF_OPTIMIZATION_INTERVAL_MINUTES = "optimization_interval_minutes"
CONF_DEGRADATION_COST_PER_CYCLE = "degradation_cost_per_cycle"
CONF_MIN_PRICE_SPREAD = "min_price_spread"

# Configuration keys - Manual control
CONF_MANUAL_POWER_SETPOINT_W = "manual_power_setpoint_w"

# Configuration keys - Zero grid control
CONF_ZERO_GRID_ENABLED = "zero_grid_enabled"
CONF_ZERO_GRID_DEADBAND_W = "zero_grid_deadband_w"
CONF_ZERO_GRID_RESPONSE_TIME_S = "zero_grid_response_time_s"

# Configuration keys - Control mode (persisted)
CONF_CONTROL_MODE = "control_mode"

# Configuration keys - Fixed prices (fallback)
CONF_FIXED_FEED_IN_PRICE = "fixed_feed_in_price"

# Default values - Battery specifications
DEFAULT_CAPACITY_KWH = 10.0
DEFAULT_MAX_CHARGE_POWER_KW = 5.0
DEFAULT_MAX_DISCHARGE_POWER_KW = 5.0
DEFAULT_ROUND_TRIP_EFFICIENCY = 0.90
# Per-direction default = sqrt(DEFAULT_ROUND_TRIP_EFFICIENCY) so the default
# round-trip efficiency stays 0.90 (0.9487 × 0.9487 ≈ 0.90), matching both the
# pre-curve scalar default and what migrated entries get.
DEFAULT_CHARGE_EFFICIENCY_CURVE = "0.9487"
DEFAULT_DISCHARGE_EFFICIENCY_CURVE = "0.9487"
DEFAULT_MIN_SOC_PERCENT = 10.0
DEFAULT_MAX_SOC_PERCENT = 90.0

# Default values - SoC-dependent power derating (disabled by default)
DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT = 100.0  # % — above this SoC, charge is derated
DEFAULT_HIGH_SOC_MAX_CHARGE_KW = 0.0  # kW — 0 means no derating
DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT = (
    0.0  # % — below this SoC, discharge is derated
)
DEFAULT_LOW_SOC_MAX_DISCHARGE_KW = 0.0  # kW — 0 means no derating

# Default values - PV array geometry
DEFAULT_PV_ORIENTATION_DEG = 180.0  # degrees, South-facing
DEFAULT_PV_TILT_DEG = 35.0  # degrees, typical mid-latitude tilt

# Default values - DC-coupled PV
DEFAULT_PV_DC_COUPLED = False
DEFAULT_PV_DC_PEAK_POWER_KWP = 0.0
# DC-coupled efficiency is higher: no DC->AC->DC round trip
# Typically ~97% for MPPT + charge controller vs ~85% for AC-coupled
DEFAULT_PV_DC_EFFICIENCY = 0.97

# Default values - Advanced settings
DEFAULT_TIME_STEP_MINUTES = 15
DEFAULT_OPTIMIZATION_INTERVAL_MINUTES = 15
# EUR per full charge+discharge cycle, derived rather than guessed:
#
#   replacement cost      250 EUR/kWh   (installed LFP home battery)
#   cycle life           6000 cycles    (at 80 % depth of discharge)
#   usable capacity          8 kWh      (the 10 kWh default at 10-90 % SoC)
#
#   per kWh throughput = 250 / 6000 / (2 x 0.8) = 0.026 EUR/kWh
#   per cycle          = 0.026 x 2 x 8          = 0.42 EUR/cycle
#
# The coordinator converts back with degradation / (2 x usable_kwh), so the
# per-kWh figure the optimizer sees is independent of battery size; only the
# per-cycle presentation scales with the default capacity.
#
# The previous default of 0.04 EUR/cycle worked out at 0.0025 EUR/kWh, an order
# of magnitude below any plausible replacement cost, which left the DP with
# almost no reason to avoid cycling. Users who set their own value keep it;
# this only changes the starting point.
DEFAULT_DEGRADATION_COST_PER_CYCLE = 0.42
DEFAULT_MIN_PRICE_SPREAD = 0.05  # EUR/kWh minimum spread for arbitrage

# Default values - Manual control
DEFAULT_MANUAL_POWER_SETPOINT_W = 0.0

# Default values - Zero grid control
DEFAULT_ZERO_GRID_ENABLED = True
DEFAULT_ZERO_GRID_DEADBAND_W = 50.0
DEFAULT_ZERO_GRID_RESPONSE_TIME_S = 10.0

# Default values - Fixed prices
DEFAULT_FIXED_FEED_IN_PRICE = 0.04  # EUR/kWh (post-salderingsregeling NL, 2025+)

# Default values - Control mode
DEFAULT_CONTROL_MODE = "hybrid"

# Base resolution of the PV/consumption forecast pipeline.
# Weather input (open-meteo) is hourly, but forecasts are emitted at
# 15-minute steps aligned to quarter-hour boundaries so they map 1:1 onto
# 15-minute price intervals without the up-to-45-minute misalignment that
# hourly series had at hour boundaries. Hourly inputs are expanded by
# repetition (mean-preserving); solar geometry is evaluated per step, so
# dawn/dusk ramps gain sub-hourly shape even from hourly radiation data.
FORECAST_INTERVAL_MINUTES = 15

# DP resolution constants
# 10 Wh SoC states: fine enough that the post-discharge SoC maps accurately
# (coarse states systematically undervalue concentrating discharge at the
# peak-price hour), while the per-action sub-resolution guard in the DP
# (new_soc_idx == s_idx -> skip) prevents "free-looking" micro-actions that
# would otherwise oscillate. See the resolution discussion in optimizer.py.
SOC_RESOLUTION_WH = 10.0  # Minimum SoC state size in Wh
POWER_STEP_W = 100  # Minimum practical power action granularity in W

# Upper bound on the number of discrete SoC states in the DP.
# At a fixed 10 Wh resolution the state count grows linearly with usable
# capacity, and DP cost grows with it: a 10 kWh battery needs ~800 states,
# a 55 kWh battery ~4500, making the solve several times slower purely
# because the battery is larger. Above this budget the resolution is
# coarsened so the state count stays bounded.
# 1000 states means the cap only engages above 10 kWh of usable range —
# typical home batteries keep the exact 10 Wh resolution and are bit-for-bit
# unaffected. At the cap the resolution is 0.1% of usable capacity (e.g.
# 45 Wh on a 45 kWh range), far below SoC sensor accuracy (~1%).
MAX_SOC_STATES = 1000

# DC-to-AC conversion efficiency (excess DC PV through inverter to AC bus)
DC_TO_AC_INVERTER_EFFICIENCY = 0.96

# Minimum cycle energy (P5.1): charge/discharge segments smaller than this are
# suppressed as micro-cycles with disproportionate degradation cost.
MIN_CYCLE_KWH = 0.2  # kWh

# Grid capacity cap: maximum import/export power at grid connection (0 = unlimited)
CONF_MAX_GRID_POWER_KW = "max_grid_power_kw"
DEFAULT_MAX_GRID_POWER_KW = 0.0  # kW, 0 = no cap

# Algorithm thresholds
MIN_PV_SURPLUS_KW = (
    0.05  # kW (50 W) — minimum surplus to apply PV opportunity-cost pricing
)
POWER_IDLE_THRESHOLD_KW = (
    0.001  # kW (1 W) — power below this is treated as idle in the schedule
)
PRICE_CHANGE_REOPTIMIZE_THRESHOLD = (
    0.10  # fractional — re-run optimizer on >=10% price change
)
PRICE_CHANGE_REOPTIMIZE_ABS_EUR = (
    0.01  # EUR/kWh — absolute change threshold when the previous price is 0
)
STALE_SENSOR_MULTIPLIER = (
    2.0  # x response_time_s — age limit before sensor is treated as stale
)
WEATHER_STALE_AFTER_MINUTES = 120.0  # minutes — weather data older than this is treated as stale (4 missed updates)
# Plausibility ceiling for a learned hourly consumption sample, in kW.
# An hourly statistics "change" equals the average power over that hour, so a
# household hour above this is a meter artefact (a total_increasing sensor that
# jumped or was replaced, a unit change, or a spurious reading), not real load:
# even a 3x80 A connection at full load for a whole hour stays near 55 kW.
# Such a sample is unbounded in magnitude — observed values reach 10^8 kW — and
# a single one poisons its (hour, weekday) bucket and wrecks the DP cost.
MAX_PLAUSIBLE_CONSUMPTION_KW = 50.0

SOC_UNCERTAINTY_RESERVE_FRACTION = (
    0.10  # max fraction of capacity reserved for solar forecast uncertainty
)

# Real-time control thresholds
BATTERY_MODE_THRESHOLD_W = 50.0  # W — battery power above/below this sets mode
SETPOINT_STABLE_THRESHOLD_KW = 0.010  # kW (10 W) — setpoint "stable" if within this
BATTERY_POWER_CHANGE_THRESHOLD_KW = 0.005  # kW (5 W) — minimum reportable power change
