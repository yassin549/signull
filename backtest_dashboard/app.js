let strategies = [];
let selectedId = null;
let running = false;
let lastResult = null;
let allHistory = false;
let liveRun = null;
let liveDrawQueued = false;

async function init() {
  const res = await fetch("/api/strategies");
  const data = await res.json();
  strategies = data.strategies || [];
  const sel = document.getElementById("strategy-select");
  if (!sel) return;
  sel.innerHTML = strategies.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
  sel.addEventListener("change", onStrategyChange);
  document.getElementById("run-btn").addEventListener("click", runBacktest);
  document.querySelectorAll(".period-btn[data-days]").forEach(btn =>
    btn.addEventListener("click", () => setRecentRange(Number(btn.dataset.days)))
  );
  document.getElementById("all-history-btn").addEventListener("click", enableAllHistory);
  const startEl = document.getElementById("start-date");
  const endEl = document.getElementById("end-date");
  if (startEl) {
    startEl.addEventListener("change", () => { allHistory = false; updateCandleEstimate(); updatePeriodButtonState(null); });
    startEl.addEventListener("input", () => { allHistory = false; updateCandleEstimate(); updatePeriodButtonState(null); });
  }
  if (endEl) {
    endEl.addEventListener("change", () => { allHistory = false; updateCandleEstimate(); updatePeriodButtonState(null); });
    endEl.addEventListener("input", () => { allHistory = false; updateCandleEstimate(); updatePeriodButtonState(null); });
  }
  document.getElementById("asset-select").addEventListener("change", updateCandleEstimate);
  const riskTypeSel = document.getElementById("risk-type-select");
  if (riskTypeSel) {
    riskTypeSel.addEventListener("change", onRiskTypeChange);
    riskTypeSel.addEventListener("input", onRiskTypeChange);
    onRiskTypeChange();
  }
  bindEquityChartEvents();
  setRecentRange(7);
  if (strategies.length) {
    const preferred = strategies.find(s => s.id === "signull_1_0");
    selectedId = preferred ? preferred.id : strategies[0].id;
    sel.value = selectedId;
    onStrategyChange();
  }
}

function onRiskTypeChange() {
  const select = document.getElementById("risk-type-select");
  if (!select) return;
  const mode = select.value;
  const label = document.getElementById("risk-value-label");
  const prefix = document.getElementById("risk-unit-prefix");
  const suffix = document.getElementById("risk-unit-suffix");
  const hint = document.getElementById("risk-value-hint");
  const typeHint = document.getElementById("risk-type-hint");
  const wrap = document.getElementById("risk-value-wrap");
  const input = document.getElementById("risk-value");

  if (mode === "fixed_dollar") {
    if (label) label.textContent = "Fixed Amount ($)";
    if (prefix) {
      prefix.classList.remove("hidden");
      prefix.style.display = "inline-block";
    }
    if (suffix) {
      suffix.classList.add("hidden");
      suffix.style.display = "none";
    }
    if (wrap) wrap.classList.add("has-prefix");
    if (hint) hint.textContent = "Fixed USD amount staked per trade.";
    if (typeHint) typeHint.textContent = "Fixed USD amount per trade.";
    if (input) {
      input.placeholder = "1";
      if (!input.value || Number(input.value) <= 0) input.value = "1";
    }
  } else {
    if (label) label.textContent = "Risk Value (%)";
    if (prefix) {
      prefix.classList.add("hidden");
      prefix.style.display = "none";
    }
    if (suffix) {
      suffix.classList.remove("hidden");
      suffix.style.display = "inline-block";
    }
    if (wrap) wrap.classList.remove("has-prefix");
    if (hint) hint.textContent = "Percentage of current balance risked per trade.";
    if (typeHint) typeHint.textContent = "Risk % of balance per trade.";
    if (input) {
      input.placeholder = "10";
      if (!input.value || Number(input.value) <= 0) input.value = "10";
    }
  }
}

