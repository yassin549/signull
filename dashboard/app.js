/* Signull dashboard */

let ws, reconnectTimer, pollTimer, lastVersion = -1;
let history = [];
let btcHistory = [];
let priceToBeat = null;
let activeBook = "up";
let cachedBooks = {};
let lastTradeId = null;
let isLive = false;
let lastUpdateAt = 0;
let obRenderPending = false;

const CANDLE_SEC = 300;
const RING_LEN = 97.4;
const POLL_MS = 1000;
const smooth = { up: null, down: null, btcDelta: null };
let needsRedraw = true;
let lastServerTs = null;
let countdownBase = null;
let activeCandleSlug = null;
let feedReconnecting = false;

function safe(fn) {
  return (...args) => {
    try { fn(...args); }
    catch (e) { console.error(fn.name || "handler", e); }
  };
}

function init() {
  initTabs("book-tabs", (tab) => {
    activeBook = tab;
    renderActiveBook();
    needsRedraw = true;
  });
  initTabs("info-tabs", (tab) => {
    document.getElementById("info-wallet").classList.toggle("hidden", tab !== "wallet");
    document.getElementById("info-log").classList.toggle("hidden", tab !== "log");
  });
  fetch("/api/config")
    .then(r => r.json())
    .then(c => {
      const el = document.getElementById("asset-badge");
      if (el) el.textContent = (c.asset || "btc").toUpperCase();
    })
    .catch(() => {});
  pollStatus();
  connect();
  pollTimer = setInterval(pollStatus, POLL_MS);
  setInterval(tickCountdown, 250);
  setInterval(tickSyncAge, 1000);
  requestAnimationFrame(renderLoop);
}

document.addEventListener("DOMContentLoaded", init);

function initTabs(id, onSwitch) {
  const el = document.getElementById(id);
  if (!el) return;
  el.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      el.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      onSwitch(btn.dataset.tab);
    });
  });
}

function connect() {
  clearTimeout(reconnectTimer);
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    ws.onclose = null;
    ws.close();
  }

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    isLive = true;
    setSyncStatus(true);
    pollStatus();
  };
  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      applySnapshot(d, false);
    } catch (err) {
      console.error("ws parse", err);
    }
  };
  ws.onerror = () => {
    isLive = false;
    setSyncStatus(false);
  };
  ws.onclose = () => {
    isLive = false;
    setSyncStatus(false);
    reconnectTimer = setTimeout(connect, 1500);
  };
}

async function pollStatus() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const d = await r.json();
    applySnapshot(d, true);
  } catch (_) {}
}

function applySnapshot(d, isFull) {
  if (!d) return;

  lastVersion = d.version ?? lastVersion;
  lastUpdateAt = Date.now();
  setSyncStatus(true);

  onUpdate(d);

  const incoming = d.price_history || [];
  if (isFull) {
    history = incoming.slice();
  } else if (incoming.length) {
    mergeHistory(incoming);
  }

  const btcIncoming = d.btc_history || [];
  if (isFull) {
    btcHistory = btcIncoming.slice();
  } else if (btcIncoming.length) {
    mergeBtcHistory(btcIncoming);
  }

  lastServerTs = history.length
    ? history[history.length - 1].t
    : (d.feed?.last_update_at || Date.now() / 1000);
  needsRedraw = true;
}

function mergeHistory(incoming) {
  if (!incoming.length) return;
  if (!history.length) {
    history = incoming.slice();
    return;
  }
  const lastT = history[history.length - 1].t;
  for (const p of incoming) {
    if (p.t > lastT) history.push(p);
    else if (p.t === lastT) history[history.length - 1] = p;
  }
  if (history.length > 6000) history = history.slice(-6000);
}

function mergeBtcHistory(incoming) {
  if (!incoming.length) return;
  if (!btcHistory.length) {
    btcHistory = incoming.slice();
    return;
  }
  const lastT = btcHistory[btcHistory.length - 1].t;
  for (const p of incoming) {
    if (p.t > lastT) btcHistory.push(p);
    else if (p.t === lastT) btcHistory[btcHistory.length - 1] = p;
  }
  if (btcHistory.length > 6000) btcHistory = btcHistory.slice(-6000);
}

