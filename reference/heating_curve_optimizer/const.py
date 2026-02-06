"""Constants for the Heating Curve Optimizer integration."""

# Domain of the integration
DOMAIN = "heating_curve_optimizer"
DOMAIN_ABBREVIATION = "HCO"

# Supported platforms for this integration
PLATFORMS = ["sensor", "binary_sensor", "number"]

# Configuration keys
CONF_SOURCE_TYPE = "source_type"
CONF_SOURCES = "sources"
CONF_PRICE_SENSOR = "price_sensor"
CONF_CONSUMPTION_PRICE_SENSOR = "consumption_price_sensor"
CONF_PRODUCTION_PRICE_SENSOR = "production_price_sensor"
CONF_PRICE_SETTINGS = "price_settings"

# New configuration keys for the heating curve optimizer
CONF_AREA_M2 = "area_m2"
CONF_ENERGY_LABEL = "energy_label"
# Planning window and time base configuration
CONF_PLANNING_WINDOW = "planning_window"
CONF_TIME_BASE = "time_base"
# Default values
DEFAULT_PLANNING_WINDOW = 6  # hours
DEFAULT_TIME_BASE = 60  # minutes per step
# Glass related configuration
CONF_GLASS_EAST_M2 = "glass_east_m2"
CONF_GLASS_WEST_M2 = "glass_west_m2"
CONF_GLASS_SOUTH_M2 = "glass_south_m2"
CONF_GLASS_U_VALUE = "glass_u_value"
CONF_POWER_CONSUMPTION = "power_consumption"
CONF_INDOOR_TEMPERATURE_SENSOR = "indoor_temperature_sensor"
CONF_SUPPLY_TEMPERATURE_SENSOR = "supply_temperature_sensor"
CONF_K_FACTOR = "k_factor"
CONF_BASE_COP = "base_cop"
CONF_COP_COMPENSATION_FACTOR = "cop_compensation_factor"
CONF_OUTDOOR_TEMP_COEFFICIENT = "outdoor_temp_coefficient"
CONF_HEAT_CURVE_MIN_OUTDOOR = "heat_curve_min_outdoor"
CONF_HEAT_CURVE_MAX_OUTDOOR = "heat_curve_max_outdoor"
CONF_HEATING_CURVE_OFFSET = "heating_curve_offset"
CONF_HEAT_CURVE_MIN = "heat_curve_min"
CONF_HEAT_CURVE_MAX = "heat_curve_max"
CONF_VENTILATION_TYPE = "ventilation_type"
CONF_CEILING_HEIGHT = "ceiling_height"
CONF_MAX_BUFFER_DEBT = "max_buffer_debt"
CONF_TARGET_INDOOR_TEMP = "target_indoor_temp"
CONF_INDOOR_TEMP_HYSTERESIS = "indoor_temp_hysteresis"  # Legacy, kept for compatibility
CONF_INDOOR_TEMP_HYSTERESIS_LOWER = "indoor_temp_hysteresis_lower"  # Below target
CONF_INDOOR_TEMP_HYSTERESIS_UPPER = "indoor_temp_hysteresis_upper"  # Above target
CONF_OFFSET_DELTA_T = "offset_delta_t"  # Minutes per 1°C offset change

# Default values for heating curve settings
DEFAULT_HEATING_CURVE_OFFSET = 0.0
DEFAULT_HEAT_CURVE_MIN = 20.0
DEFAULT_HEAT_CURVE_MAX = 45.0
DEFAULT_MAX_BUFFER_DEBT = 5.0  # kWh - allows heat debt for cost optimization
DEFAULT_TARGET_INDOOR_TEMP = 20.0  # °C - desired room temperature
DEFAULT_INDOOR_TEMP_HYSTERESIS = 0.5  # °C - legacy hysteresis (symmetric)
DEFAULT_INDOOR_TEMP_HYSTERESIS_LOWER = (
    0.3  # °C - hysteresis below target (heat pump ON)
)
DEFAULT_INDOOR_TEMP_HYSTERESIS_UPPER = (
    0.5  # °C - hysteresis above target (heat pump OFF)
)
DEFAULT_OFFSET_DELTA_T = 10  # Minutes per 1°C offset change (10 = fast, 60 = slow)

# Default ventilation and building settings
DEFAULT_VENTILATION_TYPE = "natural_standard"
DEFAULT_CEILING_HEIGHT = 2.5  # meters