function utcDateTimeInput(date) {
  const pad = n => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`
    + `T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

function utcInputToUnix(value) {
  const ts = Date.parse(`${value}:00Z`);
  return Number.isFinite(ts) ? Math.floor(ts / 1000) : NaN;
}

function updatePeriodButtonState(activeDays) {
  document.querySelectorAll(".period-btn[data-days]").forEach(btn => {
    btn.classList.toggle("active", Number(btn.dataset.days) === activeDays);
  });
  const allBtn = document.getElementById("all-history-btn");
  if (allBtn) allBtn.classList.toggle("active", activeDays === "all");
}

function setRecentRange(days) {
  allHistory = false;
  const end = new Date();
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - days);
  document.getElementById("start-date").value = utcDateTimeInput(start);
  document.getElementById("end-date").value = utcDateTimeInput(end);
  document.getElementById("date-range-hint").textContent = `Running the last ${days} UTC day${days === 1 ? "" : "s"}, to the minute.`;
  updatePeriodButtonState(days);
  updateCandleEstimate();
}

async function enableAllHistory() {
  allHistory = true;
  updatePeriodButtonState("all");
  const hint = document.getElementById("date-range-hint");
  hint.textContent = "Finding the earliest retained Polymarket market…";
  try {
    const asset = document.getElementById("asset-select").value;
    const res = await fetch(`/api/availability?asset=${encodeURIComponent(asset)}`);
    const data = await res.json();
    if (!res.ok || !data.first_available_start_ts) throw new Error(data.detail || "No history found");
    document.getElementById("start-date").value = utcDateTimeInput(new Date(data.first_available_start_ts * 1000));
    document.getElementById("end-date").value = utcDateTimeInput(new Date());
    hint.textContent = "All retained Polymarket history selected. Large first-time downloads can take a while.";
    document.getElementById("candle-estimate").textContent = "All retained history selected; the exact total will appear while data loads.";
  } catch (e) {
    allHistory = false;
    updatePeriodButtonState(null);
    hint.textContent = "Could not find the earliest available history. Choose a date range instead.";
  }
}

function updateCandleEstimate() {
  const el = document.getElementById("candle-estimate");
  if (!el) return;
  if (allHistory) {
    el.textContent = "All retained history selected; the exact total will appear while data loads.";
    return;
  }
  const start = utcInputToUnix(document.getElementById("start-date").value);
  const end = utcInputToUnix(document.getElementById("end-date").value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    el.textContent = "Choose a valid start and end time.";
    return;
  }
  const first = Math.ceil(start / 300) * 300;
  const last = Math.floor((end - 300) / 300) * 300;
  const estimate = Math.max(0, Math.floor((last - first) / 300) + 1);
  el.textContent = `Estimated ${estimate.toLocaleString()} five-minute candles (before unavailable markets).`;
}

function currentStrategy() {
  return strategies.find(s => s.id === selectedId);
}

function paramStep(v) {
  if (Number.isInteger(v)) return "1";
  if (Math.abs(v) >= 1) return "0.05";
  return "0.01";
}

function onStrategyChange() {
  selectedId = document.getElementById("strategy-select").value;
  const s = currentStrategy();
  if (!s) return;
  document.getElementById("strategy-desc").textContent = s.description;
  const box = document.getElementById("params-box");
  const filteredParams = Object.entries(s.default_params || {}).filter(([k]) => k !== "risk_pct");
  if (!filteredParams.length) {
    box.innerHTML = '<span class="hint" style="margin-top:0;">Default strategy settings</span>';
  } else {
    box.innerHTML = filteredParams.map(([k, v]) => `
      <div class="param-chip">
        <span title="${k}">${k}</span>
        <input type="number" step="${paramStep(v)}" data-param="${k}" value="${v}" />
      </div>
    `).join("");
  }
}

function gatherParams() {
  const params = {};
  document.querySelectorAll("[data-param]").forEach(el => {
    const v = parseFloat(el.value);
    if (!Number.isNaN(v)) params[el.dataset.param] = v;
  });
  return params;
}

