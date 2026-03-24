// ════════════════════════════════════════════════════════════════════
// analyzer.js  —  pure functions extracted from index.html
// No DOM dependencies; importable by Jest for unit testing.
// ════════════════════════════════════════════════════════════════════

// ── Constants (mirroring Python const.py) ──────────────────────────
const SOC_RES_WH             = 25.0;
const POWER_STEP_W           = 100;
const DC_TO_AC_EFF           = 0.96;
const MIN_PV_SURPLUS_KW      = 0.05;
const POWER_IDLE_THRESHOLD_W = 1.0;

// ── DP core ────────────────────────────────────────────────────────

function calculateStepCost(stepH, socWh, actionW, gridPrice, feedInPrice,
    pvW, consumW, rte, degradCostPerKwh, pvDcCoupled, pvDcW, pvDcEfficiency, maxGridPowerKw, maxSocWh) {
  const sqrtRte     = Math.sqrt(rte);
  const chargeEff   = sqrtRte;
  const dischargeEff= sqrtRte;
  const dcEff       = pvDcCoupled ? pvDcEfficiency : sqrtRte;

  pvW   = Math.max(0, pvW);
  pvDcW = Math.max(0, pvDcW || 0);

  let gridToBatteryW = 0, throughputKwh = 0;
  let dcPvExcessW = pvDcW;

  if (actionW > 0) {
    // Charging
    const dcChargeW = Math.min(actionW, pvDcW * dcEff);
    const acChargeW = actionW - dcChargeW;
    const dcPvUsedW = dcEff > 0 ? dcChargeW / dcEff : 0;
    dcPvExcessW     = Math.max(0, pvDcW - dcPvUsedW);
    gridToBatteryW  = acChargeW;
    throughputKwh   = actionW * stepH * chargeEff / 1000;
  } else if (actionW < 0) {
    // Discharging
    dcPvExcessW     = pvDcW;
    const usablePowerW = Math.abs(actionW);
    gridToBatteryW  = -usablePowerW;
    throughputKwh   = Math.abs(actionW) * stepH / dischargeEff / 1000;
  } else {
    // Idle
    gridToBatteryW = 0;
    if (pvDcCoupled && pvDcW > 0 && maxSocWh !== undefined) {
      const headroomWh      = Math.max(0, maxSocWh - socWh);
      const passiveChargeWh = Math.min(pvDcW * dcEff * stepH, headroomWh);
      const passiveChargeW  = stepH > 0 ? passiveChargeWh / stepH : 0;
      const dcPvConsumedW   = dcEff > 0 ? passiveChargeW / dcEff : 0;
      dcPvExcessW   = Math.max(0, pvDcW - dcPvConsumedW);
      throughputKwh = passiveChargeWh / 1000;
    }
  }

  const dcPvToAcW  = dcPvExcessW > 0 ? dcPvExcessW * DC_TO_AC_EFF : 0;
  const totalAcPvW = pvW + dcPvToAcW;
  let netGridW     = consumW - totalAcPvW + gridToBatteryW;
  if (maxGridPowerKw > 0) {
    const capW = maxGridPowerKw * 1000;
    netGridW   = Math.max(-capW, Math.min(capW, netGridW));
  }

  const energyKwh = Math.abs(netGridW) * stepH / 1000;
  const gridCost  = netGridW > 0 ? energyKwh * gridPrice : -energyKwh * feedInPrice;
  const degradCost= throughputKwh * degradCostPerKwh;
  return gridCost + degradCost;
}

function findNearestSocIdx(socWh, socStates) {
  if (socStates.length <= 1) return 0;
  const step = socStates[1] - socStates[0];
  const idx  = Math.round((socWh - socStates[0]) / step);
  return Math.max(0, Math.min(idx, socStates.length - 1));
}