const onUpdate = safe(function onUpdate(d) {
  detectCandleChange(d.market);
  updateTopbar(d);
  updateHero(d);
  updateWallet(d);
  updateOrderbooks(d.orderbooks || {});
  updateTape(d.trades || []);
  updateLog(d.activity || []);
  updateBtcPanel(d.btc);
  updateBotButtons(d.running);
});

function detectCandleChange(market) {
  const slug = market?.slug;
  if (!slug || slug === activeCandleSlug) return;
  const isRollover = activeCandleSlug !== null;
  activeCandleSlug = slug;
  if (isRollover) resetCandleUI();
  countdownBase = { market };
}

function resetCandleUI() {
  history = [];
  btcHistory = [];
  priceToBeat = null;
  smooth.up = smooth.down = smooth.btcDelta = null;
  cachedBooks = {};
  lastTradeId = null;
  needsRedraw = true;
  setText("price-up", "—");
  setText("price-down", "—");
  setText("pct-up", "—");
  setText("pct-down", "—");
  setText("prob-delta", "—");
  setText("hero-btc-beat", "Beat —");
  setText("hero-btc-delta", "Δ —");
  const probEl = document.getElementById("prob-up");
  if (probEl) probEl.style.width = "50%";
  setText("signal-side", "HOLD");
  setClass("signal-side", "signal-badge hold");
  setText("signal-reason", "New candle — warming up");
}

function marketSecsToClose(market) {
  if (!market) return null;
  if (market.end_date) {
    const end = new Date(market.end_date).getTime();
    if (!isNaN(end)) return Math.max(0, (end - Date.now()) / 1000);
  }
  return market.seconds_to_close != null ? Math.max(0, market.seconds_to_close) : null;
}

function updateTopbar(d) {
  const feed = d.feed || {};
  feedReconnecting = !!feed.reconnecting;
  let feedCls = "status-item";
  if (feed.reconnecting) feedCls += " warn";
  else if (feed.connected) feedCls += " live";
  else feedCls += " off";
  setClass("feed-status", feedCls);

  let feedLabel = feed.updates_per_sec != null ? Math.round(feed.updates_per_sec) : "—";
  if (feed.error) feedLabel = "err";
  else if (feed.reconnecting) feedLabel = "…";
  setText("feed-rate", feedLabel);
  const feedEl = document.getElementById("feed-status");
  if (feedEl) feedEl.title = feed.error || (feed.reconnecting ? "Reconnecting…" : "Feed");

  const isLiveMode = d.mode === "live";
  setClass("mode-status", "status-item" + (isLiveMode ? " live" : " warn"));
  setText("mode-text", isLiveMode ? "Live" : "Paper");

  setClass("bot-status", "status-item" + (d.running ? " live" : " off"));
  setText("bot-text", d.running ? "On" : "Off");

  if (d.market?.title) setText("market-short", shortenMarket(d.market.title));
}

function setSyncStatus(ok) {
  setClass("sync-status", "status-item" + (ok ? " live" : " off"));
  tickSyncAge();
}

function tickSyncAge() {
  if (!lastUpdateAt) {
    setText("sync-text", "—");
    return;
  }
  const sec = Math.floor((Date.now() - lastUpdateAt) / 1000);
  setText("sync-text", sec < 2 ? "live" : sec + "s");
}

function shortenMarket(title) {
  const m = title.match(/(\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M\s*ET)/i);
  return m ? m[1].replace(/\s+/g, " ") : title.slice(0, 36);
}