# Ventilation types and their effective Air Changes per Hour (ACH)
# ACH represents the volume of air exchanged per hour
# For heat recovery systems, effective ACH accounts for recovered heat
VENTILATION_TYPES = {
    "none": {"ach": 0.2, "name_nl": "Geen/minimaal", "name_en": "None/minimal"},
    "natural_low": {
        "ach": 0.5,
        "name_nl": "Natuurlijke ventilatie (basis)",
        "name_en": "Natural ventilation (basic)",
    },
    "natural_standard": {
        "ach": 1.0,
        "name_nl": "Natuurlijke ventilatie (standaard)",
        "name_en": "Natural ventilation (standard)",
    },
    "natural_leaky": {
        "ach": 1.5,
        "name_nl": "Natuurlijke ventilatie (lekkage)",
        "name_en": "Natural ventilation (leaky)",
    },
    "mechanical_exhaust": {
        "ach": 0.7,
        "name_nl": "Mechanische afzuiging",
        "name_en": "Mechanical exhaust",
    },
    "balanced_no_recovery": {
        "ach": 0.8,
        "name_nl": "Gebalanceerd (zonder WTW)",
        "name_en": "Balanced (no heat recovery)",
    },
    "heat_recovery_50": {
        "ach": 0.4,
        "name_nl": "WTW 50% rendement",
        "name_en": "Heat recovery 50% efficiency",
    },
    "heat_recovery_70": {
        "ach": 0.24,
        "name_nl": "WTW 70% rendement",
        "name_en": "Heat recovery 70% efficiency",
    },
    "heat_recovery_90": {
        "ach": 0.08,
        "name_nl": "WTW 90% rendement",
        "name_en": "Heat recovery 90% efficiency",
    },
}

# PV panel configuration
CONF_PV_EAST_WP = "pv_east_wp"
CONF_PV_SOUTH_WP = "pv_south_wp"
CONF_PV_WEST_WP = "pv_west_wp"
CONF_PV_TILT = "pv_tilt"

# Default PV tilt angle (degrees) - typical for Netherlands
DEFAULT_PV_TILT = 35

# Allowed energy labels
ENERGY_LABELS = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F", "G"]

# Mapping energielabel -> primary energy consumption (kWh/m²/year)
# Based on NTA 8800 standard (since Jan 2021)
# Using midpoint values for each label range
ENERGY_LABEL_CONSUMPTION = {
    "A+++": 50,  # Very low energy (passive house level)
    "A++": 75,  # Very low energy
    "A+": 90,  # Very low energy
    "A": 132,  # 105-160 kWh/m²/year (midpoint)
    "B": 175,  # 160-190 kWh/m²/year (midpoint)
    "C": 220,  # 190-250 kWh/m²/year (midpoint)
    "D": 275,  # 250-300 kWh/m²/year (midpoint)
    "E": 340,  # 300-380 kWh/m²/year (midpoint)
    "F": 420,  # 380-460 kWh/m²/year (midpoint)
    "G": 500,  # >460 kWh/m²/year (estimate)
}

# Heating fraction of total primary energy by label
# Better insulated homes have relatively more DHW energy use
HEATING_FRACTION_MAP = {
    "A+++": 0.40,  # Passive house: excellent insulation, DHW is major portion
    "A++": 0.42,
    "A+": 0.45,
    "A": 0.50,  # Good insulation
    "B": 0.55,
    "C": 0.60,
    "D": 0.65,
    "E": 0.68,
    "F": 0.70,
    "G": 0.72,  # Poor insulation: heating dominates
}

# Heating degree-days for Netherlands (base 18°C)
# Average value for Dutch climate
HEATING_DEGREE_DAYS_NL = 2900

# Binnentemperatuur in °C voor warmteverliesberekening
INDOOR_TEMPERATURE = 21.0

# Legacy U-value map (deprecated, kept for backward compatibility)
# DO NOT USE - these values incorrectly treat energy labels as U-values
U_VALUE_MAP = {
    "A+++": 0.2,
    "A++": 0.3,
    "A+": 0.4,
    "A": 0.6,
    "B": 0.8,
    "C": 1.0,
    "D": 1.2,
    "E": 1.4,
    "F": 1.6,
    "G": 1.8,
}