function runDP(cfg, currentSocKwh, priceFc, feedInFc, pvFc, consumFc,
               stepDurations, degradCost, minPriceSpread, pvDcFc, terminalShadowPrice) {

  const rte     = cfg.rte;
  const sqrtRte = Math.sqrt(rte);
  const minSocWh = Math.round(cfg.minSocKwh * 1000);
  const maxSocWh = Math.round(cfg.maxSocKwh * 1000);

  const nSteps = Math.min(priceFc.length, pvFc.length, consumFc.length);
  if (!stepDurations || stepDurations.length === 0)
    stepDurations = new Array(nSteps).fill(0.25);
  while (stepDurations.length < nSteps)
    stepDurations.push(stepDurations[stepDurations.length - 1]);
  if (!pvDcFc) pvDcFc = new Array(nSteps).fill(0);
  if (!feedInFc || feedInFc.length === 0) feedInFc = [...priceFc];

  const minStepH  = Math.min(...stepDurations.slice(0, nSteps));
  const fullStepH = stepDurations.length > 1 ? stepDurations[1] : minStepH;
  const socResWh  = SOC_RES_WH;
  const alignedStepW = socResWh / fullStepH;
  const powerStepW   = Math.max(POWER_STEP_W, alignedStepW);

  const nSocStates = Math.round((maxSocWh - minSocWh) / socResWh) + 1;
  const socStates  = [];
  for (let i = 0; i < nSocStates; i++) socStates.push(minSocWh + i * socResWh);

  // Terminal value
  let terminalPrice;
  if (terminalShadowPrice !== null && terminalShadowPrice !== undefined && terminalShadowPrice >= 0) {
    terminalPrice = terminalShadowPrice;
  } else if (feedInFc.length > 0) {
    const lookback = Math.min(6, feedInFc.length);
    let sum = 0;
    for (let i = feedInFc.length - lookback; i < feedInFc.length; i++) sum += feedInFc[i];
    const avgTail = sum / lookback;
    terminalPrice = Math.min(feedInFc[feedInFc.length - 1], avgTail);
  } else {
    terminalPrice = 0;
  }

  const V      = [];
  const policy = [];
  for (let t = 0; t <= nSteps; t++) {
    V.push(new Float64Array(nSocStates).fill(Infinity));
    if (t < nSteps) policy.push(new Float32Array(nSocStates));
  }
  for (let s = 0; s < nSocStates; s++) {
    const storedKwh = (socStates[s] - minSocWh) / 1000;
    V[nSteps][s] = -storedKwh * terminalPrice;
  }

  const maxChargeW     = Math.round(cfg.maxChargeKw * 1000);
  const maxDischargeW  = Math.round(cfg.maxDischargeKw * 1000);
  const chargeSteps    = Math.floor(maxChargeW    / powerStepW);
  const dischargeSteps = Math.floor(maxDischargeW / powerStepW);
  const actions = [];
  for (let i = dischargeSteps; i >= 1; i--) actions.push(-i * powerStepW);
  for (let i = chargeSteps; i >= 0; i--)    actions.push(i * powerStepW);

  for (let t = nSteps - 1; t >= 0; t--) {
    const stepH     = stepDurations[t];
    const gridPrice = priceFc[t];
    const feedIn    = t < feedInFc.length ? feedInFc[t] : gridPrice;
    const pvW       = t < pvFc.length    ? pvFc[t]    * 1000  : 0;
    const pvDcW     = t < pvDcFc.length  ? pvDcFc[t]  * 1000  : 0;
    const consumW   = t < consumFc.length ? consumFc[t] * 1000 : 0;
    const Vnext     = V[t + 1];

    for (let s = 0; s < nSocStates; s++) {
      const socWh  = socStates[s];
      let bestCost = Infinity;
      let bestAction = 0;

      for (let ai = 0; ai < actions.length; ai++) {
        const actionW = actions[ai];
        let newSocWh;
        if (actionW > 0) {
          newSocWh = socWh + actionW * stepH * sqrtRte;
          if (newSocWh > maxSocWh) continue;
        } else if (actionW < 0) {
          newSocWh = socWh - Math.abs(actionW) * stepH / sqrtRte;
          if (newSocWh < minSocWh) continue;
        } else {
          newSocWh = socWh;
        }

        const newSocIdx = findNearestSocIdx(newSocWh, socStates);
        if (actionW !== 0 && newSocIdx === s) continue;

        const stepCost  = calculateStepCost(
          stepH, socWh, actionW, gridPrice, feedIn,
          pvW, consumW, rte, degradCost,
          cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWh
        );
        const totalCost = stepCost + Vnext[newSocIdx];
        if (totalCost < bestCost) {
          bestCost   = totalCost;
          bestAction = actionW;
        }
      }
      V[t][s]      = isFinite(bestCost) ? bestCost : Vnext[s];
      policy[t][s] = bestAction;
    }
  }

  return { V, policy, socStates, socResWh, powerStepW, stepDurations, minSocWh, maxSocWh, terminalPrice, nSteps };
}