function updateHero(d) {
  const prices = d.prices || {};
  const up = prices.up;
  const down = prices.down;

  if (up != null) {
    smooth.up = lerp(smooth.up, up, 0.35);
    setText("price-up", fmt(up));
    setText("pct-up", pct(up));
    flashEl("price-up");
  }
  if (down != null) {
    smooth.down = lerp(smooth.down, down, 0.35);
    setText("price-down", fmt(down));
    setText("pct-down", pct(down));
    flashEl("price-down");
  }

  if (up != null && down != null) {
    const total = up + down;
    const upPct = total > 0 ? (up / total) * 100 : 50;
    const probEl = document.getElementById("prob-up");
    if (probEl) probEl.style.width = upPct + "%";
    const delta = up - down;
    setText("prob-delta", (delta >= 0 ? "▲ " : "▼ ") + Math.abs(delta * 100).toFixed(1) + "¢");
    needsRedraw = true;
  }

  if (d.market) {
    const secs = Math.max(0, Math.round(marketSecsToClose(d.market) || 0));
    countdownBase = { market: d.market };
    renderCountdown(secs);
    updateTimerRing(secs, d.market);
  }

  if (d.signal) {
    const side = (d.signal.side || "hold").toLowerCase();
    const badge = document.getElementById("signal-side");
    if (badge) {
      badge.textContent = side.toUpperCase();
      badge.className = "signal-badge " + side;
    }
    setText("signal-reason", d.signal.reason || "");
  }
}

function flashEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function tickCountdown() {
  if (!countdownBase?.market) return;
  const secs = Math.max(0, Math.round(marketSecsToClose(countdownBase.market) || 0));
  renderCountdown(secs);
  updateTimerRing(secs, countdownBase.market);
  // Poll faster in the final seconds so the new candle slug arrives quickly.
  if (secs <= 10 && !tickCountdown._fast) {
    tickCountdown._fast = true;
    clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, 400);
  } else if (secs > 15 && tickCountdown._fast) {
    tickCountdown._fast = false;
    clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, POLL_MS);
  }
}

