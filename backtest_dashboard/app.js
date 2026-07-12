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
  sel.innerHTML = strategies.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
  sel.addEventListener("change", onStrategyChange);
  document.getElementById("run-btn").addEventListener("click", runBacktest);
  document.querySelectorAll(".period-btn[data-days]").forEach(btn => btn.addEventListener("click", () => setRecentRange(Number(btn.dataset.days))));
  document.getElementById("all-history-btn").addEventListener("click", enableAllHistory);
  document.getElementById("start-date").addEventListener("change", () => { allHistory = false; updateCandleEstimate(); });
  document.getElementById("end-date").addEventListener("change", () => { allHistory = false; updateCandleEstimate(); });
  document.getElementById("asset-select").addEventListener("change", updateCandleEstimate);
  setRecentRange(7);
  if (strategies.length) {
    const preferred = strategies.find(s => s.id === "signull_1_0");
    selectedId = preferred ? preferred.id : strategies[0].id;
    sel.value = selectedId;
    onStrategyChange();
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

function setRecentRange(days) {
  allHistory = false;
  const end = new Date();
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - days);
  document.getElementById("start-date").value = utcDateTimeInput(start);
  document.getElementById("end-date").value = utcDateTimeInput(end);
  document.getElementById("date-range-hint").textContent = `Running the last ${days} UTC day${days === 1 ? "" : "s"}, to the minute.`;
  updateCandleEstimate();
}

async function enableAllHistory() {
  allHistory = true;
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
  box.innerHTML = Object.entries(s.default_params || {}).map(([k, v]) => `
    <div class="param-chip">
      <span title="${k}">${k}</span>
      <input type="number" step="${paramStep(v)}" data-param="${k}" value="${v}" />
    </div>
  `).join("");
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

  const body = {
    strategy_id: selectedId,
    asset: document.getElementById("asset-select").value,
    initial_capital: parseFloat(document.getElementById("initial-capital").value),
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
  body.innerHTML = trades.slice().reverse().map(t => {
    const expWin = t.entry_price > 0 ? t.stake * (1 - t.entry_price) / t.entry_price : 0;
    const pnlTitle = t.won
      ? `Win @ ${t.entry_price}: stake/price shares settle at $1 → +$${expWin.toFixed(2)}`
      : `Loss @ ${t.entry_price}: stake lost → -$${Number(t.stake).toFixed(2)}`;
    return `
      <div class="trade-row ${t.won ? "win" : "loss"}" title="${escapeAttr(t.reason || "")}">
        <span title="${escapeAttr(t.candle_title)}">${shortTitle(t.candle_title)}</span>
        <span class="side ${t.side}">${t.side.toUpperCase()}</span>
        <span title="Limit fill price">${Number(t.entry_price).toFixed(2)}</span>
        <span class="size ${t.size_label || ""}" title="${t.risk_pct != null ? t.risk_pct + "% of initial" : ""}">${formatSize(t)}</span>
        <span>$${Number(t.stake).toFixed(2)}</span>
        <span>${t.won ? "WIN" : "LOSS"}</span>
        <span class="pnl" title="${pnlTitle}">${t.pnl >= 0 ? "+" : ""}$${Number(t.pnl).toFixed(2)}</span>
        <span>$${Number(t.equity_after).toFixed(2)}</span>
      </div>`;
  }).join("");
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

function drawEquity(curve, initial) {
  const canvas = document.getElementById("equity-chart");
  if (!canvas || !curve.length) return;

  const parent = canvas.parentElement;
  if (!parent || parent.clientWidth < 10) return;

  const dpr = window.devicePixelRatio || 1;
  const W = parent.clientWidth;
  const H = parent.clientHeight;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const points = normalizeCurve(curve);
  const pad = { l: 58, r: 16, t: 18, b: 32 };
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
    ctx.fillText("$" + v.toFixed(2), pad.l - 6, y + 3);
  }

  // X labels (candle index)
  ctx.textAlign = "center";
  ctx.fillStyle = "#5a6d8a";
  const xTicks = 4;
  for (let i = 0; i <= xTicks; i++) {
    const xv = xMin + (xSpan / xTicks) * i;
    const px = xS(xv);
    ctx.fillText(String(Math.round(xv)), px, H - 10);
    ctx.beginPath();
    ctx.moveTo(px, pad.t);
    ctx.lineTo(px, pad.t + plotH);
    ctx.strokeStyle = "rgba(26,36,56,0.5)";
    ctx.stroke();
    ctx.strokeStyle = "#1a2438";
  }
  // Starting capital reference
  const y0 = yS(initial);
  ctx.strokeStyle = "rgba(240,185,11,0.45)";
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, y0);
  ctx.lineTo(W - pad.r, y0);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(240,185,11,0.8)";
  ctx.textAlign = "left";
  ctx.fillText(`Start $${initial.toFixed(0)}`, pad.l + 4, y0 - 5);

  const last = points[points.length - 1].equity;
  const color = last >= initial ? "#0ecb81" : "#f6465d";

  // Step chart fill (equity changes at candle close)
  ctx.fillStyle = last >= initial ? "rgba(14,203,129,0.12)" : "rgba(246,70,93,0.12)";
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
}

document.addEventListener("DOMContentLoaded", init);

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (lastResult) drawEquity(lastResult.equity_curve, lastResult.initial_capital);
  }, 120);
});