function formatParams(params) {
  if (!params || typeof params !== "object") return "";
  return Object.entries(params)
    .map(([k, v]) => `${k}=${typeof v === "number" ? Number(v.toPrecision(4)) : v}`)
    .join(" · ");
}

async function runBacktest() {
  if (running) return;
  running = true;
  const btn = document.getElementById("run-btn");
  btn.disabled = true;
  btn.textContent = "Running…";

  const riskTypeEl = document.getElementById("risk-type-select");
  const riskValueEl = document.getElementById("risk-value");
  const riskType = riskTypeEl ? riskTypeEl.value : "percent_balance";
  const riskVal = riskValueEl && riskValueEl.value !== "" ? parseFloat(riskValueEl.value) : null;

  const body = {
    strategy_id: selectedId,
    asset: document.getElementById("asset-select").value,
    initial_capital: parseFloat(document.getElementById("initial-capital").value),
    risk_type: riskType,
    risk_value: Number.isFinite(riskVal) ? riskVal : null,
    params: gatherParams(),
    use_cache: true,
  };
  if (allHistory) body.all_history = true;
  else {
    body.start_ts = utcInputToUnix(document.getElementById("start-date").value);
    body.end_ts = utcInputToUnix(document.getElementById("end-date").value);
    if (!Number.isFinite(body.start_ts) || !Number.isFinite(body.end_ts)) {
      alert("Choose both start and end times.");
      running = false;
      btn.disabled = false;
      btn.textContent = "▶ Run Backtest";
      return;
    }
  }

  try {
    const res = await fetch("/api/run/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json();
      alert(err.detail || "Backtest failed");
      return;
    }
    const started = await res.json();
    beginLiveRun(body);
    const source = new EventSource(`/api/run/${encodeURIComponent(started.job_id)}/events`);
    source.onmessage = event => {
      const update = JSON.parse(event.data);
      if (update.type === "progress" || update.type === "status") {
        renderLiveProgress(update);
        return;
      }
      source.close();
      if (update.type === "complete") {
        lastResult = update.result;
        renderResult(update.result);
      } else {
        alert(update.message || "Backtest failed");
      }
      finishLiveRun();
    };
    source.onerror = () => {
      source.close();
      if (running) alert("Lost the live backtest connection.");
      finishLiveRun();
    };
    return;
  } catch (e) {
    alert("Backtest error: " + e.message);
    finishLiveRun();
  } finally {
    if (!running) return;
    // The live event stream owns completion and re-enables the button.
  }
}

function finishLiveRun() {
  running = false;
  const btn = document.getElementById("run-btn");
  btn.disabled = false;
  btn.textContent = "▶ Run Backtest";
}

function beginLiveRun(body) {
  const initial = Number(body.initial_capital) || 0;
  liveRun = { initial_capital: initial, equity_curve: [{ idx: 0, equity: initial }], trades: [] };
  setText("stat-ending", `$${initial.toFixed(2)}`);
  setText("stat-return", "+0.00%");
  setText("stat-trades", "0 / 0");
  setText("stat-wl", "0 / 0");
  setText("run-meta", "Preparing backtest…");
  renderTradeList([]);
  scheduleLiveDraw();
}

function renderLiveProgress(update) {
  if (!liveRun) return;
  if (update.phase === "loading") {
    const resolved = update.candles_resolved || 0;
    setText("run-meta", `Loading market data: ${update.candles_completed || 0} / ${update.candles_total || 0} checked · ${resolved} resolved`);
    return;
  }
  if (update.phase !== "backtesting") return;
  const point = update.equity_point;
  if (point) liveRun.equity_curve.push(point);
  if (update.trade) {
    liveRun.trades.push(update.trade);
    renderTradeList(liveRun.trades);
  }
  const equity = Number(update.equity || liveRun.initial_capital);
  const pct = liveRun.initial_capital ? ((equity / liveRun.initial_capital) - 1) * 100 : 0;
  const wins = liveRun.trades.filter(t => t.won).length;
  setText("stat-ending", `$${equity.toFixed(2)}`, equity >= liveRun.initial_capital ? "up" : "down");
  setText("stat-return", `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`, pct >= 0 ? "up" : "down");
  setText("stat-trades", `${liveRun.trades.length} / ${update.candles_completed}`);
  setText("stat-wl", `${wins} / ${liveRun.trades.length - wins}`);
  setText("run-meta", `Backtesting ${update.candles_completed} / ${update.candles_total} candles · ${liveRun.trades.length} trades`);
  scheduleLiveDraw();
}