function forwardPass(dpResult, cfg, currentSocKwh, pvDcFc) {
  const { policy, socStates, stepDurations, minSocWh, maxSocWh, nSteps } = dpResult;
  const sqrtRte = Math.sqrt(cfg.rte);
  let curSocWh = currentSocKwh * 1000;
  const powerKw = [], modes = [], socKwh = [currentSocKwh];

  for (let t = 0; t < nSteps; t++) {
    const stepH   = stepDurations[t];
    const sIdx    = findNearestSocIdx(curSocWh, socStates);
    const actionW = policy[t][sIdx];
    const pvDcW   = pvDcFc && t < pvDcFc.length ? pvDcFc[t] * 1000 : 0;

    powerKw.push(-actionW / 1000);
    if (actionW > 0) {
      modes.push('charging');
      curSocWh = Math.min(curSocWh + actionW * stepH * sqrtRte, maxSocWh);
    } else if (actionW < 0) {
      modes.push('discharging');
      curSocWh = Math.max(curSocWh - Math.abs(actionW) * stepH / sqrtRte, minSocWh);
    } else {
      if (cfg.pvDcCoupled && pvDcW > 0) {
        const dcEff      = cfg.pvDcEfficiency;
        const headroomWh = Math.max(0, maxSocWh - curSocWh);
        const passiveWh  = Math.min(pvDcW * dcEff * stepH, headroomWh);
        curSocWh += passiveWh;
      }
      modes.push('idle');
    }
    socKwh.push(curSocWh / 1000);
  }
  return { powerKw, modes, socKwh };
}

function computeShadowPrice(V, socStates, currentSocKwh) {
  if (socStates.length < 3) return 0;
  const idx     = findNearestSocIdx(currentSocKwh * 1000, socStates);
  const clamped = Math.max(1, Math.min(socStates.length - 2, idx));
  const dV      = V[0][clamped - 1] - V[0][clamped + 1];
  const dSocKwh = (socStates[clamped + 1] - socStates[clamped - 1]) / 1000;
  return dSocKwh !== 0 ? -dV / dSocKwh : 0;
}

function computeTotalCost(powerKw, socKwh, inputs, cfg) {
  let total = 0, baseline = 0;
  const { priceFc, feedInFc, pvFc, consumFc, stepDurations, degradCost, pvDcFc } = inputs;
  for (let t = 0; t < powerKw.length; t++) {
    const stepH  = stepDurations[t] || 0.25;
    const feedIn = feedInFc && feedInFc[t] !== undefined ? feedInFc[t] : priceFc[t];
    const pvW    = pvFc    && pvFc[t]    !== undefined ? pvFc[t]    * 1000 : 0;
    const consumW= consumFc && consumFc[t] !== undefined ? consumFc[t] * 1000 : 0;
    const pvDcW  = pvDcFc  && pvDcFc[t]  !== undefined ? pvDcFc[t]  * 1000 : 0;
    const maxSocWhC = cfg.maxSocKwh * 1000;
    total += calculateStepCost(
      stepH, socKwh[t] * 1000, -powerKw[t] * 1000,
      priceFc[t], feedIn, pvW, consumW,
      cfg.rte, degradCost, cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWhC
    );
    baseline += calculateStepCost(
      stepH, socKwh[t] * 1000, 0,
      priceFc[t], feedIn, pvW, consumW,
      cfg.rte, 0, cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWhC
    );
  }
  return { total, baseline };
}