def calculate_ventilation_htc(
    area_m2: float,
    ventilation_type: str = DEFAULT_VENTILATION_TYPE,
    ceiling_height: float = DEFAULT_CEILING_HEIGHT,
) -> float:
    """
    Calculate ventilation Heat Transfer Coefficient (H_V).

    This calculates the heat loss through ventilation and air infiltration
    based on the ventilation system type and building volume.

    Formula:
    - Volume (m³) = Area × Ceiling height
    - H_V (W/K) = ρ × c × Volume × ACH / 3.6
      where ρ = 1.2 kg/m³ (air density)
            c = 1.005 kJ/(kg·K) (specific heat of air)
            ACH = Air Changes per Hour from ventilation type

    Args:
        area_m2: Floor area in m²
        ventilation_type: Ventilation system type key from VENTILATION_TYPES
        ceiling_height: Ceiling height in meters (default: 2.5m)

    Returns:
        Ventilation Heat Transfer Coefficient in W/K
    """
    # Get ACH for this ventilation type
    vent_data = VENTILATION_TYPES.get(ventilation_type)
    if vent_data is None:
        # Fallback to standard natural ventilation
        vent_data = VENTILATION_TYPES[DEFAULT_VENTILATION_TYPE]
    ach = vent_data["ach"]

    # Calculate building volume
    volume = area_m2 * ceiling_height

    # Calculate ventilation HTC
    # ρ (air density) = 1.2 kg/m³
    # c (specific heat) = 1.005 kJ/(kg·K)
    # Division by 3.6 converts kJ to W and hours to seconds
    rho = 1.2  # kg/m³
    c = 1.005  # kJ/(kg·K)
    h_v = rho * c * volume * ach / 3.6

    return h_v


def calculate_htc_from_energy_label(
    energy_label: str,
    area_m2: float,
    heating_degree_days: float = HEATING_DEGREE_DAYS_NL,
    ventilation_type: str = DEFAULT_VENTILATION_TYPE,
    ceiling_height: float = DEFAULT_CEILING_HEIGHT,
) -> float:
    """
    Calculate total Heat Transfer Coefficient (HTC) from energy label.

    This converts the energy label (primary energy in kWh/m²/year) to the
    total building heat loss coefficient (W/K) using heating degree-days.

    The total HTC includes both transmission losses (through building fabric)
    and ventilation losses (air exchange), following ISO 13789:
        HTC_total = H_T + H_V

    Formula:
    - Annual heating energy (kWh) = Label energy × Area × Heating fraction
    - H_T (W/K) = Annual heating energy × 1000 / (HDD × 24)
    - H_V (W/K) = Ventilation heat loss coefficient (see calculate_ventilation_htc)
    - HTC (W/K) = H_T + H_V

    Args:
        energy_label: Energy label (A+++, A++, A+, A, B, C, D, E, F, G)
        area_m2: Floor area in m²
        heating_degree_days: Heating degree-days for the climate (default: NL)
        ventilation_type: Ventilation system type (default: natural_standard)
        ceiling_height: Ceiling height in meters (default: 2.5m)

    Returns:
        Total Heat Transfer Coefficient in W/K (transmission + ventilation)
    """
    # Get energy consumption and heating fraction for this label
    energy_per_m2 = ENERGY_LABEL_CONSUMPTION.get(energy_label.upper(), 220)
    heating_fraction = HEATING_FRACTION_MAP.get(energy_label.upper(), 0.60)

    # Calculate annual heating energy (kWh/year)
    annual_heating_energy = energy_per_m2 * area_m2 * heating_fraction

    # Convert to transmission HTC using degree-days
    # H_T (W/K) = kWh/year × 1000 W/kW / (degree-days × 24 hours/day)
    if heating_degree_days <= 0:
        heating_degree_days = HEATING_DEGREE_DAYS_NL

    h_t = annual_heating_energy * 1000.0 / (heating_degree_days * 24.0)

    # Calculate ventilation HTC
    h_v = calculate_ventilation_htc(area_m2, ventilation_type, ceiling_height)

    # Total HTC = transmission + ventilation (ISO 13789)
    htc_total = h_t + h_v

    return htc_total


# Default COP at a supply temperature of 35 °C
DEFAULT_COP_AT_35 = 4.2

# Default decline in COP per °C supply temperature increase
DEFAULT_K_FACTOR = 0.11

# Default increase in COP per °C outdoor temperature rise
DEFAULT_OUTDOOR_TEMP_COEFFICIENT = 0.08

# Default COP compensation factor
DEFAULT_COP_COMPENSATION_FACTOR = 1.0

# Thermal storage efficiency: fraction of heat demand that goes to/from
# thermal mass storage per degree of temperature offset
# When offset is +1°C, building is overheated and stores thermal energy
# When offset is -1°C, building uses stored thermal energy
# Value of 0.15 means 15% of current heat demand is stored/released per °C offset
# This represents the thermal inertia of building materials (concrete, brick, etc.)
DEFAULT_THERMAL_STORAGE_EFFICIENCY = 0.15

# Possible source types
SOURCE_TYPE_CONSUMPTION = "Electricity consumption"
SOURCE_TYPE_PRODUCTION = "Electricity production"

# allowed values for source_type
SOURCE_TYPES = [
    SOURCE_TYPE_CONSUMPTION,
    SOURCE_TYPE_PRODUCTION,
]

CONF_CONFIGS = "configurations"