function scheduleLiveDraw() {
  if (liveDrawQueued || !liveRun) return;
  liveDrawQueued = true;
  requestAnimationFrame(() => {
    liveDrawQueued = false;
    if (liveRun) drawEquity(liveRun.equity_curve, liveRun.initial_capital);
  });
}

function renderTradeList(trades) {
  const body = document.getElementById("trade-body");
  setText("trade-count", `${trades.length} trades`);
  if (!trades.length) {
    body.innerHTML = '<div class="placeholder">No trades triggered yet</div>';
    return;
  }
  body.innerHTML = trades.slice().reverse().map((t, idx) => {
    const originalIdx = trades.length - 1 - idx;
    const expWin = t.entry_price > 0 ? t.stake * (1 - t.entry_price) / t.entry_price : 0;
    const pnlTitle = t.won
      ? `Win @ ${t.entry_price}: stake/price shares settle at $1 → +$${expWin.toFixed(2)}`
      : `Loss @ ${t.entry_price}: stake lost → -$${Number(t.stake).toFixed(2)}`;
    const simProbVal = t.sim_entry_prob != null ? `${(t.sim_entry_prob * 100).toFixed(1)}%` : "—";
    return `
      <div class="trade-row ${t.won ? "win" : "loss"}" title="${escapeAttr(t.reason || "")}">
        <span title="${escapeAttr(t.candle_title)}">${shortTitle(t.candle_title)}</span>
        <span class="side ${t.side}">${t.side.toUpperCase()}</span>
        <span title="Limit fill price">${Number(t.entry_price).toFixed(2)}</span>
        <span class="sim-prob-cell"><span class="sim-prob-badge" data-trade-idx="${originalIdx}" title="Hover to view SIM probability trajectory during trade">${simProbVal}</span></span>
        <span class="size ${t.size_label || ""}" title="${t.risk_pct != null ? t.risk_pct + "% of initial" : ""}">${formatSize(t)}</span>
        <span>$${Number(t.stake).toFixed(2)}</span>
        <span>${t.won ? "WIN" : "LOSS"}</span>
        <span class="pnl" title="${pnlTitle}">${t.pnl >= 0 ? "+" : ""}$${Number(t.pnl).toFixed(2)}</span>
        <span>$${Number(t.equity_after).toFixed(2)}</span>
      </div>`;
  }).join("");

  bindSimTooltipEvents(trades);
}

function renderResult(r) {
  const retCls = r.total_return_pct >= 0 ? "up" : "down";
  setText("stat-ending", `$${r.ending_capital.toFixed(2)}`, retCls);
  setText("stat-return", `${r.total_return_pct >= 0 ? "+" : ""}${r.total_return_pct.toFixed(2)}%`, retCls);
  setText("stat-winrate", `${r.win_rate.toFixed(1)}%`);
  setText("stat-trades", `${r.candles_traded} / ${r.candles_loaded}`);
  setText("stat-wl", `${r.wins} / ${r.losses}`);
  setText("stat-dd", `-${r.max_drawdown_pct.toFixed(2)}%`, "down");
  setText("stat-pf", r.profit_factor >= 999 ? "∞" : r.profit_factor.toFixed(2));
  setText("stat-avg", `+$${r.avg_win.toFixed(2)} / -$${Math.abs(r.avg_loss).toFixed(2)}`);
  const paramStr = formatParams(r.params);
  const dataWindow = formatDataWindow(r.data_start_ts, r.data_end_ts);
  setText(
    "run-meta",
    `${dataWindow} · ${r.candles_loaded}${r.candles_requested != null ? ` / ${r.candles_requested}` : ""} candles`
      + (r.candles_missing ? ` (${r.candles_missing} unavailable)` : "")
      + ` · ${r.strategy_name} · ${r.elapsed_ms.toFixed(0)} ms`
      + (paramStr ? ` · ${paramStr}` : "")
  );
  renderTradeList(r.trades);

  requestAnimationFrame(() => drawEquity(r.equity_curve, r.initial_capital));
}