function runOptimizer(cfg, currentSocKwh, inputs) {
  const { priceFc, feedInFc, pvFc, consumFc, stepDurations, degradCost,
          minPriceSpread, pvDcFc, terminalShadowPrice } = inputs;

  const dp = runDP(cfg, currentSocKwh, priceFc, feedInFc, pvFc, consumFc,
                   stepDurations, degradCost, minPriceSpread, pvDcFc, terminalShadowPrice);
  const { powerKw, modes, socKwh } = forwardPass(dp, cfg, currentSocKwh, pvDcFc);
  const shadow = computeShadowPrice(dp.V, dp.socStates, currentSocKwh);
  const { total, baseline } = computeTotalCost(powerKw, socKwh, inputs, cfg);

  return {
    powerKw, modes, socKwh,
    totalCost: total, baselineCost: baseline, savings: baseline - total,
    shadowPrice: shadow, terminalPrice: dp.terminalPrice,
    nSocStates: dp.socStates.length, powerStepW: dp.powerStepW, socResWh: dp.socResWh,
  };
}

// ── Diagnostics tips ───────────────────────────────────────────────

function generateTips(d) {
  const bc   = d.battery_config || {};
  const opt  = d.optimization   || {};
  const bs   = opt.battery_state || {};
  const sched= opt.schedule      || {};
  const fc   = d.forecast        || {};
  const opts = d.config_entry?.options || {};
  const tips = [];

  const rte     = bc.round_trip_efficiency || 0.9;
  const sqrtRte = Math.sqrt(rte);
  const usableKwh780 = ((bc.max_soc_kwh || 0) - (bc.min_soc_kwh || 0)) || 1;
  const degradCycle780 = opts.degradation_cost_per_cycle ?? opts.degradation_cost_per_kwh;
  const degrad  = degradCycle780 != null ? degradCycle780 / usableKwh780 : 0.04 / usableKwh780;
  const spread  = opts.min_price_spread         || 0.05;
  const savings = opt.savings   || 0;
  const baseline= opt.baseline_cost || 0;
  const savPct  = baseline > 0 ? savings / baseline * 100 : 0;
  const prices  = sched.price_forecast || [];
  const maxP    = prices.length > 0 ? Math.max(...prices) : 0;
  const minP    = prices.length > 0 ? Math.min(...prices) : 0;
  const actualSpread = maxP - minP;
  const minArb  = (2 * degrad + spread) / sqrtRte;

  // 0. Failure reason
  if (opt.failure_reason) {
    tips.push({ t:'err', title:'Optimization coordinator failed',
      text:`Last failure: <code>${opt.failure_reason}</code><br>The optimizer may be running on stale data.
      Check Home Assistant logs for details and resolve the underlying error.`});
  }

  // 0b. Price forecast source
  const priceSrc = opt.price_forecast_source || '';
  if (priceSrc === 'current_only') {
    tips.push({ t:'err', title:'Price forecast is current price only — no future prices',
      text:`Price forecast source: <b>current_only</b>. The optimizer only has the current price,
      not a multi-hour forecast. This severely limits arbitrage scheduling.<br>
      Fix: connect a dynamic price sensor (Nordpool, ENTSO-E, or an entity that exposes a list/forecast).`});
  } else if (priceSrc === 'historical_model') {
    tips.push({ t:'warn', title:'Price forecast using historical model (no live data)',
      text:`Price forecast source: <b>historical_model</b>. No live price sensor is connected —
      the optimizer uses a learned daily pattern instead of actual tariff data.
      For dynamic tariffs, connect a Nordpool / ENTSO-E sensor for better results.`});
  } else if (priceSrc === 'live+historical_model') {
    tips.push({ t:'info', title:'Price forecast: live + historical model fill',
      text:`Live price data is available but not covering the full horizon — the historical model fills the gap.
      This is normal for day-ahead prices before they publish (usually afternoon).`});
  }

  // 1. RTE
  if (rte < 0.82) {
    tips.push({ t:'err', title:'Very low round-trip efficiency (RTE)',
      text:`RTE = ${(rte*100).toFixed(0)}%. Energy loss per cycle is ${((1-rte)*100).toFixed(0)}%.
      This makes arbitrage much harder to be profitable. Check inverter settings, cable quality,
      and battery health. LFP typical: 93–96%, NMC: 89–93%.`});
  } else if (rte < 0.88) {
    tips.push({ t:'warn', title:'Below-average round-trip efficiency',
      text:`RTE = ${(rte*100).toFixed(0)}%. Could be accurate (older battery, warm conditions)
      but worth verifying. Check manufacturer specs vs measured values.`});
  } else if (rte > 0.98) {
    tips.push({ t:'warn', title:'Unrealistically high RTE',
      text:`RTE = ${(rte*100).toFixed(0)}% is above typical maximum. This will cause the optimizer to overestimate savings. Typical max is 96–97%.`});
  }

  // 2. Price spread vs minimum needed
  if (prices.length > 0) {
    if (actualSpread < minArb) {
      tips.push({ t:'warn', title:'Price spread below arbitrage threshold',
        text:`Forecast spread: ${(actualSpread*100).toFixed(1)} ct/kWh.
        Minimum needed for profitable arbitrage: ${(minArb*100).toFixed(1)} ct/kWh
        (2×degradation + min_price_spread, divided by √RTE).
        The optimizer correctly schedules idle. If your real tariff has higher peaks,
        check if your price sensor is up-to-date.`});
    } else {
      const margin = actualSpread - minArb;
      tips.push({ t:'ok', title:'Price spread sufficient for arbitrage',
        text:`Spread ${(actualSpread*100).toFixed(1)} ct/kWh vs minimum ${(minArb*100).toFixed(1)} ct/kWh.
        Margin: ${(margin*100).toFixed(1)} ct. Potential max profit per full cycle: €${(margin*(bc.max_soc_kwh||0)-(bc.min_soc_kwh||0)).toFixed(3)}.`});
    }
  }

  // 3. Min price spread setting
  if (spread > 0.12) {
    tips.push({ t:'warn', title:'min_price_spread is high',
      text:`Set to ${(spread*100).toFixed(0)} ct/kWh. This aggressively blocks arbitrage — any charge/discharge
      pair with a spread below ${(minArb*100).toFixed(1)} ct is suppressed.
      Consider reducing to 3–5 ct if your forecast is accurate.`});
  }

  // 4. Control mode
  const mode = opt.control_mode || '';
  if (mode === 'manual') {
    tips.push({ t:'err', title:'Manual mode: optimizer is disabled',
      text:`The battery is being controlled manually. Automatic cost optimization is off.
      Switch to follow_schedule, hybrid, or zero_grid in integration options.`});
  }
  if (mode === 'follow_schedule') {
    tips.push({ t:'info', title:'Consider switching to hybrid mode',
      text:`follow_schedule mode executes the committed DP schedule. Within the same price
      period, the commitment filter may keep an active charge/discharge step locked, and
      that lock is reflected in the published controller setpoint as well. Hybrid mode
      additionally applies real-time zero-grid balancing between optimization runs, which
      handles unexpected PV/consumption swings. Recommended for variable households.`});
  }

  // 5. Feed-in sensor
  const hasFeedInSensor = (opts.feed_in_price_sensor && opts.feed_in_price_sensor !== '') ||
    (sched.feed_in_price_forecast && sched.feed_in_price_forecast.length > 0);
  const fixedFeedIn = opts.fixed_feed_in_price || 0.04;
  if (!hasFeedInSensor) {
    tips.push({ t:'info', title:'No live feed-in price sensor',
      text:`Feed-in tariff uses fixed fallback: €${fixedFeedIn.toFixed(3)}/kWh.
      If your energy contract has a dynamic feed-in tariff (e.g. Nordpool-linked),
      connecting a sensor improves PV and export timing decisions.`});
  }

  // 6. SoC boundary hits
  const socVals = sched.soc_schedule_kwh || [];
  const maxSoc  = bc.max_soc_kwh || Infinity;
  const minSoc  = bc.min_soc_kwh || 0;
  if (socVals.length > 2) {
    const atMax = socVals.filter(s => s >= maxSoc - 0.15).length / socVals.length;
    const atMin = socVals.filter(s => s <= minSoc + 0.15).length / socVals.length;
    if (atMax > 0.4) {
      tips.push({ t:'info', title:`Battery at max SoC ${(atMax*100).toFixed(0)}% of the time`,
        text:`Frequent saturation may indicate: (1) PV production exceeds battery capacity,
        (2) max_soc_percent too conservative, or (3) not enough discharge windows to make room.`});
    }
    if (atMin > 0.35) {
      tips.push({ t:'info', title:`Battery at minimum SoC ${(atMin*100).toFixed(0)}% of the time`,
        text:`Battery is frequently depleted. Could indicate undersized capacity vs discharge demand,
        or min_soc_percent too high. Consider whether this is intentional (backup reserve) or not.`});
    }
  }

  // 7. All idle
  const modes = sched.mode_schedule || [];
  if (modes.length > 0 && modes.every(m => m === 'idle')) {
    tips.push({ t:'warn', title:'Entire schedule is idle — no arbitrage scheduled',
      text:`The optimizer finds no profitable charge/discharge opportunities.
      Check: (1) price spread vs threshold above, (2) freshness of price sensor,
      (3) min_price_spread setting, (4) battery may already be at optimal SoC.`});
  }

  // 8. Stale optimization
  const ts = opt.timestamp;
  if (ts) {
    const ageMin = (Date.now() - new Date(ts)) / 60000;
    if (ageMin > 60) {
      tips.push({ t:'err', title:`Optimization data is ${ageMin.toFixed(0)} minutes old`,
        text:`Expected refresh at each price period boundary (15–60 min depending on your price sensor). Check if Home Assistant and the integration
        are running. Possible issues: HA restart, coordinator error, or connection problem.`});
    }
  }

  // 9. DC-coupled PV
  if (bc.pv_dc_coupled) {
    const dcEff = bc.pv_dc_efficiency || 0;
    if (dcEff < 0.93) {
      tips.push({ t:'warn', title:'Low DC PV efficiency configured',
        text:`DC efficiency = ${(dcEff*100).toFixed(0)}%. Expected 95–98% for modern MPPT charge controllers.
        Low value may cause optimizer to undervalue DC PV, reducing self-consumption efficiency.`});
    }
  } else {
    tips.push({ t:'info', title:'AC-coupled PV',
      text:`AC-coupled PV goes through inverter (~85% efficiency). DC-coupled PV (panels directly
      on battery inverter) achieves ~97% and enables passive battery charging when idle —
      consider DC coupling if your hardware supports it.`});
  }

  // 10. Setpoint deviation analysis (requires optimizer_run_log / setpoint_log)
  const runLog2 = opt.optimizer_run_log || [];
  const setpLog2 = opt.setpoint_log || [];

  if (runLog2.length > 0) {
    const socLimitedRuns = runLog2.filter(e =>
      Math.abs((e.setpoint_kw ?? 0) - (e.effective_power_kw ?? 0)) > 0.05
    );
    const commitLocked = runLog2.filter(e => e.commitment_locked);

    if (socLimitedRuns.length > 0) {
      const pct = Math.round(socLimitedRuns.length / runLog2.length * 100);
      const examples = socLimitedRuns.slice(-3).map(e =>
        `${(e.timestamp||'').slice(11,16)}: eff=${(e.effective_power_kw??0).toFixed(2)} kW → setp=${(e.setpoint_kw??0).toFixed(2)} kW (SoC=${(e.soc_kwh??0).toFixed(2)} kWh)`
      ).join('<br>');
      tips.push({ t:'warn', title:`Setpoint was SoC/power limited in ${socLimitedRuns.length}/${runLog2.length} optimizer runs (${pct}%)`,
        text:`The published setpoint differed from the effective power because the battery hit a SoC or power limit.
        This is expected behavior, not a bug — it means the optimizer planned an action that became impossible at execution time.
        <br><br><b>Recent examples:</b><br>${examples}
        <br><br>If this happens frequently, consider adjusting min/max SoC settings or reviewing forecast accuracy.`});
    }

    if (commitLocked.length > 0) {
      const pct = Math.round(commitLocked.length / runLog2.length * 100);
      const byReason = {};
      commitLocked.forEach(e => { byReason[e.commitment_reason || 'unknown'] = (byReason[e.commitment_reason || 'unknown'] || 0) + 1; });
      const reasons = Object.entries(byReason).map(([k,v]) => `${k}: ${v}×`).join(', ');
      tips.push({ t:'info', title:`Commitment filter active in ${commitLocked.length}/${runLog2.length} runs (${pct}%)`,
        text:`The commitment filter overrode the DP output to prevent rapid charge/discharge oscillation.
        This is expected for the first run after a price-period boundary.
        <br>Reasons: ${reasons}
        <br><br>If commitment is active continuously across many price periods, check <b>min_price_spread</b> — it may be too low, causing the optimizer to oscillate.`});
    }
  }

  if (setpLog2.length > 0) {
    const socLimitedRt = setpLog2.filter(e => e.soc_limited);
    if (socLimitedRt.length > 0) {
      const pct = Math.round(socLimitedRt.length / setpLog2.length * 100);
      tips.push({ t:'warn', title:`SoC limit blocked real-time setpoint in ${socLimitedRt.length} setpoint changes (${pct}%)`,
        text:`The real-time controller wanted to charge/discharge but the battery SoC was at its limit.
        Charge was blocked at max SoC, or discharge was blocked at min SoC.
        This is normal during saturation or depletion, but frequent blocking may indicate the schedule is over-committing relative to actual capacity.`});
    }
  }

  // 12. Savings summary
  if (savings > 0 && baseline > 0) {
    tips.push({ t:'ok', title:`Saving €${savings.toFixed(4)} over this horizon (${savPct.toFixed(1)}%)`,
      text:`Extrapolated: ~€${(savings * 96).toFixed(2)}/day (assuming similar price pattern 24h).
      Actual savings depend on price volatility throughout the day.`});
  }

  if (tips.filter(t => t.t !== 'ok').length === 0) {
    tips.push({ t:'ok', title:'Configuration looks well-tuned', text:'No issues detected.' });
  }
  return tips;
}

// ── Node.js module export (ignored in browser) ─────────────────────
if (typeof module !== 'undefined') {
  module.exports = {
    SOC_RES_WH, POWER_STEP_W, DC_TO_AC_EFF, MIN_PV_SURPLUS_KW,
    calculateStepCost,
    findNearestSocIdx,
    runDP,
    forwardPass,
    computeShadowPrice,
    computeTotalCost,
    runOptimizer,
    generateTips,
  };
}
