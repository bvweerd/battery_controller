# Glossary

Terms used throughout this documentation, the diagnostics file and the analyzer.

### Arbitrage

Buying energy when it is cheap and using it when it is expensive. The battery is the
store; the profit is the price spread minus efficiency losses and wear.

### Backward induction

The way the DP is solved: start at the end of the horizon, where the value of stored
energy is known, and work backwards computing the best action for every state. See
[the algorithm page](algorithm.md).

### Commitment filter

A post-processing step that keeps an active charge or discharge locked within the same
price period, so the controller does not chatter between actions on small input changes.
It is why `battery_setpoint` can hold steady while `optimal_power` suggests something
else.

### Cold-start curve

The built-in consumption pattern used before anything has been learned from your own
sensors: ≈3500 kWh/year, ~0.4 kW average, with a morning and evening peak. If your
forecast looks suspiciously like a generic household, this is what you are seeing.

### DC-coupled / AC-coupled

Whether a PV array feeds the battery inverter's DC bus directly (DC-coupled, ~97 %
efficient, MPPT only) or passes through its own inverter first (AC-coupled, ~85 %). The
distinction changes the efficiency path in the cost function, so it is not cosmetic.

### Degradation cost

The modelled cost of battery wear per full charge+discharge cycle, in EUR. Part of the
optimizer's cost function, so a trade must beat wear as well as efficiency losses to be
scheduled.

### DP (dynamic programming)

The optimization technique used. The planning horizon is discretized into time steps and
SoC states, and the best action is computed for every combination — which is what allows
the optimizer to reserve capacity now for a better opportunity later.

### Feed-in price

The price you receive for exported energy. Never allowed to be missing: when no feed-in
sensor is configured, the **Fixed feed-in price** is used, because falling back to the
grid price would make PV arbitrage look unprofitable.

### GHI (global horizontal irradiance)

Solar radiation on a horizontal surface, in W/m², fetched from open-meteo.com. Drives the
internal PV model and is one of the features of the historical price model.

### Historical price model

A self-learning model that estimates prices from recorder history, keyed on hour,
weekday, irradiance and wind speed. Used before day-ahead prices publish (~13:00 CET) and
to extend the horizon when live prices cover less than 24 hours.

### Horizon

The forward window the optimizer plans over — rolling, 24 to 36 hours. Decisions near the
end of the horizon lean on the terminal condition rather than on explicit prices.

### Idle

A scheduled action, not an absence of one: the DP has decided that neither charging nor
discharging beats doing nothing in this step.

### Net load

`consumption − PV`. What the house needs from the grid or the battery. The optimizer
computes it itself, which is why the configured consumption sensor must measure **gross**
household load rather than grid import.

### Oscillation filter

A post-DP pass that removes charge↔discharge pairs whose price spread is too small to
cover twice the degradation cost plus the minimum spread, adjusted for round-trip
efficiency. Prevents the plan from trading against itself within a couple of hours.

### RTE (round-trip efficiency)

Energy out divided by energy in over a full cycle. Split as `√RTE` per direction, so
charge efficiency × discharge efficiency reproduces the round trip. A 90 % RTE therefore
means ≈94.87 % each way.

### Rolling horizon

Re-solving the whole horizon from scratch on every run, starting from the current SoC,
rather than incrementally updating the previous plan. The reason two runs minutes apart
can produce visibly different schedules.

### Shadow price (λ)

The marginal value of 1 kWh stored right now, derived from the DP value function
(`λ = −dV[0]/dSoC`). Hybrid mode uses it as a charge/discharge threshold, and it is
exposed as the **Shadow Price of Storage** sensor for use in your own automations.

### SoC (state of charge)

How full the battery is, in % or kWh. Discretized to 10 Wh resolution internally.

### Terminal condition

The value assigned to stored energy at the end of the horizon,
`V[T][s] = −(soc_kwh × feed_in_price_T)`. Without it the optimizer would dump the whole
battery in the final step, since energy beyond the horizon would appear worthless.

### Zero-grid control

Real-time control (~5 s) that drives grid exchange toward zero by charging or discharging
the battery to match the live meter reading. Independent of the DP schedule, and the
mechanism behind the `zero_grid` mode.
