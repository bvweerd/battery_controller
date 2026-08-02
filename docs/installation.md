# Installation

## Prerequisites

- **Home Assistant 2025.1 or later**
- A dynamic electricity price sensor with forecast attributes (e.g. Nordpool, ENTSO-E,
  or the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator))
- Battery SoC sensor(s) from your inverter integration
- [HACS](https://hacs.xyz/) installed in your Home Assistant (recommended)

!!! tip "Check your sensors before you install"
    Two sensor problems account for most of the "it does not work" reports: a price
    sensor without forecast attributes, and an energy sensor with the wrong
    `state_class`. Both are quicker to verify now than to diagnose later — see the two
    sections below.

---

## Verifying your price sensor

The integration reads forecast data from the **forecast attributes** of your price
sensor. Before setting up, verify that your sensor exposes the required attributes.

Go to **Developer Tools → States**, find your price sensor (e.g.
`sensor.nordpool_kwh_nl_eur_3_10_21`) and check that the attributes contain a list of
future prices. The integration supports several common formats.

=== "Nordpool / ENTSO-E"

    Attributes contain `raw_today` and `raw_tomorrow`, each a list of objects with a
    `value` key:

    ```yaml
    raw_today:
      - start: "2026-03-16T00:00:00+01:00"
        end:   "2026-03-16T01:00:00+01:00"
        value: 0.1234
      - ...
    raw_tomorrow:
      - start: "2026-03-17T00:00:00+01:00"
        value: 0.2345
      - ...
    ```

=== "Generic forecast list"

    Attributes contain a `forecast` key with a list of objects:

    ```yaml
    forecast:
      - datetime: "2026-03-16T12:00:00+00:00"
        price: 0.1580
      - datetime: "2026-03-16T13:00:00+00:00"
        price: 0.2210
      - ...
    ```

=== "Dynamic Energy Contract Calculator"

    Exposes `prices_today` and `prices_tomorrow` as plain lists of floats (one per
    hour), or a combined `price_forecast` list.

=== "OMIE (Spain/Portugal)"

    The [OMIE integration](https://github.com/luuuis/hass_omie) exposes `today_hours`
    and `tomorrow_hours` as dicts mapping period-start times to prices in **€/MWh**:

    ```yaml
    today_hours:
      "2026-03-16T00:00:00+00:00": 96.9
      "2026-03-16T01:00:00+00:00": 85.2
      ...
    tomorrow_hours: null   # published around 13:30 CET
    ```

    Prices are converted to €/kWh automatically (based on the sensor's
    `unit_of_measurement`), provisional `null` entries are skipped, and the self-learning
    price model applies the same conversion to historical data. Quarter-hourly OMIE data
    is detected and used at its native 15-minute interval.

!!! failure "A price sensor with no forecast list will not work"
    If your sensor's state is the current price but its attributes contain no forecast
    list, the optimizer runs with a flat price forecast and cannot perform meaningful
    arbitrage. Use a different sensor, or add the
    [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator)
    on top of your existing sensor.

---

## Verifying your consumption sensors

The consumption forecast is **learned from long-term statistics** of the energy sensors
you configure under *Electricity consumption sensors*. These are cumulative **kWh**
sensors (DSMR-style), not power sensors — the integration reads the hourly `change` in
each sensor to reconstruct your consumption pattern.

For that to work, the sensor must produce **sum-type statistics**, which requires:

| Attribute | Required value |
|-----------|----------------|
| `state_class` | `total_increasing` (or `total`) |
| `device_class` | `energy` |
| `unit_of_measurement` | `kWh` |

!!! danger "A `state_class` of `measurement` is the most common mistake"
    It produces mean/min/max statistics instead of sum statistics, so the hourly `change`
    the learner needs does not exist. The integration then silently falls back to a
    built-in default pattern. This is easy to get wrong when you build the sensor
    yourself with a template or a Riemann-sum helper — and note that a *price* sensor is
    the opposite case, where `measurement` is correct.

**How to check:** go to **Developer Tools → Statistics** and search for your sensor. If
it does not appear in the list, or Home Assistant reports an issue for it, it is not
generating the statistics the learner needs.

**Until a pattern is learned**, the consumption forecast uses a built-in cold-start curve
for a typical household (≈3500 kWh/year, ~0.4 kW average, with a morning and evening
peak). If your home uses substantially more, the forecast will look far too low until
real statistics are available. The learner reads the **past 14 days** from the recorder,
so a correctly configured sensor that already has history populates the pattern on the
very first refresh — you do not have to wait for the integration itself to accumulate
data.

!!! warning "PV double counting"
    Only configure *Electricity production sensors* if your consumption sensor measures
    **net grid import** (i.e. it goes down when PV produces). If it already measures
    gross household load — as with a sensor between the inverter and the house — leave
    the production field empty. Filling both fields activates a correction that adds PV
    production back on top, which would inflate the learned pattern.

---

## Installing the integration

=== "Via HACS (recommended)"

    1. Navigate to **HACS → Integrations → Three dots → Custom repositories**.
    2. Add `https://github.com/bvweerd/battery_controller` as an **Integration**.
    3. Install the "Battery Controller" integration and restart Home Assistant.

=== "Manual"

    1. Copy `custom_components/battery_controller` to your `custom_components` directory.
    2. Restart Home Assistant.

---

## Setup

After installation, add the integration through the UI:

**Settings → Devices & Services → Add Integration → Battery Controller**

Once the main integration is added, you **must** add your hardware as subentries:

1. Go to **Settings → Devices & Services → Battery Controller**.
2. Click **Add Subentry**.
3. Select **Battery** or **PV Array** and follow the instructions.

Continue to the [configuration reference](configuration.md) for what every field means.

---

## Removal

1. Go to **Settings → Devices & Services → Battery Controller**.
2. Click the three-dot menu and select **Delete**.
3. Confirm the deletion — this removes the integration, all subentries, and all
   associated entities.
4. Restart Home Assistant to ensure all entities are fully removed from the registry.

If entities remain after deletion, go to **Settings → Devices & Services → Entities**,
filter by `battery_controller`, and delete any remaining entries manually.
