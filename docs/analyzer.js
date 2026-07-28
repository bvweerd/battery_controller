// ════════════════════════════════════════════════════════════════════
// analyzer.js  —  pure functions extracted from index.html
// No DOM dependencies; importable by Jest for unit testing.
// ════════════════════════════════════════════════════════════════════

// ── Constants (mirroring Python const.py) ──────────────────────────
const SOC_RES_WH             = 10.0;
const POWER_STEP_W           = 100;
const MAX_SOC_STATES         = 1000;
const DC_TO_AC_EFF           = 0.96;
const MIN_PV_SURPLUS_KW      = 0.05;
const POWER_IDLE_THRESHOLD_W = 1.0;
const MIN_CYCLE_KWH          = 0.2;

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
    // Charging: grid draws the AC setpoint; passive DC MPPT charging
    // continues on top, limited by the remaining headroom.
    gridToBatteryW  = actionW;
    const acStoredWh = actionW * stepH * chargeEff;
    throughputKwh   = acStoredWh / 1000;
    if (pvDcCoupled && pvDcW > 0 && maxSocWh !== undefined) {
      const headroomWh      = Math.max(0, maxSocWh - socWh - acStoredWh);
      const passiveChargeWh = Math.min(pvDcW * dcEff * stepH, headroomWh);
      const passiveChargeW  = stepH > 0 ? passiveChargeWh / stepH : 0;
      const dcPvConsumedW   = dcEff > 0 ? passiveChargeW / dcEff : 0;
      dcPvExcessW    = Math.max(0, pvDcW - dcPvConsumedW);
      throughputKwh += passiveChargeWh / 1000;
    }
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

// Total PV available on the AC side, per step. DC-coupled PV reaches the
// house through the inverter at DC_TO_AC_EFF — the same path the optimizer's
// baseline uses. Charts that only summed the AC series showed a DC-coupled
// system as having no solar at all.
function totalPvSeries(pvFc, pvDcFc) {
  const ac = pvFc || [];
  const dc = pvDcFc || [];
  const n = Math.max(ac.length, dc.length);
  const out = [];
  for (let i = 0; i < n; i++) out.push((ac[i] || 0) + (dc[i] || 0) * DC_TO_AC_EFF);
  return out;
}

// Net position per step: total PV minus consumption (positive = surplus).
function netPvSeries(pvFc, pvDcFc, consumFc) {
  const total = totalPvSeries(pvFc, pvDcFc);
  const consum = consumFc || [];
  return total.map((v, i) => v - (consum[i] || 0));
}

function findNearestSocIdx(socWh, socStates) {
  if (socStates.length <= 1) return 0;
  const step = socStates[1] - socStates[0];
  const idx  = Math.round((socWh - socStates[0]) / step);
  return Math.max(0, Math.min(idx, socStates.length - 1));
}