function renderCountdown(secs) {
  const el = document.getElementById("countdown");
  const label = document.querySelector(".timer-label");
  const rolling = secs <= 0 && feedReconnecting;

  if (el) {
    el.textContent = rolling ? "0:00" : `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    el.classList.toggle("rolling", rolling);
  }
  if (label) label.textContent = rolling ? "rolling over" : "to close";
}

function updateTimerRing(secs, market) {
  const ring = document.getElementById("ring-fg");
  if (!ring) return;
  const total = market?.candle_duration_sec || CANDLE_SEC;
  const rolling = secs <= 0 && feedReconnecting;
  const elapsed = rolling ? 1 : Math.max(0, Math.min(1, 1 - secs / total));
  ring.style.strokeDashoffset = String(RING_LEN * (1 - elapsed));
  ring.classList.toggle("urgent", !rolling && secs < 30);
  ring.classList.toggle("rolling", rolling);
}

function updateWallet(d) {
  const a = d.account || {};
  const connected = !!a.connected;

  setText("wallet-balance", a.balance_usdc != null ? `$${Number(a.balance_usdc).toFixed(2)}` : "—");
  setText("wallet-funder", fmtAddr(a.funder_address));
  setText("wallet-signer", fmtAddr(a.signer_address));
  setText("wallet-type", a.signature_label || "—");
  setText("wallet-mode", (a.mode || d.mode || "paper").toUpperCase());

  const statusEl = document.getElementById("wallet-status");
  if (statusEl) {
    statusEl.textContent = connected ? "Connected" : "Not connected";
    statusEl.className = "wallet-pill " + (connected ? "ok" : "off");
  }

  const tipsEl = document.getElementById("wallet-tips");
  if (tipsEl) {
    if (a.tips?.length) {
      tipsEl.classList.remove("hidden");
      tipsEl.innerHTML = a.tips.map(t => `<div class="wallet-tip">${esc(t)}</div>`).join("");
    } else {
      tipsEl.classList.add("hidden");
      tipsEl.innerHTML = "";
    }
  }
}

function updateOrderbooks(books) {
  cachedBooks = books;
  if (!obRenderPending) {
    obRenderPending = true;
    requestAnimationFrame(() => {
      obRenderPending = false;
      renderActiveBook();
      needsRedraw = true;
    });
  }
}

function renderActiveBook() {
  const book = cachedBooks[activeBook];
  const body = document.getElementById("book-body");
  if (!body) return;

  const hasLevels = book?.asks?.length || book?.bids?.length;

  setText("book-best-bid", book?.best_bid != null ? fmt(book.best_bid) : "—");
  setText("book-best-ask", book?.best_ask != null ? fmt(book.best_ask) : "—");
  setText("book-mid", book?.mid != null ? fmt(book.mid) : "—");
  setText("book-spread", book?.spread != null ? fmt(book.spread) : "—");

  if (!book || !hasLevels) {
    if (book?.best_bid != null && book?.best_ask != null) {
      body.innerHTML = '<div class="placeholder">Depth loading…</div>';
    } else {
      body.innerHTML = '<div class="placeholder">Waiting for book…</div>';
    }
    return;
  }

  const asks = [...(book.asks || [])].sort((a, b) => b.price - a.price).slice(0, 10);
  const bids = [...(book.bids || [])].sort((a, b) => b.price - a.price).slice(0, 10);
  const maxSize = Math.max(...asks.map(l => l.size), ...bids.map(l => l.size), 1);

  let askTotal = 0;
  let bidTotal = 0;

  let html = '<div class="book-section asks-section">';
  asks.forEach(l => {
    askTotal += l.size;
    html += bookRow(l, maxSize, "ask", askTotal);
  });
  html += '</div>';

  html += `<div class="book-spread-row">
    <span class="spread-lbl">Spread</span>
    <span class="spread-val">${fmt(book.spread)}</span>
  </div>`;

  html += '<div class="book-section bids-section">';
  bids.forEach(l => {
    bidTotal += l.size;
    html += bookRow(l, maxSize, "bid", bidTotal);
  });
  html += '</div>';

  body.innerHTML = html;
}

function bookRow(level, maxSize, cls, cumulative) {
  const pct = Math.min(100, (level.size / maxSize) * 100);
  const sz = level.size >= 100 ? level.size.toFixed(0) : level.size.toFixed(1);
  const cum = cumulative >= 100 ? cumulative.toFixed(0) : cumulative.toFixed(1);
  return `<div class="book-row ${cls}">
    <div class="bg" style="width:${pct}%"></div>
    <span class="px">${fmt(level.price)}</span>
    <span class="sz">${sz}</span>
    <span class="cum">${cum}</span>
  </div>`;
}

function updateTape(trades) {
  const el = document.getElementById("trade-tape");
  if (!el) return;

  setText("tape-count", `${trades?.length || 0} trade${trades?.length === 1 ? "" : "s"}`);

  if (!trades?.length) {
    el.innerHTML = '<div class="placeholder">No trades yet</div>';
    lastTradeId = null;
    return;
  }

  const newest = trades[0];
  const tradeKey = `${newest.t}-${newest.price}-${newest.size}`;
  const isNew = tradeKey !== lastTradeId;
  lastTradeId = tradeKey;

  el.innerHTML = trades.slice(0, 24).map((t, i) => {
    const isBuy = (t.trade_side || "").toLowerCase() === "buy";
    const side = (t.side || "").toLowerCase();
    const time = new Date(t.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const flash = isNew && i === 0 ? " flash" : "";
    return `<div class="tape-row${flash}">
      <span class="tape-time">${time}</span>
      <span class="tape-side ${isBuy ? "buy" : "sell"}">${isBuy ? "BUY" : "SELL"}</span>
      <span class="tape-outcome ${side}">${(t.side || "").toUpperCase()}</span>
      <span class="tape-price">${fmt(t.price)}</span>
      <span class="tape-size">${fmtSize(t.size)}</span>
    </div>`;
  }).join("");
}

function updateLog(entries) {
  const el = document.getElementById("activity-log");
  if (!el) return;
  if (!entries?.length) {
    el.innerHTML = '<div class="placeholder">No activity</div>';
    return;
  }
  el.innerHTML = entries.slice(0, 30).map(e => {
    const lvl = (e.level || "info").toLowerCase();
    return `<div class="activity-item ${lvl}">
      <span class="activity-dot"></span>
      <span class="activity-time">${e.time || ""}</span>
      <span class="activity-msg">${esc(e.message)}</span>
    </div>`;
  }).join("");
}

function updateBtcPanel(btc) {
  if (!btc) return;
  if (btc.price_to_beat != null) priceToBeat = btc.price_to_beat;

  const beatPrefix = btc.beat_estimated ? "Beat ~" : "Beat";
  const beatText = btc.price_to_beat != null ? `${beatPrefix} ${fmtUsd(btc.price_to_beat)}` : "Beat —";

  const livePrice = btc.price ?? btc.chainlink;
  if (livePrice != null) {
    setText("btc-spot", fmtUsd(livePrice));
    setText("hero-btc-price", fmtUsd(livePrice));
    flashEl("hero-btc-price");
  }
  if (btc.price_to_beat != null) {
    setText("btc-beat", beatText);
    setText("hero-btc-beat", beatText);
  }

  const deltaText = btc.delta != null
    ? `${btc.delta >= 0 ? "▲" : "▼"} ${fmtUsd(Math.abs(btc.delta))}${btc.delta_pct != null ? ` (${Math.abs(btc.delta_pct).toFixed(3)}%)` : ""}`
    : "Δ —";
  const deltaCls = btc.delta != null ? (btc.delta >= 0 ? "up" : "down") : "";

  if (btc.delta != null) {
    smooth.btcDelta = lerp(smooth.btcDelta, btc.delta, 0.55);
    setText("btc-chart-meta", fmtDelta(btc.delta));
    const metaEl = document.getElementById("btc-chart-meta");
    if (metaEl) metaEl.className = deltaCls;
  } else {
    setText("btc-chart-meta", "—");
  }

  for (const id of ["btc-delta", "hero-btc-delta"]) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.textContent = deltaText;
    el.className = deltaCls;
  }
  needsRedraw = true;
}

function updateBotButtons(running) {
  const start = document.querySelector(".btn-start");
  const stop = document.querySelector(".btn-stop");
  if (start) {
    start.disabled = running;
    start.classList.toggle("disabled", running);
  }
  if (stop) {
    stop.disabled = !running;
    stop.classList.toggle("disabled", !running);
  }
}

function setupCanvas(canvas) {
  if (!canvas) return null;
  const parent = canvas.parentElement;
  if (!parent) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = parent.clientWidth;
  const h = parent.clientHeight;
  if (w < 10 || h < 10) return null;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function chartWindowSec() {
  if (history.length < 2) return CANDLE_SEC;
  const span = history[history.length - 1].t - history[0].t;
  return Math.min(CANDLE_SEC, Math.max(60, span + 5));
}

function drawPriceChart() {
  const canvas = document.getElementById("price-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;

  if (!history.length) {
    ctx.fillStyle = "#06090f";
    ctx.fillRect(0, 0, W, H);
    setText("chart-meta", "—");
    return true;
  }

  const pad = { l: 44, r: 12, t: 12, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const windowSec = chartWindowSec();
  const now = lastServerTs || history[history.length - 1].t;
  const tMin = now - windowSec;

  let visible = history.filter(p => p.t >= tMin && p.up != null && p.down != null);
  if (visible.length < 2) {
    ctx.fillStyle = "#06090f";
    ctx.fillRect(0, 0, W, H);
    setText("chart-meta", "—");
    return true;
  }
  if (smooth.up != null && smooth.down != null) {
    visible = [...visible, { t: now, up: smooth.up, down: smooth.down }];
  }

  const vals = visible.flatMap(p => [p.up, p.down]);
  let yMin = Math.max(0, Math.min(...vals) - 0.03);
  let yMax = Math.min(1, Math.max(...vals) + 0.03);
  if (yMax - yMin < 0.06) {
    const mid = (yMax + yMin) / 2;
    yMin = Math.max(0, mid - 0.03);
    yMax = Math.min(1, mid + 0.03);
  }

  const xS = t => pad.l + ((t - tMin) / windowSec) * plotW;
  const yS = v => pad.t + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono, monospace";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    ctx.fillText((yMax - ((yMax - yMin) / 4) * i).toFixed(2), pad.l - 4, y + 3);
  }

  if (yMin < 0.5 && yMax > 0.5) {
    const y50 = yS(0.5);
    ctx.strokeStyle = "rgba(110,128,153,0.2)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.l, y50);
    ctx.lineTo(W - pad.r, y50);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.fillStyle = "rgba(240,185,11,0.04)";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    if (i === 0) ctx.moveTo(x, yS(p.up));
    else ctx.lineTo(x, yS(p.up));
  });
  for (let i = visible.length - 1; i >= 0; i--) {
    ctx.lineTo(xS(visible[i].t), yS(visible[i].down));
  }
  ctx.closePath();
  ctx.fill();

  drawLine(ctx, visible, "up", "#0ecb81", xS, yS);
  drawLine(ctx, visible, "down", "#f6465d", xS, yS);

  const last = visible[visible.length - 1];
  const label = windowSec >= CANDLE_SEC - 1 ? "5m" : `${Math.round(windowSec)}s`;
  setText("chart-meta", `Δ ${fmt(last.up - last.down)} · ${label}`);
  return true;
}

function drawLine(ctx, pts, key, color, xS, yS) {
  if (pts.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = xS(p.t), y = yS(p[key]);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  const last = pts[pts.length - 1];
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(xS(last.t), yS(last[key]), 3.5, 0, Math.PI * 2);
  ctx.fill();
}

function btcChartWindowSec() {
  if (btcHistory.length < 2) return CANDLE_SEC;
  const span = btcHistory[btcHistory.length - 1].t - btcHistory[0].t;
  return Math.min(CANDLE_SEC, Math.max(60, span + 5));
}

function btcPointDelta(p) {
  if (p.d != null) return p.d;
  if (priceToBeat != null && p.v != null) return p.v - priceToBeat;
  return null;
}

function drawBtcChart() {
  const canvas = document.getElementById("btc-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;

  if (!btcHistory.length || priceToBeat == null) {
    ctx.fillStyle = "#06090f";
    ctx.fillRect(0, 0, W, H);
    return true;
  }

  const pad = { l: 52, r: 12, t: 12, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const windowSec = btcChartWindowSec();
  const now = btcHistory[btcHistory.length - 1].t;
  const tMin = now - windowSec;

  let visible = btcHistory
    .map(p => ({ t: p.t, d: btcPointDelta(p) }))
    .filter(p => p.t >= tMin && p.d != null);
  if (visible.length < 2) {
    ctx.fillStyle = "#06090f";
    ctx.fillRect(0, 0, W, H);
    return true;
  }
  if (smooth.btcDelta != null) {
    const tailT = btcHistory[btcHistory.length - 1]?.t || now;
    visible = [...visible, { t: Math.max(tailT, now - 0.05), d: smooth.btcDelta }];
  }

  const vals = visible.map(p => p.d);
  const absMax = Math.max(...vals.map(Math.abs), 5);
  const padY = Math.max(2, absMax * 0.15);
  let yMin = Math.min(-padY, Math.min(...vals) - padY);
  let yMax = Math.max(padY, Math.max(...vals) + padY);
  if (yMax - yMin < 10) {
    yMin = -5;
    yMax = 5;
  }

  const xS = t => pad.l + ((t - tMin) / windowSec) * plotW;
  const yS = d => pad.t + plotH - ((d - yMin) / (yMax - yMin)) * plotH;
  const y0 = yS(0);

  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  // Up / down zones vs beat (0)
  ctx.fillStyle = "rgba(14, 203, 129, 0.06)";
  ctx.fillRect(pad.l, pad.t, plotW, Math.max(0, y0 - pad.t));
  ctx.fillStyle = "rgba(246, 70, 93, 0.06)";
  ctx.fillRect(pad.l, y0, plotW, pad.t + plotH - y0);

  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono, monospace";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    const tickVal = yMax - ((yMax - yMin) / 4) * i;
    ctx.fillText(fmtDeltaCompact(tickVal), pad.l - 4, y + 3);
  }

  // Beat = 0 line
  ctx.strokeStyle = "rgba(240, 185, 11, 0.7)";
  ctx.setLineDash([6, 4]);
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(pad.l, y0);
  ctx.lineTo(W - pad.r, y0);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(240, 185, 11, 0.9)";
  ctx.textAlign = "left";
  ctx.fillText("Beat 0", pad.l + 4, y0 - 5);

  const lastDelta = visible[visible.length - 1].d;
  const lineColor = lastDelta >= 0 ? "#0ecb81" : "#f6465d";

  // Fill to zero
  ctx.fillStyle = lastDelta >= 0 ? "rgba(14, 203, 129, 0.12)" : "rgba(246, 70, 93, 0.12)";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t), y = yS(p.d);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineTo(xS(visible[visible.length - 1].t), y0);
  ctx.lineTo(xS(visible[0].t), y0);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t), y = yS(p.d);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const last = visible[visible.length - 1];
  ctx.fillStyle = lineColor;
  ctx.beginPath();
  ctx.arc(xS(last.t), yS(last.d), 3.5, 0, Math.PI * 2);
  ctx.fill();

  return true;
}

function renderLoop() {
  const animating = smooth.up != null || smooth.btcDelta != null;
  if (needsRedraw || (isLive && animating)) {
    drawPriceChart();
    drawBtcChart();
    if (!isLive || !animating) needsRedraw = false;
  }
  requestAnimationFrame(renderLoop);
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setClass(id, cls) {
  const el = document.getElementById(id);
  if (el) el.className = cls;
}

function fmt(n) { return n == null || isNaN(n) ? "—" : Number(n).toFixed(3); }
function fmtUsd(n) {
  if (n == null || isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtUsdCompact(n) {
  if (n == null || isNaN(n)) return "—";
  if (n >= 1000) return "$" + Math.round(n).toLocaleString("en-US");
  return "$" + Number(n).toFixed(2);
}
function fmtDelta(n) {
  if (n == null || isNaN(n)) return "—";
  const sign = n >= 0 ? "+" : "−";
  return sign + "$" + Math.abs(n).toFixed(2);
}
function fmtDeltaCompact(n) {
  if (n == null || isNaN(n)) return "—";
  const sign = n >= 0 ? "+" : "−";
  const v = Math.abs(n);
  if (v >= 100) return sign + "$" + Math.round(v);
  return sign + "$" + v.toFixed(1);
}
function pct(n) { return n == null ? "—" : (n * 100).toFixed(1) + "%"; }
function fmtSize(n) { return n == null ? "—" : n >= 100 ? Number(n).toFixed(0) : Number(n).toFixed(1); }
function fmtAddr(a) { return a ? a.slice(0, 6) + "…" + a.slice(-4) : "—"; }
function lerp(a, b, t) { return a == null ? b : a + (b - a) * t; }
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

let botBusy = false;
async function startBot() {
  if (botBusy) return;
  botBusy = true;
  try { await fetch("/api/bot/start", { method: "POST" }); pollStatus(); }
  finally { botBusy = false; }
}
async function stopBot() {
  if (botBusy) return;
  botBusy = true;
  try { await fetch("/api/bot/stop", { method: "POST" }); pollStatus(); }
  finally { botBusy = false; }
}

window.addEventListener("resize", () => { needsRedraw = true; });