function formatSize(t) {
  if (t.size_label && t.size_label.startsWith("$")) return t.size_label;
  if (t.size_label) return `${t.size_label} · ${t.risk_pct}%`;
  if (t.risk_pct != null) return `${t.risk_pct}%`;
  return "—";
}

function formatDataWindow(startTs, endTs) {
  if (!Number.isFinite(startTs) || !Number.isFinite(endTs)) return "Data window unavailable";
  const options = {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    hour12: false,
  };
  const start = new Date(startTs * 1000).toLocaleString([], options);
  const end = new Date(endTs * 1000).toLocaleString([], options);
  return `${start} – ${end}`;
}

function shortTitle(title) {
  const m = title.match(/(\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M\s*ET)/i);
  return m ? m[1] : title.slice(0, 28);
}

function escapeAttr(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function setText(id, val, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className = "val" + (cls ? " " + cls : "");
}

/** Normalize legacy {t: unix} and new {idx: n} equity points. */
function normalizeCurve(curve) {
  if (!curve.length) return [];
  const hasIdx = curve[0].idx != null;
  if (hasIdx) {
    return curve.map(p => ({ x: Number(p.idx), equity: Number(p.equity) }));
  }
  // Legacy: first point t=0, rest unix — remap to 0..n-1
  return curve.map((p, i) => ({ x: i, equity: Number(p.equity) }));
}

let lastEquityCurve = null;
let lastEquityInitial = null;
let activeEquityHoverIdx = null;

function bindEquityChartEvents() {
  const canvas = document.getElementById("equity-chart");
  if (!canvas || canvas.dataset.hoverBound) return;
  canvas.dataset.hoverBound = "true";

  canvas.addEventListener("mousemove", (e) => {
    if (!lastEquityCurve || !lastEquityCurve.length) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const points = normalizeCurve(lastEquityCurve);
    if (!points.length) return;

    const pad = { l: 62, r: 20 };
    const plotW = rect.width - pad.l - pad.r;
    if (plotW <= 0) return;

    const relX = Math.max(0, Math.min(plotW, x - pad.l));
    const ratio = relX / plotW;
    const idx = Math.min(points.length - 1, Math.max(0, Math.round(ratio * (points.length - 1))));

    if (activeEquityHoverIdx !== idx) {
      activeEquityHoverIdx = idx;
      drawEquity(lastEquityCurve, lastEquityInitial);
    }
  });

  canvas.addEventListener("mouseleave", () => {
    if (activeEquityHoverIdx != null) {
      activeEquityHoverIdx = null;
      if (lastEquityCurve) drawEquity(lastEquityCurve, lastEquityInitial);
    }
    const tooltip = document.getElementById("equity-tooltip");
    if (tooltip) tooltip.classList.add("hidden");
  });
}

function drawEquity(curve, initial) {
  const canvas = document.getElementById("equity-chart");
  if (!canvas) return;

  const parent = canvas.parentElement;
  if (!parent || parent.clientWidth < 10) return;

  if (!curve || !curve.length) return;

  lastEquityCurve = curve;
  lastEquityInitial = initial;
  bindEquityChartEvents();

  const dpr = window.devicePixelRatio || 1;
  const W = parent.clientWidth;
  const H = parent.clientHeight;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const points = normalizeCurve(curve);
  const pad = { l: 62, r: 20, t: 20, b: 34 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const vals = points.map(p => p.equity);
  const rawMin = Math.min(...vals, initial);
  const rawMax = Math.max(...vals, initial);
  const span = rawMax - rawMin || 1;
  const yPad = Math.max(span * 0.12, 3);
  const yMin = rawMin - yPad;
  const yMax = rawMax + yPad;

  const xMin = points[0].x;
  const xMax = points[points.length - 1].x || xMin + 1;
  const xSpan = xMax - xMin || 1;

  const xS = x => pad.l + ((x - xMin) / xSpan) * plotW;
  const yS = v => pad.t + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  // Background
  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  // Grid + Y labels
  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono, monospace";
  const yTicks = 5;
  for (let i = 0; i <= yTicks; i++) {
    const y = pad.t + (plotH / yTicks) * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    const v = yMax - ((yMax - yMin) / yTicks) * i;
    ctx.fillText("$" + v.toFixed(2), pad.l - 8, y + 3);
  }

  // X labels (candle index)
  ctx.textAlign = "center";
  ctx.fillStyle = "#5a6d8a";
  const xTicks = Math.min(5, Math.max(1, points.length - 1));
  for (let i = 0; i <= xTicks; i++) {
    const xv = xMin + (xSpan / xTicks) * i;
    const px = xS(xv);
    ctx.fillText(String(Math.round(xv)), px, H - 10);
    ctx.beginPath();
    ctx.moveTo(px, pad.t);
    ctx.lineTo(px, pad.t + plotH);
    ctx.strokeStyle = "rgba(26,36,56,0.4)";
    ctx.stroke();
    ctx.strokeStyle = "#1a2438";
  }

  // Starting capital benchmark line
  const y0 = yS(initial);
  ctx.strokeStyle = "rgba(240,185,11,0.45)";
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, y0);
  ctx.lineTo(W - pad.r, y0);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(240,185,11,0.85)";
  ctx.textAlign = "left";
  ctx.fillText(`Start $${initial.toFixed(0)}`, pad.l + 6, y0 - 6);

  const last = points[points.length - 1].equity;
  const color = last >= initial ? "#0ecb81" : "#f6465d";

  // Gradient area fill
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  if (last >= initial) {
    grad.addColorStop(0, "rgba(14, 203, 129, 0.2)");
    grad.addColorStop(1, "rgba(14, 203, 129, 0.01)");
  } else {
    grad.addColorStop(0, "rgba(246, 70, 93, 0.2)");
    grad.addColorStop(1, "rgba(246, 70, 93, 0.01)");
  }
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.moveTo(xS(points[0].x), yS(points[0].equity));
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const cur = points[i];
    ctx.lineTo(xS(cur.x), yS(prev.equity));
    ctx.lineTo(xS(cur.x), yS(cur.equity));
  }
  ctx.lineTo(xS(points[points.length - 1].x), yS(yMin));
  ctx.lineTo(xS(points[0].x), yS(yMin));
  ctx.closePath();
  ctx.fill();

  // Step line
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(xS(points[0].x), yS(points[0].equity));
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const cur = points[i];
    ctx.lineTo(xS(cur.x), yS(prev.equity));
    ctx.lineTo(xS(cur.x), yS(cur.equity));
  }
  ctx.stroke();

  // Mark wins/losses on steps
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const cur = points[i];
    if (Math.abs(cur.equity - prev.equity) < 0.001) continue;
    const dotColor = cur.equity > prev.equity ? "#0ecb81" : "#f6465d";
    ctx.fillStyle = dotColor;
    ctx.beginPath();
    ctx.arc(xS(cur.x), yS(cur.equity), 3, 0, Math.PI * 2);
    ctx.fill();
  }

  // End marker
  const lp = points[points.length - 1];
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(xS(lp.x), yS(lp.equity), 4.5, 0, Math.PI * 2);
  ctx.fill();

  // Interactive Hover Crosshair
  if (activeEquityHoverIdx != null && activeEquityHoverIdx >= 0 && activeEquityHoverIdx < points.length) {
    const targetPt = points[activeEquityHoverIdx];
    const hx = xS(targetPt.x);
    const hy = yS(targetPt.equity);

    ctx.strokeStyle = "rgba(255, 255, 255, 0.4)";
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 1;

    ctx.beginPath();
    ctx.moveTo(hx, pad.t);
    ctx.lineTo(hx, pad.t + plotH);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(pad.l, hy);
    ctx.lineTo(W - pad.r, hy);
    ctx.stroke();

    ctx.setLineDash([]);

    ctx.fillStyle = "#3861fb";
    ctx.beginPath();
    ctx.arc(hx, hy, 5.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    const tooltip = document.getElementById("equity-tooltip");
    if (tooltip) {
      const pct = initial > 0 ? ((targetPt.equity / initial) - 1) * 100 : 0;
      const prevEq = activeEquityHoverIdx > 0 ? points[activeEquityHoverIdx - 1].equity : initial;
      const diff = targetPt.equity - prevEq;
      const diffStr = Math.abs(diff) > 0.001 ? ` · Trade: ${diff >= 0 ? "+" : ""}$${diff.toFixed(2)}` : "";

      tooltip.innerHTML = `
        <div style="font-weight:700; color:#e2e8f0;">Candle #${targetPt.x}</div>
        <div style="color:${targetPt.equity >= initial ? '#0ecb81' : '#f6465d'}; font-weight:700;">
          $${targetPt.equity.toFixed(2)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)${diffStr}
        </div>
      `;

      let leftPx = hx + 12;
      if (leftPx + 160 > W) leftPx = hx - 165;
      let topPx = hy - 45;
      if (topPx < 10) topPx = hy + 12;

      tooltip.style.left = `${leftPx}px`;
      tooltip.style.top = `${topPx}px`;
      tooltip.classList.remove("hidden");
    }
  } else {
    const tooltip = document.getElementById("equity-tooltip");
    if (tooltip) tooltip.classList.add("hidden");
  }
}