function runDP(cfg, currentSocKwh, priceFc, feedInFc, pvFc, consumFc,
               stepDurations, degradCost, minPriceSpread, pvDcFc, chargeEffOverride, dischargeEffOverride) {

  const rte      = cfg.rte;
  const sqrtRte  = Math.sqrt(rte);
  const chargeEff    = chargeEffOverride    ?? sqrtRte;
  const dischargeEff = dischargeEffOverride ?? sqrtRte;
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
  // Coarsen the SoC grid for large batteries so the state count stays
  // bounded (mirrors optimizer.py).
  const socResWh  = Math.max(SOC_RES_WH, (maxSocWh - minSocWh) / MAX_SOC_STATES);
  const alignedStepW = socResWh / fullStepH;
  const powerStepW   = Math.max(POWER_STEP_W, alignedStepW);

  const nSocStates = Math.round((maxSocWh - minSocWh) / socResWh) + 1;
  const socStates  = [];
  for (let i = 0; i < nSocStates; i++) socStates.push(minSocWh + i * socResWh);

  // Terminal value: clipped tail average (each tail price capped at median)
  let terminalPrice;
  if (feedInFc.length > 0) {
    const lookback = Math.max(1, Math.min(Math.round(6.0 / fullStepH), feedInFc.length));
    const sortedPrices = [...feedInFc].sort((a, b) => a - b);
    const medianPrice = sortedPrices[Math.floor(sortedPrices.length / 2)];
    const clippedTail = feedInFc.slice(-lookback).map(p => Math.min(p, medianPrice));
    const avgTail = clippedTail.reduce((s, p) => s + p, 0) / clippedTail.length;
    // Clamp at 0: negative feed-in tail must not penalize stored energy.
    terminalPrice = Math.max(0, Math.min(feedInFc[feedInFc.length - 1], avgTail));
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

  // Pre-compute SoC-dependent power limits per state.
  // highSocMaxChargeKw = 0 or absent means no derating.
  const highSocThreshPct  = cfg.highSocChargeThresholdPct  ?? 100;
  const highSocMaxChargeW = (cfg.highSocMaxChargeKw  ?? 0) * 1000;
  const lowSocThreshPct   = cfg.lowSocDischargeThresholdPct ?? 0;
  const lowSocMaxDischargeW = (cfg.lowSocMaxDischargeKw ?? 0) * 1000;
  const socMaxChargeW    = socStates.map(wh => {
    if (highSocMaxChargeW > 0 && wh / cfg.capacityKwh / 10 >= highSocThreshPct)
      return highSocMaxChargeW;
    return maxChargeW;
  });
  const socMaxDischargeW = socStates.map(wh => {
    if (lowSocMaxDischargeW > 0 && wh / cfg.capacityKwh / 10 <= lowSocThreshPct)
      return lowSocMaxDischargeW;
    return maxDischargeW;
  });

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
      const maxChgW = socMaxChargeW[s];
      const maxDisW = socMaxDischargeW[s];
      let bestCost = Infinity;
      let bestAction = 0;

      for (let ai = 0; ai < actions.length; ai++) {
        const actionW = actions[ai];
        let newSocWh;
        if (actionW > 0) {
          if (actionW > maxChgW) continue;
          newSocWh = socWh + actionW * stepH * chargeEff;
          if (newSocWh > maxSocWh) continue;
          if (cfg.pvDcCoupled && pvDcW > 0) {
            const headroomWh = Math.max(0, maxSocWh - newSocWh);
            newSocWh += Math.min(pvDcW * cfg.pvDcEfficiency * stepH, headroomWh);
          }
        } else if (actionW < 0) {
          if (-actionW > maxDisW) continue;
          newSocWh = socWh - Math.abs(actionW) * stepH / dischargeEff;
          if (newSocWh < minSocWh) continue;
        } else {
          if (cfg.pvDcCoupled && pvDcW > 0) {
            const headroomWh = Math.max(0, maxSocWh - socWh);
            newSocWh = socWh + Math.min(pvDcW * cfg.pvDcEfficiency * stepH, headroomWh);
          } else {
            newSocWh = socWh;
          }
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
      // Boundary actions: exact power to reach min/max SoC.
      // new_soc_idx is known directly — no floating-point round-trip needed.
      if (socWh > minSocWh) {
        const drainW = (socWh - minSocWh) * dischargeEff / stepH;
        if (drainW > 0 && drainW <= maxDisW) {
          const stepCost = calculateStepCost(
            stepH, socWh, -drainW, gridPrice, feedIn,
            pvW, consumW, rte, degradCost,
            cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWh
          );
          const totalCost = stepCost + Vnext[0];
          if (totalCost < bestCost) { bestCost = totalCost; bestAction = -drainW; }
        }
      }
      if (socWh < maxSocWh) {
        const fillW = (maxSocWh - socWh) / (stepH * chargeEff);
        if (fillW > 0 && fillW <= maxChgW) {
          const stepCost = calculateStepCost(
            stepH, socWh, fillW, gridPrice, feedIn,
            pvW, consumW, rte, degradCost,
            cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWh
          );
          const totalCost = stepCost + Vnext[nSocStates - 1];
          if (totalCost < bestCost) { bestCost = totalCost; bestAction = fillW; }
        }
      }
      V[t][s]      = bestCost;
      policy[t][s] = bestAction;
    }
  }

  return { V, policy, socStates, socResWh, powerStepW, stepDurations, minSocWh, maxSocWh, terminalPrice, nSteps };
}

function forwardPass(dpResult, cfg, currentSocKwh, pvDcFc, inputs, chargeEffOverride, dischargeEffOverride) {
  const { V, socStates, stepDurations, minSocWh, maxSocWh, nSteps, powerStepW } = dpResult;
  const { priceFc, feedInFc, pvFc, consumFc, degradCost } = inputs;
  const sqrtRte      = Math.sqrt(cfg.rte);
  const chargeEff    = chargeEffOverride    ?? sqrtRte;
  const dischargeEff = dischargeEffOverride ?? sqrtRte;
  const nSocStates   = socStates.length;
  const INF          = Infinity;

  const maxChargeW    = Math.round(cfg.maxChargeKw    * 1000);
  const maxDischargeW = Math.round(cfg.maxDischargeKw * 1000);
  const chargeSteps    = Math.floor(maxChargeW    / powerStepW);
  const dischargeSteps = Math.floor(maxDischargeW / powerStepW);
  const actions = [];
  for (let i = dischargeSteps; i >= 1; i--) actions.push(-i * powerStepW);
  for (let i = chargeSteps;    i >= 0; i--) actions.push( i * powerStepW);

  let curSocWh = currentSocKwh * 1000;  // continuous, not snapped
  const powerKw = [], modes = [], socKwh = [currentSocKwh];

  for (let t = 0; t < nSteps; t++) {
    const stepH      = stepDurations[t];
    const gridPrice  = priceFc[t] ?? 0;
    const feedIn     = feedInFc && t < feedInFc.length ? feedInFc[t] : gridPrice;
    const pvW        = pvFc  && t < pvFc.length  ? pvFc[t]  * 1000 : 0;
    const pvDcW      = pvDcFc && t < pvDcFc.length ? pvDcFc[t] * 1000 : 0;
    const consumW    = consumFc && t < consumFc.length ? consumFc[t] * 1000 : 0;

    const sIdx = findNearestSocIdx(curSocWh, socStates);
    const maxChgW = cfg.highSocChargeThresholdPct && cfg.highSocMaxChargeKw &&
      (curSocWh / (cfg.capacityKwh * 10) >= cfg.highSocChargeThresholdPct)
        ? cfg.highSocMaxChargeKw * 1000 : maxChargeW;
    const maxDisW = cfg.lowSocDischargeThresholdPct && cfg.lowSocMaxDischargeKw &&
      (curSocWh / (cfg.capacityKwh * 10) <= cfg.lowSocDischargeThresholdPct)
        ? cfg.lowSocMaxDischargeKw * 1000 : maxDischargeW;

    let bestCost = INF, bestAction = 0, bestNewSoc = curSocWh;

    for (const actionW of actions) {
      let newSocWh;
      if (actionW > 0) {
        if (actionW > maxChgW) continue;
        newSocWh = curSocWh + actionW * stepH * chargeEff;
        if (newSocWh > maxSocWh) continue;
        if (cfg.pvDcCoupled && pvDcW > 0) {
          const headroomWh = Math.max(0, maxSocWh - newSocWh);
          newSocWh += Math.min(pvDcW * cfg.pvDcEfficiency * stepH, headroomWh);
        }
      } else if (actionW < 0) {
        if (-actionW > maxDisW) continue;
        newSocWh = curSocWh - Math.abs(actionW) * stepH / dischargeEff;
        if (newSocWh < minSocWh) continue;
      } else {
        if (cfg.pvDcCoupled && pvDcW > 0) {
          const dcEff      = cfg.pvDcEfficiency;
          const headroomWh = Math.max(0, maxSocWh - curSocWh);
          const passiveWh  = Math.min(pvDcW * dcEff * stepH, headroomWh);
          newSocWh = curSocWh + passiveWh;
        } else {
          newSocWh = curSocWh;
        }
      }

      const newSocIdx = findNearestSocIdx(newSocWh, socStates);
      if (actionW !== 0 && newSocIdx === sIdx) continue;

      const stepCost = calculateStepCost(
        stepH, curSocWh, actionW, gridPrice, feedIn,
        pvW, consumW, cfg.rte, degradCost,
        cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWh
      );
      const totalCost = stepCost + V[t + 1][newSocIdx];
      if (totalCost < bestCost) { bestCost = totalCost; bestAction = actionW; bestNewSoc = newSocWh; }
    }

    // Boundary actions: exact power to drain to min or fill to max SoC
    if (curSocWh > minSocWh) {
      const drainW = (curSocWh - minSocWh) * dischargeEff / stepH;
      if (drainW > 0 && drainW <= maxDisW) {
        const stepCost = calculateStepCost(
          stepH, curSocWh, -drainW, gridPrice, feedIn,
          pvW, consumW, cfg.rte, degradCost,
          cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWh
        );
        const totalCost = stepCost + V[t + 1][0];
        if (totalCost < bestCost) { bestCost = totalCost; bestAction = -drainW; bestNewSoc = minSocWh; }
      }
    }
    if (curSocWh < maxSocWh) {
      const fillW = (maxSocWh - curSocWh) / (stepH * chargeEff);
      if (fillW > 0 && fillW <= maxChgW) {
        const stepCost = calculateStepCost(
          stepH, curSocWh, fillW, gridPrice, feedIn,
          pvW, consumW, cfg.rte, degradCost,
          cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWh
        );
        const totalCost = stepCost + V[t + 1][nSocStates - 1];
        if (totalCost < bestCost) { bestCost = totalCost; bestAction = fillW; bestNewSoc = maxSocWh; }
      }
    }

    powerKw.push(-bestAction / 1000);
    if (bestAction > 0) modes.push('charging');
    else if (bestAction < 0) modes.push('discharging');
    else modes.push('idle');
    curSocWh = bestNewSoc;
    socKwh.push(curSocWh / 1000);
  }
  return { powerKw, modes, socKwh };
}

// ── Post-processing filters (mirrors optimizer.py) ─────────────────

/**
 * Rebuild SoC schedule from a (possibly filtered) power schedule.
 * Mirrors Python _rebuild_schedule.
 * NOTE: powerKw convention here: negative = charging, positive = discharging
 * (same as forwardPass output).
 */
function rebuildSoc(powerKw, modes, cfg, currentSocKwh, stepDurations, pvDcFc) {
  const sqrtRte      = Math.sqrt(cfg.rte);
  const chargeEff    = cfg.chargeEffOverride    ?? sqrtRte;
  const dischargeEff = cfg.dischargeEffOverride ?? sqrtRte;
  const minSocWh = cfg.minSocKwh * 1000;
  const maxSocWh = cfg.maxSocKwh * 1000;
  const socKwh   = [currentSocKwh];
  let curSocWh   = currentSocKwh * 1000;

  for (let t = 0; t < powerKw.length; t++) {
    const stepH  = stepDurations[t] || 0.25;
    const p      = powerKw[t];
    const pvDcW  = pvDcFc && t < pvDcFc.length ? pvDcFc[t] * 1000 : 0;

    if (modes[t] === 'charging' && p < -1e-9) {
      const actionW = -p * 1000;   // positive
      curSocWh = Math.min(curSocWh + actionW * stepH * chargeEff, maxSocWh);
      if (cfg.pvDcCoupled && pvDcW > 0) {
        const headroomWh = Math.max(0, maxSocWh - curSocWh);
        curSocWh += Math.min(pvDcW * cfg.pvDcEfficiency * stepH, headroomWh);
      }
    } else if (modes[t] === 'discharging' && p > 1e-9) {
      const actionW = p * 1000;    // positive
      curSocWh = Math.max(curSocWh - actionW * stepH / dischargeEff, minSocWh);
    } else {
      if (cfg.pvDcCoupled && pvDcW > 0) {
        const dcEff      = cfg.pvDcEfficiency;
        const headroomWh = Math.max(0, maxSocWh - curSocWh);
        const passiveWh  = Math.min(pvDcW * dcEff * stepH, headroomWh);
        curSocWh = Math.min(curSocWh + passiveWh, maxSocWh);
      }
    }
    socKwh.push(curSocWh / 1000);
  }
  return socKwh;
}

/**
 * Oscillation filter: removes charge↔discharge pairs where the price spread
 * is insufficient to cover RTE losses and degradation.
 * Mirrors Python _filter_oscillations.
 */
function filterOscillations(powerKw, modes, cfg, priceFc, feedInFc, pvFc,
    consumFc, stepDurations, degradCost, minPriceSpread, pvDcFc) {
  if (!powerKw.length) return { powerKw, modes };

  const { rte, minSocKwh, maxSocKwh, pvDcCoupled, pvDcEfficiency, maxDischargeKw } = cfg;
  const sqrtRte  = Math.sqrt(rte);
  const minArb   = (2 * degradCost + minPriceSpread) / sqrtRte;

  const refStepH = stepDurations.length > 1 ? stepDurations[1] : (stepDurations[0] || 0.25);
  const usableKwh = maxSocKwh - minSocKwh;
  const cycleHours = maxDischargeKw > 0 ? usableKwh / maxDischargeKw : 2.0;
  const windowH  = Math.max(2.0, cycleHours);
  const lookahead = Math.max(1, Math.round(windowH / refStepH));

  const filtPow  = [...powerKw];
  const filtMode = [...modes];

  function getChargeCost(i, chargePowKw) {
    // The commanded power is the AC setpoint only: passive DC PV charging
    // happens regardless of the setpoint, so it needs no deduction here.
    if (chargePowKw <= 0 || !pvFc || !consumFc || !feedInFc) return priceFc[i];
    const pvSurplusKw = Math.max(0, (pvFc[i] || 0) - (consumFc[i] || 0));
    const fromPv  = Math.min(chargePowKw, pvSurplusKw);
    const fromGrid = Math.max(0, chargePowKw - fromPv);
    const total   = fromPv + fromGrid;
    if (total <= 0) return priceFc[i];
    return (fromPv * feedInFc[i] + fromGrid * priceFc[i]) / total;
  }

  function getDischargeVal(i, disPowKw) {
    if (disPowKw <= 0) return priceFc[i];
    if (!pvFc || !consumFc || !feedInFc) return priceFc[i];
    const residualKw = Math.max(0, (consumFc[i] || 0) - (pvFc[i] || 0));
    const toSelf  = Math.min(disPowKw, residualKw);
    const toExport = Math.max(0, disPowKw - toSelf);
    const total   = toSelf + toExport;
    if (total <= 0) return priceFc[i];
    return (toSelf * priceFc[i] + toExport * feedInFc[i]) / total;
  }

  let changed = true;
  while (changed) {
    changed = false;
    for (let i = 0; i < filtMode.length - 1; i++) {
      if (filtMode[i] === 'charging') {
        const chargeCost = getChargeCost(i, Math.abs(filtPow[i]));
        let hasDisInWindow = false, hasProfDis = false;
        const end = Math.min(i + lookahead + 1, filtMode.length);
        for (let j = i + 1; j < end; j++) {
          if (filtMode[j] === 'discharging') {
            hasDisInWindow = true;
            const disVal = getDischargeVal(j, Math.abs(filtPow[j]));
            if (disVal - chargeCost / rte >= minArb) { hasProfDis = true; break; }
          }
        }
        if (hasDisInWindow && !hasProfDis) {
          filtPow[i] = 0; filtMode[i] = 'idle'; changed = true;
        }
      } else if (filtMode[i] === 'discharging') {
        const disVal = getDischargeVal(i, Math.abs(filtPow[i]));
        let hasChargeInWindow = false, hasProfCharge = false;
        const end = Math.min(i + lookahead + 1, filtMode.length);
        for (let j = i + 1; j < end; j++) {
          if (filtMode[j] === 'charging') {
            hasChargeInWindow = true;
            const cc = getChargeCost(j, Math.abs(filtPow[j]));
            if (disVal - cc / rte >= minArb) { hasProfCharge = true; break; }
          }
        }
        if (hasChargeInWindow && !hasProfCharge) {
          filtPow[i] = 0; filtMode[i] = 'idle'; changed = true;
        }
      }
    }
  }
  return { powerKw: filtPow, modes: filtMode };
}

/**
 * Micro-cycle filter: removes charge/discharge blocks that move less than
 * MIN_CYCLE_KWH total energy — too small to be worthwhile.
 * Mirrors Python _filter_micro_cycles.
 */
function filterMicroCycles(powerKw, modes, stepDurations) {
  if (!powerKw.length) return { powerKw, modes };

  const filtPow  = [...powerKw];
  const filtMode = [...modes];
  let i = 0;
  while (i < filtMode.length) {
    const dir = filtMode[i];
    if (dir !== 'charging' && dir !== 'discharging') { i++; continue; }
    let j = i, totalEnergy = 0;
    while (j < filtMode.length && filtMode[j] === dir) {
      const stepH = stepDurations[j] || 0.25;
      totalEnergy += Math.abs(filtPow[j]) * stepH;
      j++;
    }
    if (totalEnergy < MIN_CYCLE_KWH) {
      for (let k = i; k < j; k++) { filtPow[k] = 0; filtMode[k] = 'idle'; }
    }
    i = j;
  }
  return { powerKw: filtPow, modes: filtMode };
}

function computeShadowPrice(V, socStates, currentSocKwh) {
  if (socStates.length < 3) return 0;
  const idx     = findNearestSocIdx(currentSocKwh * 1000, socStates);
  const clamped = Math.max(1, Math.min(socStates.length - 2, idx));
  // λ = -dV/dSoC = (V[s-1] - V[s+1]) / (2 * ΔSoC)
  // V at lower SoC > V at higher SoC (more energy = lower future cost),
  // so (V[low] - V[high]) is positive → shadow price is positive.
  // Matches Python: (V[0][idx-1] - V[0][idx+1]) / (2 * step_kwh)
  const dV      = V[0][clamped - 1] - V[0][clamped + 1];
  const dSocKwh = (socStates[clamped + 1] - socStates[clamped - 1]) / 1000;
  return dSocKwh !== 0 ? dV / dSocKwh : 0;
}

/**
 * Baseline cost: no battery exists.
 * DC-coupled PV all goes to AC at DC_TO_AC_EFF (no passive charging).
 * Mirrors Python _calculate_baseline_cost.
 */
function computeBaselineCost(inputs, cfg) {
  const { priceFc, feedInFc, pvFc, consumFc, stepDurations, pvDcFc } = inputs;
  let baseline = 0;
  for (let t = 0; t < priceFc.length; t++) {
    const stepH   = stepDurations[t] || 0.25;
    const feedIn  = feedInFc && feedInFc[t] !== undefined ? feedInFc[t] : priceFc[t];
    const pvW     = pvFc    && pvFc[t]    !== undefined ? pvFc[t]    * 1000 : 0;
    const consumW = consumFc && consumFc[t] !== undefined ? consumFc[t] * 1000 : 0;
    const pvDcW   = pvDcFc  && pvDcFc[t]  !== undefined ? pvDcFc[t]  * 1000 : 0;
    // All DC PV → AC at DC_TO_AC_EFF (no battery to absorb it)
    const dcPvToAcW = pvDcW > 0 ? pvDcW * DC_TO_AC_EFF : 0;
    const totalPvW  = pvW + dcPvToAcW;
    let netGridW    = consumW - totalPvW;
    // Apply the grid capacity cap like calculateStepCost does.
    if (cfg.maxGridPowerKw > 0) {
      const capW = cfg.maxGridPowerKw * 1000;
      netGridW   = Math.max(-capW, Math.min(capW, netGridW));
    }
    const energyKwh = Math.abs(netGridW) * stepH / 1000;
    baseline += netGridW > 0 ? energyKwh * priceFc[t] : -energyKwh * feedIn;
  }
  return baseline;
}

/**
 * Schedule total cost including terminal value.
 * Mirrors Python _calculate_schedule_total_cost.
 */
function computeScheduleCost(powerKw, socKwh, inputs, cfg, terminalPrice) {
  const { priceFc, feedInFc, pvFc, consumFc, stepDurations, degradCost, pvDcFc } = inputs;
  let total = 0;
  const maxSocWhC = cfg.maxSocKwh * 1000;
  for (let t = 0; t < powerKw.length; t++) {
    const stepH   = stepDurations[t] || 0.25;
    const feedIn  = feedInFc && feedInFc[t] !== undefined ? feedInFc[t] : priceFc[t];
    const pvW     = pvFc    && pvFc[t]    !== undefined ? pvFc[t]    * 1000 : 0;
    const consumW = consumFc && consumFc[t] !== undefined ? consumFc[t] * 1000 : 0;
    const pvDcW   = pvDcFc  && pvDcFc[t]  !== undefined ? pvDcFc[t]  * 1000 : 0;
    total += calculateStepCost(
      stepH, socKwh[t] * 1000, -powerKw[t] * 1000,
      priceFc[t], feedIn, pvW, consumW,
      cfg.rte, degradCost, cfg.pvDcCoupled, pvDcW, cfg.pvDcEfficiency, cfg.maxGridPowerKw, maxSocWhC
    );
  }
  // Terminal value: stored energy above min SoC at end of horizon is worth terminalPrice
  if (terminalPrice > 0 && socKwh.length > powerKw.length) {
    const finalSoc      = socKwh[socKwh.length - 1];
    const finalStoredKwh = Math.max(0, finalSoc - cfg.minSocKwh);
    total -= finalStoredKwh * terminalPrice;
  }
  return total;
}

/** @deprecated Use computeBaselineCost + computeScheduleCost directly. */
function computeTotalCost(powerKw, socKwh, inputs, cfg) {
  const baseline = computeBaselineCost(inputs, cfg);
  const total    = computeScheduleCost(powerKw, socKwh, inputs, cfg, 0);
  return { total, baseline };
}

function runOptimizer(cfg, currentSocKwh, inputs) {
  const { priceFc, feedInFc, pvFc, consumFc, stepDurations, degradCost,
          minPriceSpread, pvDcFc, chargeEffCorrection,
          dischargeEffCorrection } = inputs;
  const nominalSqrtRte = Math.sqrt(cfg.rte);
  const chargeEffOverride =
    chargeEffCorrection != null && chargeEffCorrection < 0.995
      ? nominalSqrtRte * chargeEffCorrection
      : null;
  const dischargeEffOverride =
    dischargeEffCorrection != null && dischargeEffCorrection < 0.995
      ? nominalSqrtRte / dischargeEffCorrection
      : null;

  const dp = runDP(cfg, currentSocKwh, priceFc, feedInFc, pvFc, consumFc,
                   stepDurations, degradCost, minPriceSpread, pvDcFc, chargeEffOverride, dischargeEffOverride);
  let { powerKw, modes } = forwardPass(dp, cfg, currentSocKwh, pvDcFc, inputs, chargeEffOverride, dischargeEffOverride);

  // Post-processing filters (matching optimizer.py)
  const oscResult = filterOscillations(
    powerKw, modes, cfg, priceFc, feedInFc || priceFc, pvFc, consumFc,
    dp.stepDurations, degradCost, minPriceSpread || 0.05, pvDcFc
  );
  powerKw = oscResult.powerKw;
  modes   = oscResult.modes;

  const mcResult = filterMicroCycles(powerKw, modes, dp.stepDurations);
  powerKw = mcResult.powerKw;
  modes   = mcResult.modes;

  // Rebuild SoC from filtered schedule
  const socKwh = rebuildSoc(
    powerKw, modes, { ...cfg, chargeEffOverride, dischargeEffOverride }, currentSocKwh, dp.stepDurations, pvDcFc
  );

  const shadow   = computeShadowPrice(dp.V, dp.socStates, currentSocKwh);
  const baseline = computeBaselineCost(inputs, cfg);
  const total    = computeScheduleCost(
    powerKw, socKwh, inputs, cfg, dp.terminalPrice
  );

  // Savings: value added by battery actions only.
  // Subtract initial terminal value so savings = 0 when battery is idle,
  // regardless of how much energy is already stored.
  // Mirrors: savings = baseline - initial_terminal_value - total_cost
  const initialStoredKwh = Math.max(0, currentSocKwh - cfg.minSocKwh);
  const savings = baseline - initialStoredKwh * dp.terminalPrice - total;

  return {
    powerKw, modes, socKwh,
    totalCost: total, baselineCost: baseline, savings,
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
  // Per-cycle → per-kWh: a full cycle is 2 × usable kWh of throughput
  // (charge + discharge), matching the coordinator's conversion.
  const degrad  = degradCycle780 != null ? degradCycle780 / (2 * usableKwh780) : 0.04 / (2 * usableKwh780);
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

  // 0c. Consumption pattern learning. An empty pattern means the consumption
  // forecast is the built-in cold-start curve (~0.4 kW average) rather than
  // the actual household — the usual cause of "my forecast is far too low".
  // Absent key = diagnostics from an older version: stay silent.
  const consumPattern = fc.consumption_hourly_pattern;
  if (consumPattern && typeof consumPattern === 'object') {
    const nBuckets  = Object.keys(consumPattern).length;
    const curConsum = fc.current_consumption_kw;
    const curTxt = curConsum != null
      ? ` Right now it forecasts <b>${curConsum} kW</b> — compare that with your actual household load.`
      : '';
    if (nBuckets === 0) {
      tips.push({ t:'err', title:'No consumption pattern learned — forecast is a built-in default',
        text:`The learned consumption pattern is empty, so the forecast is the built-in cold-start curve
        for a typical household (≈3500 kWh/year, ~0.4 kW average).${curTxt}<br>
        This is <b>not</b> fixed by waiting: the pattern is learned from the last 14 days of
        <i>your own</i> kWh sensors in the recorder, not from how long the integration has run.
        A correctly configured sensor fills the pattern on the next refresh.<br>
        Most common cause: the sensor under <i>Electricity consumption sensors</i> has
        <code>state_class: measurement</code> instead of <code>total_increasing</code>, so it produces
        mean statistics and the hourly <code>change</code> the learner needs does not exist.
        Check it under <b>Developer Tools → Statistics</b>.`});
    } else if (nBuckets < 24) {
      tips.push({ t:'warn', title:`Consumption pattern only partly learned (${nBuckets} of 168 slots)`,
        text:`Only ${nBuckets} hour/weekday slots have been learned so far, so many hours still fall back
        to the built-in default curve.${curTxt}<br>
        This is normal in the first days after adding a new consumption sensor and resolves as the
        recorder accumulates history. If it does not improve, verify the sensor produces sum-type
        statistics (<code>state_class: total_increasing</code>).`});
    }
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

  // 6. SoC boundary hits.
  // soc_schedule_kwh is the schedule the optimizer just produced for the
  // horizon ahead — not measured history. The wording has to say so, or the
  // percentage reads as a claim about what the battery actually did, which
  // sends people looking for a fault in the past.
  const socVals = sched.soc_schedule_kwh || [];
  const maxSoc  = bc.max_soc_kwh || Infinity;
  const minSoc  = bc.min_soc_kwh || 0;
  if (socVals.length > 2) {
    const atMax = socVals.filter(s => s >= maxSoc - 0.15).length / socVals.length;
    const atMin = socVals.filter(s => s <= minSoc + 0.15).length / socVals.length;
    if (atMax > 0.4) {
      tips.push({ t:'info', title:`Planned schedule sits at max SoC for ${(atMax*100).toFixed(0)}% of the horizon`,
        text:`This describes the schedule the optimizer just produced for the hours ahead,
        not what the battery did in the past.<br>
        Planned saturation may indicate: (1) PV production exceeds battery capacity,
        (2) max_soc_percent too conservative, or (3) not enough discharge windows to make room.`});
    }
    if (atMin > 0.35) {
      tips.push({ t:'info', title:`Planned schedule sits at minimum SoC for ${(atMin*100).toFixed(0)}% of the horizon`,
        text:`This describes the schedule the optimizer just produced for the hours ahead,
        not what the battery did in the past.<br>
        The plan leaves the battery depleted for much of the horizon. Could indicate undersized
        capacity vs discharge demand, or min_soc_percent too high. Consider whether this is
        intentional (backup reserve) or not.`});
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

  // 8b. PV forecast volume. Reported explicitly because a DC-coupled system
  // has pv_forecast_kw all zeros with everything in pv_dc_forecast_kw, which
  // otherwise looks like "no PV forecast" in the charts.
  const pvAcFcT  = fc.pv_forecast_kw    || [];
  const pvDcFcT  = fc.pv_dc_forecast_kw || [];
  if (pvAcFcT.length || pvDcFcT.length) {
    const fcStepH = (fc.forecast_interval_minutes || 60) / 60;
    const sum = arr => arr.reduce((a, b) => a + (b || 0), 0);
    const acKwh = sum(pvAcFcT) * fcStepH;
    const dcKwh = sum(pvDcFcT) * fcStepH;
    const totKwh = acKwh + dcKwh;
    if (totKwh <= 0.01) {
      tips.push({ t:'warn', title:'PV forecast is zero over the whole horizon',
        text:`Neither the AC nor the DC PV forecast contains any production.
        If you have solar, check that a PV array subentry is configured with the right
        peak power, orientation and tilt, and that the weather coordinator is reaching
        open-meteo.com. The optimizer will plan as if there is no solar at all.`});
    } else if (acKwh <= 0.01) {
      tips.push({ t:'info', title:`PV forecast: ${dcKwh.toFixed(1)} kWh, all DC-coupled`,
        text:`All forecast production is on the DC side (<code>pv_dc_forecast_kw</code>);
        <code>pv_forecast_kw</code> is zero, which is expected when every array is
        DC-coupled. The "PV AC" series in the chart is flat at zero by design — the
        orange "PV DC" line carries your production, and the net line accounts for it
        through the inverter at ${(DC_TO_AC_EFF*100).toFixed(0)}%.`});
    } else {
      const dcTxt = dcKwh > 0.01
        ? ` (${acKwh.toFixed(1)} kWh AC + ${dcKwh.toFixed(1)} kWh DC-coupled)`
        : '';
      tips.push({ t:'info', title:`PV forecast: ${totKwh.toFixed(1)} kWh over the horizon`,
        text:`Total forecast solar production over the planning horizon${dcTxt}.
        Compare this with what your inverter actually produces on a comparable day —
        a large mismatch points at the array configuration (peak power, orientation, tilt)
        rather than at the optimizer.`});
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
    // effective_power_kw is always 0 in zero_grid mode by design (the real-time
    // controller determines power dynamically, not the DP schedule), so comparing
    // it to setpoint_kw there would always look "limited" even under normal
    // zero_grid charging/discharging. Only compare when effective_mode isn't zero_grid.
    const socLimitedRuns = runLog2.filter(e =>
      e.effective_mode !== 'zero_grid'
      && Math.abs((e.setpoint_kw ?? 0) - (e.effective_power_kw ?? 0)) > 0.05
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
    tips.push({ t:'ok', title:`Saving €${savings.toFixed(4)} over this horizon (${savPct.toFixed(1)}% vs no battery)`,
      text:`Savings compared to having no battery over the planned horizon.
      Actual daily savings depend on how many profitable charge/discharge cycles occur.`});
  }

  if (tips.filter(t => t.t !== 'ok').length === 0) {
    tips.push({ t:'ok', title:'Configuration looks well-tuned', text:'No issues detected.' });
  }
  return tips;
}

// ── Node.js module export (ignored in browser) ─────────────────────
if (typeof module !== 'undefined') {
  module.exports = {
    SOC_RES_WH, POWER_STEP_W, DC_TO_AC_EFF, MIN_PV_SURPLUS_KW, MIN_CYCLE_KWH,
    calculateStepCost,
    totalPvSeries,
    netPvSeries,
    findNearestSocIdx,
    runDP,
    forwardPass,
    rebuildSoc,
    filterOscillations,
    filterMicroCycles,
    computeShadowPrice,
    computeBaselineCost,
    computeScheduleCost,
    computeTotalCost,
    runOptimizer,
    generateTips,
  };
}