function bindSimTooltipEvents(trades) {
  const container = document.getElementById("sim-tooltip-card");
  if (!container) return;

  document.querySelectorAll(".sim-prob-badge").forEach(el => {
    const idx = parseInt(el.dataset.tradeIdx, 10);
    const trade = trades[idx];
    if (!trade) return;

    el.addEventListener("mouseenter", (e) => {
      showSimTooltip(trade, e);
    });

    el.addEventListener("mousemove", (e) => {
      moveSimTooltip(e);
    });

    el.addEventListener("mouseleave", () => {
      hideSimTooltip();
    });
  });
}

function showSimTooltip(trade, e) {
  const container = document.getElementById("sim-tooltip-card");
  if (!container) return;

  const entryProbStr = trade.sim_entry_prob != null ? `${(trade.sim_entry_prob * 100).toFixed(1)}%` : "—";
  setText("sim-tt-entry-prob", entryProbStr);
  setText("sim-tt-title", `${(trade.side || "").toUpperCase()} @ ${Number(trade.entry_price || 0).toFixed(2)}`);
  setText("sim-tt-sub", shortTitle(trade.candle_title || "Trade duration"));

  const durSec = trade.sim_lifetime_probs ? trade.sim_lifetime_probs.length - 1 : 0;
  const pnlStr = `${trade.pnl >= 0 ? "+" : ""}$${Number(trade.pnl || 0).toFixed(2)}`;
  setText("sim-tt-meta", `${durSec}s duration · ${trade.won ? "WIN" : "LOSS"} (${pnlStr})`);

  const canvas = document.getElementById("sim-tooltip-canvas");
  renderSimTooltipChart(canvas, trade.sim_lifetime_probs || [], trade);

  container.classList.remove("hidden");
  moveSimTooltip(e);
}

function moveSimTooltip(e) {
  const container = document.getElementById("sim-tooltip-card");
  if (!container || container.classList.contains("hidden")) return;

  let x = e.clientX + 16;
  let y = e.clientY - 40;

  const rect = container.getBoundingClientRect();
  if (x + rect.width > window.innerWidth - 10) {
    x = e.clientX - rect.width - 16;
  }
  if (y + rect.height > window.innerHeight - 10) {
    y = window.innerHeight - rect.height - 10;
  }
  if (y < 10) y = 10;

  container.style.left = `${x}px`;
  container.style.top = `${y}px`;
}

function hideSimTooltip() {
  const container = document.getElementById("sim-tooltip-card");
  if (container) container.classList.add("hidden");
}

function renderSimTooltipChart(canvas, probs, trade) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const W = 280;
  const H = 120;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  canvas.style.width = `${W}px`;
  canvas.style.height = `${H}px`;

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  if (!probs || !probs.length) {
    ctx.fillStyle = "#5a6d8a";
    ctx.font = "11px JetBrains Mono, monospace";
    ctx.textAlign = "center";
    ctx.fillText("No probability data", W / 2, H / 2);
    return;
  }

  const pad = { l: 36, r: 12, t: 12, b: 22 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const yMin = 0.0;
  const yMax = 1.0;
  const n = probs.length;

  const xS = idx => pad.l + (n > 1 ? (idx / (n - 1)) * plotW : plotW / 2);
  const yS = p => pad.t + plotH - ((p - yMin) / (yMax - yMin)) * plotH;

  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "9px JetBrains Mono, monospace";

  [0.0, 0.5, 1.0].forEach(val => {
    const y = yS(val);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    ctx.fillText(`${(val * 100).toFixed(0)}%`, pad.l - 4, y + 3);
  });

  const y50 = yS(0.5);
  ctx.strokeStyle = "rgba(0, 242, 254, 0.3)";
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(pad.l, y50);
  ctx.lineTo(W - pad.r, y50);
  ctx.stroke();
  ctx.setLineDash([]);

  const gradArea = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  gradArea.addColorStop(0, "rgba(0, 242, 254, 0.25)");
  gradArea.addColorStop(1, "rgba(0, 242, 254, 0.01)");
  ctx.fillStyle = gradArea;
  ctx.beginPath();
  probs.forEach((pt, i) => {
    const x = xS(i);
    const y = yS(pt.prob);
    if (i === 0) {
      ctx.moveTo(x, pad.t + plotH);
      ctx.lineTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.lineTo(xS(n - 1), pad.t + plotH);
  ctx.closePath();
  ctx.fill();

  const lineGrad = ctx.createLinearGradient(pad.l, 0, W - pad.r, 0);
  lineGrad.addColorStop(0, "#00f2fe");
  lineGrad.addColorStop(1, "#4facfe");

  ctx.strokeStyle = lineGrad;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  probs.forEach((pt, i) => {
    const x = xS(i);
    const y = yS(pt.prob);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const entryY = yS(probs[0].prob);
  ctx.fillStyle = "#f0b90b";
  ctx.beginPath();
  ctx.arc(xS(0), entryY, 3.5, 0, Math.PI * 2);
  ctx.fill();

  const exitY = yS(probs[n - 1].prob);
  ctx.fillStyle = trade.won ? "#0ecb81" : "#f6465d";
  ctx.beginPath();
  ctx.arc(xS(n - 1), exitY, 3.5, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#5a6d8a";
  ctx.font = "9px JetBrains Mono, monospace";
  ctx.textAlign = "left";
  ctx.fillText("0s (Entry)", pad.l, H - 4);
  ctx.textAlign = "right";
  ctx.fillText(`${n - 1}s (Exit)`, W - pad.r, H - 4);
}

document.addEventListener("DOMContentLoaded", init);

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (lastResult) drawEquity(lastResult.equity_curve, lastResult.initial_capital);
  }, 120);
});
