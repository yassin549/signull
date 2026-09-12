(function () {
/* Signull live dashboard module */

let ws, reconnectTimer, pollTimer, lastVersion = -1;
let history = [];
let btcHistory = [];
let simHistory = [];
let equityHistory = [];
let equityInitial = null;
let liveStrategyTrades = [];
let eqViewState = { xMin: null, xMax: null, isUserZoomed: false, autoScroll: true };
let eqHoverState = { active: false, x: 0, y: 0, trade: null };
let eqDragState = { isDragging: false, startX: 0, startMin: null, startMax: null };
let equityChartEventsBound = false;
let priceToBeat = null;
let activeBook = "up";
let cachedBooks = {};
let lastTradeId = null;
let isLive = false;
let lastUpdateAt = 0;
let obRenderPending = false;

const CANDLE_SEC = 300;
const RING_LEN = 97.4;
const POLL_MS = 2000;
const POLL_FAST_MS = 1000;
const smooth = { up: null, down: null, btcDelta: null };
let needsRedraw = true;
let lastServerTs = null;
let countdownBase = null;
let activeCandleSlug = null;
let activeCandleStartTs = null;
let lastBtcSnapshot = null;
let feedReconnecting = false;
let localWindowStart = null;
let lastPingAt = 0;
let lastRttMs = null;
let pingTimer = null;
let netOnline = typeof navigator === "undefined" ? true : navigator.onLine;
/** When false, WS/state keep updating but canvas paint is skipped. */
let viewActive = true;
let liveInited = false;
let candleTransitionTs = 0;
let candleTransitionTimer = null;

let cachedConfig = null;
let latestSnapshotStrategy = null;
let latestSnapshot = null;
let lwEquityChart = null;
let lwEquitySeries = null;
/** Cache to avoid re-rendering unchanged innerHTML content (fixes blinking) */
let _renderedOpenOrders = "";
let _renderedPositions = "";
let _renderedActiveBook = "";

function safe(fn) {
  return (...args) => {
    try { fn(...args); }
    catch (e) { console.error(fn.name || "handler", e); }
  };
}

function init() {
  if (liveInited) return;
  liveInited = true;
  initTabs("book-tabs", (tab) => {
    activeBook = tab;
    renderActiveBook();
    needsRedraw = true;
  });
  initTabs("info-tabs", (tab) => {
    const walletEl = document.getElementById("info-wallet");
    const logEl = document.getElementById("info-log");
    if (!walletEl || !logEl) return;
    if (tab === "wallet") {
      walletEl.classList.remove("hidden");
      logEl.classList.add("hidden");
    } else {
      walletEl.classList.add("hidden");
      logEl.classList.remove("hidden");
    }
  });
  const startBtn = document.getElementById("btn-start");
  const stopBtn = document.getElementById("btn-stop");
  if (startBtn) startBtn.addEventListener("click", startBot);
  if (stopBtn) stopBtn.addEventListener("click", stopBot);
  const verifyBtn = document.getElementById("btn-verify-wallet");
  if (verifyBtn) verifyBtn.addEventListener("click", verifyWallet);
  initStrategyCollapse();
  initHealthPopover();
  initNetWatch();
  initCopyButtons();

  initLiveStrategyControl();

  fetch("/api/config")
    .then(r => r.json())
    .then(c => {
      cachedConfig = c;
      const el = document.getElementById("asset-badge");
      if (el) el.textContent = (c.asset || "btc").toUpperCase();
      updateStrategyParams(latestSnapshotStrategy || {});
      updateSimCardHeader();
    })
    .catch(() => {});
  pollStatus();
  connect();
  setInterval(tickCountdown, 250);
  setInterval(tickSyncAge, 1000);
  setInterval(refreshTradeDetail, 1000);
  requestAnimationFrame(renderLoop);
}

function setActive(active) {
  viewActive = !!active;
  if (viewActive) needsRedraw = true;
}

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
    clearInterval(pollTimer);
    pollTimer = null;
    setSyncStatus(true);
    startPing();
    pollStatus();
  };
  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d && d.type === "pong") {
        handlePong(d);
        return;
      }
      applySnapshot(d, false);
    } catch (err) {
      console.error("ws parse", err);
    }
  };
  ws.onerror = () => {
    isLive = false;
    setSyncStatus(false);
    updateNetBanner();
  };
  ws.onclose = () => {
    isLive = false;
    setSyncStatus(false);
    stopPing();
    updateNetBanner();
    if (!pollTimer) pollTimer = setInterval(pollStatus, POLL_MS);
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

  // Delta merges may be partial — keep existing state for missing fields
  const prevVersion = lastVersion;

  lastVersion = d.version ?? lastVersion;
  lastUpdateAt = Date.now();
  setSyncStatus(true);

  // Detect version reset (server restart) — force reload
  if (isFull && prevVersion >= 0 && d.version != null && d.version < prevVersion - 100) {
    location.reload();
    return;
  }

  // Always update fast-changing fields
  if (d.btc != null) {
    lastBtcSnapshot = d.btc;
    const prevBeat = priceToBeat;
    if (d.btc.price_to_beat != null) {
      const nextBeat = Number(d.btc.price_to_beat);
      if (priceToBeat == null || Math.abs(nextBeat - priceToBeat) > 1e-9) {
        priceToBeat = nextBeat;
        if (prevBeat !== priceToBeat) recomputeBtcDeltas();
      }
    }
    seedBtcLivePoint(d.btc);
  }

  if (d.feed != null) feedReconnecting = !!d.feed.reconnecting;

  if (d.prices != null) {
    if (d.prices.up != null) smooth.up = lerp(smooth.up, d.prices.up, 0.35);
    if (d.prices.down != null) smooth.down = lerp(smooth.down, d.prices.down, 0.35);
  }

  // Candle detection
  const incomingStart = d.btc_candle_start_ts != null && Number.isFinite(Number(d.btc_candle_start_ts))
    ? Number(d.btc_candle_start_ts) : null;
  if (incomingStart != null && incomingStart !== activeCandleStartTs) {
    const isRollover = activeCandleStartTs != null;
    activeCandleStartTs = incomingStart;
    if (d.market?.slug) activeCandleSlug = d.market.slug;
    if (isRollover) rollToCandle(incomingStart, d.market?.provisional ? "soft" : "full");
    isFull = true; // treat candle roll as full reset
  }

  // Market metadata
  if (d.market) {
    if (!activeCandleSlug && d.market.slug) activeCandleSlug = d.market.slug;
    countdownBase = { market: d.market };
  }

  // Merge history tails
  if (d.equity_history != null) {
    if (isFull || !equityHistory.length) equityHistory = [];
    mergeEquityHistory(d.equity_history);
  }

  if (d.price_history != null) {
    if (isFull || !history.length) history = [];
    mergeHistory(d.price_history);
  }

  if (d.btc_history != null) {
    if (isFull || !btcHistory.length) {
      btcHistory = filterBtcHistoryForCandle(d.btc_history.map(normalizeBtcPoint));
    } else {
      mergeBtcHistory(d.btc_history);
    }
  }

  if (d.sim_history != null) {
    if (isFull || !simHistory.length) {
      simHistory = filterBtcHistoryForCandle(d.sim_history.map(normalizeSimPoint));
    } else {
      mergeSimHistory(d.sim_history);
    }
  }

  // Shared wall-clock "now"
  const oddsT = history.length ? history[history.length - 1].t : 0;
  const btcT = btcHistory.length ? btcHistory[btcHistory.length - 1].t : 0;
  const simT = simHistory.length ? simHistory[simHistory.length - 1].t : 0;
  lastServerTs = Math.max(
    oddsT, btcT, simT,
    d.feed?.last_update_at || 0,
    d.btc?.updated_at || 0,
    Date.now() / 1000 - 1
  );
  needsRedraw = true;

  onUpdate(d);
}

function mergeEquityHistory(incoming) {
  // Timestamps are generated independently by the status poll and websocket
  // paths.  Bucket them to the chart's sampling precision so a replayed
  // snapshot cannot turn one balance observation into near-vertical slivers.
  const keyFor = p => Math.round(Number(p.t) * 20) / 20;
  const byT = new Map(equityHistory.map(p => [keyFor(p), p]));
  incoming.forEach(p => {
    if (p && Number.isFinite(Number(p.t)) && Number.isFinite(Number(p.v))) {
      byT.set(keyFor(p), { ...p, t: Number(p.t), v: Number(p.v) });
    }
  });
  equityHistory = [...byT.values()].sort((a, b) => a.t - b.t).slice(-50000);
  updateLightweightEquityChart();
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
    // Never mutate existing points
  }
  if (history.length > 6000) history = history.slice(-6000);
}

function candleWindowStart() {
  if (activeCandleStartTs != null) return activeCandleStartTs;
  return clockWindowStart();
}

function syncCandleWindow(d) {
  const cs = d.btc_candle_start_ts ?? d.market?.candle_start_ts;
  if (cs != null && Number.isFinite(Number(cs))) {
    const next = Number(cs);
    if (activeCandleStartTs !== next) {
      activeCandleStartTs = next;
      btcHistory = filterBtcHistoryForCandle(btcHistory);
      recomputeBtcDeltas();
    }
  }
}

function filterBtcHistoryForCandle(points) {
  const start = candleWindowStart();
  if (start == null) return points;
  return points.filter(p =>
    (p.cs != null && Number(p.cs) === Number(start))
    || (p.cs == null && p.t >= start - 1)
  );
}

function mergeBtcHistory(incoming) {
  incoming = incoming.map(normalizeBtcPoint);
  if (!incoming.length) return;
  btcHistory = filterBtcHistoryForCandle(btcHistory);
  if (!btcHistory.length) {
    btcHistory = incoming.slice();
    return;
  }
  const lastT = btcHistory[btcHistory.length - 1].t;
  for (const pt of incoming) {
    if (pt.t > lastT && pt.v != null) {
      btcHistory.push(pt);
    }
  }
  btcHistory = filterBtcHistoryForCandle(btcHistory);
  if (btcHistory.length > 6000) btcHistory = btcHistory.slice(-6000);
}

function normalizeBtcPoint(p) {
  if (!p) return { t: 0 };
  const out = { t: Number(p.t) };
  if (p.v != null && !isNaN(Number(p.v))) out.v = Number(p.v);
  if (p.cs != null && Number.isFinite(Number(p.cs))) out.cs = Number(p.cs);
  // Prefer absolute price vs locked beat when both known — never trust a stale d.
  if (out.v != null && priceToBeat != null) {
    out.d = out.v - priceToBeat;
  } else if (p.d != null && !isNaN(Number(p.d))) {
    out.d = Number(p.d);
  }
  return out;
}

function normalizeSimPoint(p) {
  if (!p) return { t: 0 };
  const out = { t: Number(p.t) };
  if (p.v != null && !isNaN(Number(p.v))) out.v = Number(p.v);
  if (p.cs != null) out.cs = p.cs;
  return out;
}

function mergeSimHistory(incoming) {
  incoming = incoming.map(normalizeSimPoint);
  if (!incoming.length) return;
  simHistory = filterBtcHistoryForCandle(simHistory);
  if (!simHistory.length) {
    simHistory = incoming.slice();
    return;
  }
  const lastT = simHistory[simHistory.length - 1].t;
  for (const pt of incoming) {
    if (pt.t > lastT && pt.v != null) {
      simHistory.push(pt);
    }
  }
  simHistory = filterBtcHistoryForCandle(simHistory);
  if (simHistory.length > 6000) simHistory = simHistory.slice(-6000);
}

/** Recompute Δ from absolute price when beat is known. */
function recomputeBtcDeltas() {
  if (priceToBeat == null || !btcHistory.length) return;
  for (let i = 0; i < btcHistory.length; i++) {
    const p = btcHistory[i];
    if (p.v != null) {
      btcHistory[i] = { t: p.t, v: p.v, d: p.v - priceToBeat };
    }
  }
}

/** Pick the freshest usable spot source for the BTC panel and chart. */
function currentBtcSpot(btc, now = Date.now() / 1000) {
  if (!btc) return { value: null, source: null, binanceAge: Infinity };
  const binance = btc.price != null ? Number(btc.price) : NaN;
  const chainlink = btc.chainlink != null ? Number(btc.chainlink) : NaN;
  const binanceAge = btc.updated_at != null ? now - Number(btc.updated_at) : Infinity;

  if (Number.isFinite(binance) && binanceAge <= 2) {
    return { value: binance, source: "binance", binanceAge };
  }
  // State switches the server history to Chainlink after a two-second Binance
  // gap. Mirror that choice here so a stale opening price cannot pin Δ at $0.
  if (Number.isFinite(chainlink)) {
    return { value: chainlink, source: "chainlink", binanceAge };
  }
  return Number.isFinite(binance)
    ? { value: binance, source: "stale-binance", binanceAge }
    : { value: null, source: null, binanceAge };
}

/**
 * Push a live sample from the latest BTC panel tick.
 * Always uses wall clock so a frozen server updated_at cannot pin the series.
 * Only seeds when the snapshot is fresh (Binance < 2s or Chainlink) to avoid
 * injecting fake flat-line points during disconnection.
 */
function seedBtcLivePoint(btc) {
  const spot = currentBtcSpot(btc);
  if (spot.source === "stale-binance" || spot.source == null) return false;
  const v = spot.value;
  if (v == null) return false;

  const now = Date.now() / 1000;
  let d = null;
  if (priceToBeat != null) d = v - priceToBeat;
  else if (btc.delta != null) d = Number(btc.delta);

  const cs = activeCandleStartTs;
  const point = { t: now, v, d, cs };

  if (!btcHistory.length) {
    btcHistory.push(point);
    return true;
  }
  const last = btcHistory[btcHistory.length - 1];
  // Only append if at least 50ms has passed — never overwrite
  if (now - last.t >= 0.05) {
    btcHistory.push(point);
    if (btcHistory.length > 6000) btcHistory = btcHistory.slice(-6000);
  }
  return true;
}

function liveBtcSpotPrice() {
  if (!lastBtcSnapshot) return null;
  return currentBtcSpot(lastBtcSnapshot).value;
}

const onUpdate = safe(function onUpdate(d) {
  latestSnapshot = d;
  detectCandleChange(d.market);
  updateTopbar(d);
  updateHero(d);
  updateStrategy(d);
  if (d.account) updateWallet(d);
  if (d.orderbooks) updateOrderbooks(d.orderbooks);
  updateStrategyTrades(d.strategy_trades || []);
  updateTape();
  if (d.activity) updateLog(d.activity);
  updateBtcPanel(d.btc);
  updateBotButtons(d.running);
});

function candleStartFromMarket(market) {
  if (!market) return null;
  if (market.candle_start_ts != null && Number.isFinite(Number(market.candle_start_ts))) {
    return Number(market.candle_start_ts);
  }
  const slug = String(market.slug || "");
  const m = slug.match(/(\d{9,})$/);
  return m ? Number(m[1]) : null;
}

function detectCandleChange(market) {
  if (!market) return;
  const nextStart = candleStartFromMarket(market);
  if (nextStart == null) {
    if (market.slug) activeCandleSlug = market.slug;
    countdownBase = { market };
    return;
  }
  if (activeCandleStartTs === nextStart) {
    activeCandleSlug = market.slug || activeCandleSlug;
    countdownBase = { market };
    return;
  }
  const isRollover = activeCandleStartTs != null;
  activeCandleSlug = market.slug || null;
  activeCandleStartTs = nextStart;
  countdownBase = { market };
  if (isRollover) {
    rollToCandle(nextStart, market.provisional ? "soft" : "full");
  }
}

function rollToCandle(startTs, mode = "full") {
  clearTimeout(candleTransitionTimer);
  if (candleTransitionTimer && candleTransitionTimer._cleanup) clearTimeout(candleTransitionTimer._cleanup);
  candleTransitionTs = Date.now();

  // Show settlement outcome from latest trade if we just had one
  const latestTrade = liveStrategyTrades.length ? liveStrategyTrades[liveStrategyTrades.length - 1] : null;
  const recentTrade = latestTrade && (Date.now() / 1000 - Number(latestTrade.t) < 10);
  const outcomeWon = recentTrade ? !!latestTrade.won : null;
  const outcomeSide = recentTrade ? latestTrade.side : null;

  resetCandleUI(mode, outcomeWon, outcomeSide);

  // After showing the outcome, transition to "loading new candle" phase
  const loadingTm = setTimeout(() => {
    if (Date.now() - candleTransitionTs < 4000) {
      resetCandleUI(mode, null, null, true);
    }
  }, 1800);

  // Clear transition state after 3.2s so new candle data can take over
  const cleanupTm = setTimeout(() => {
    candleTransitionTs = 0;
  }, 3200);

  // Store both timers so repeat calls clear them all
  candleTransitionTimer = loadingTm;
  candleTransitionTimer._cleanup = cleanupTm;
}

function resetCandleUI(mode = "full", outcomeWon = null, outcomeSide = null, showLoading = false) {
  history = [];
  btcHistory = filterBtcHistoryForCandle(btcHistory);
  priceToBeat = null;
  smooth.btcDelta = null;
  lastServerTs = null;
  lastTradeId = null;
  clearCachedYRanges();
  _renderedOpenOrders = "";
  _renderedPositions = "";
  _renderedActiveBook = "";
  _lastStratTradesHTML = "";
  _lastLogHTML = "";
  _tapeKeys = [];
  _tapeLimit = TAPE_BATCH;
  _lastStratParamsHTML = "";
  needsRedraw = true;
  if (mode === "full") {
    smooth.up = smooth.down = null;
    cachedBooks = {};
    setText("price-up", "—");
    setText("price-down", "—");
    setText("pct-up", "—");
    setText("pct-down", "—");
    setText("prob-delta", "—");
  }
  setText("hero-btc-beat", "Beat —");
  setText("hero-btc-delta", "Δ —");
  setText("btc-chart-meta", "—");
  const probEl = document.getElementById("prob-up");
  if (probEl && mode === "full") probEl.style.width = "50%";

  if (showLoading) {
    setText("signal-side", "LOADING");
    setClass("signal-side", "signal-badge hold");
    setText("signal-reason", "Loading new candle data…");
  } else if (outcomeWon != null) {
    setText("signal-side", outcomeWon ? "WIN" : "LOSS");
    setClass("signal-side", "signal-badge " + (outcomeWon ? "up" : "down"));
    setText("signal-reason", `Last candle closed — ${outcomeWon ? "Correct" : "Wrong"} ${(outcomeSide || "").toUpperCase()} call`);
  } else {
    setText("signal-side", "HOLD");
    setClass("signal-side", "signal-badge hold");
    setText("signal-reason", mode === "soft" ? "Rolling into new candle…" : "New candle — warming up");
  }
}

/** UTC 5m window: seconds remaining in the current wall-clock candle. */
function clockSecsToClose() {
  const now = Date.now() / 1000;
  const start = Math.floor(now / CANDLE_SEC) * CANDLE_SEC;
  return Math.max(0, start + CANDLE_SEC - now);
}

function clockWindowStart() {
  return Math.floor(Date.now() / 1000 / CANDLE_SEC) * CANDLE_SEC;
}

function marketSecsToClose(market) {
  // Prefer market end_date while it's still in the future.
  if (market?.end_date) {
    const end = new Date(market.end_date).getTime();
    if (!isNaN(end)) {
      const secs = (end - Date.now()) / 1000;
      // Stale previous candle — roll with wall clock immediately (no 0:00 freeze).
      if (secs <= 0) return clockSecsToClose();
      return secs;
    }
  }
  if (market?.seconds_to_close != null) {
    const s = Number(market.seconds_to_close);
    if (s > 0) return s;
  }
  return clockSecsToClose();
}

function marketIsStale(market) {
  if (!market?.end_date) return false;
  const end = new Date(market.end_date).getTime();
  return !isNaN(end) && end <= Date.now();
}

function updateTopbar(d) {
  const feed = d.feed || {};
  const health = d.health || {};
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
  if (feedEl) {
    const t = feed.error || (feed.reconnecting ? "Reconnecting…" : "CLOB feed");
    if (feedEl.title !== t) feedEl.title = t;
  }

  setClass("mode-status", "status-item live");
  setText("mode-text", "LIVE");

  setClass("bot-status", "status-item" + (d.running ? " live" : " off"));
  setText("bot-text", d.running ? "On" : "Off");

  const netOk = netOnline && isLive;
  setClass("net-status", "status-item" + (netOk ? " live" : " off"));
  setText("net-text", lastRttMs != null ? `${Math.round(lastRttMs)}ms` : (netOk ? "on" : "off"));

  const srv = health.loop_lag_ms != null && health.loop_lag_ms > 80 ? "warn"
    : (health.updated_at ? "live" : "");
  setClass("srv-status", "status-item" + (srv ? " " + srv : ""));
  setText("srv-text", health.loop_lag_ms != null ? `${Math.round(health.loop_lag_ms)}ms` : "ok");

  const spot = health.spot || {};
  const spotAge = spot.age_sec;
  const spotCls = spot.connected && (spotAge == null || spotAge < 3) ? "live"
    : (spot.connected ? "warn" : "off");
  setClass("spot-status", "status-item " + spotCls);
  setText("spot-text", spot.connected ? "live" : "off");

  const wal = health.wallet || d.account || {};
  const walOk = !!(wal.connected || wal.ok);
  setClass("wallet-status-dot", "status-item" + (walOk ? " live" : " off"));
  setText("wallet-dot-text", walOk ? "ready" : "off");

  if (d.market?.title) setText("market-short", shortenMarket(d.market.title));
  updateHealthPopover(d);
  updateNetBanner();
}

let _lastSyncOk = null;
function setSyncStatus(ok) {
  if (_lastSyncOk === ok) return;
  _lastSyncOk = ok;
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

let _probWidth = "";
function updateHero(d) {
  const prices = d.prices || {};
  const up = prices.up;
  const down = prices.down;

  if (up != null) {
    smooth.up = lerp(smooth.up, up, 0.35);
    if (setText("price-up", fmt(up))) flashEl("price-up");
    setText("pct-up", pct(up));
  }
  if (down != null) {
    smooth.down = lerp(smooth.down, down, 0.35);
    if (setText("price-down", fmt(down))) flashEl("price-down");
    setText("pct-down", pct(down));
  }

  if (up != null && down != null) {
    const total = up + down;
    const upPct = total > 0 ? (up / total) * 100 : 50;
    const w = upPct.toFixed(1) + "%";
    if (_probWidth !== w) { _probWidth = w; const probEl = document.getElementById("prob-up"); if (probEl) probEl.style.width = w; }
    const delta = up - down;
    const deltaStr = (delta >= 0 ? "▲ " : "▼ ") + Math.abs(delta * 100).toFixed(1) + "¢";
    setText("prob-delta", deltaStr);
    needsRedraw = true;
  }

  if (d.market) {
    countdownBase = { market: d.market };
  }

  if (d.signal) {
    const side = (d.signal.side || "hold").toLowerCase();
    setText("signal-side", side.toUpperCase());
    setClass("signal-side", "signal-badge " + side);
    setText("signal-reason", d.signal.reason || "");
  }
}

function _setEl(id, txt, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  if (txt != null && el.textContent !== txt) el.textContent = txt;
  if (cls != null && el.className !== cls) el.className = cls;
}

function updateStrategy(d) {
  const s = d.strategy || {};
  const prices = d.prices || {};
  const p = s.pending;
  if (p) {
    setText("strat-pending", `LIVE ${String(p.side).toUpperCase()} @ ${Number(p.entry_price).toFixed(2)} · ${p.size_label} $${Number(p.stake).toFixed(2)}`);
    const stake = Number(p.stake) || 0;
    const entryPrice = Number(p.entry_price) || 1;
    const shares = entryPrice > 0 ? stake / entryPrice : 0;
    const fee = Number(p.entry_fee) || 0;
    const potentialGain = shares * 1.0 - stake - fee;
    const side = String(p.side || "").toUpperCase();
    const currentPrice = side === "UP" ? prices.up : (side === "DOWN" ? prices.down : null);
    const currentValue = currentPrice != null && currentPrice > 0 ? shares * currentPrice : null;
    const unrealizedPnl = currentValue != null ? currentValue - stake : null;
    setText("td-side", side);
    _setEl("td-side", side, "td-value td-side " + side.toLowerCase());
    setText("td-entry-price", `@ ${entryPrice.toFixed(2)} (${(entryPrice * 100).toFixed(0)}¢)`);
    setText("td-stake", `$${stake.toFixed(2)}`);
    setText("td-risk-pct", p.risk_pct != null ? `${Number(p.risk_pct).toFixed(1)}%` : "—");
    setText("td-entry-fee", fee > 0 ? `-$${fee.toFixed(4)}` : "—");
    setText("td-potential-gain", `+$${potentialGain.toFixed(2)}`);
    setText("td-current-value", currentValue != null ? `$${currentValue.toFixed(2)}` : "—");
    if (unrealizedPnl != null) {
      const uval = `${unrealizedPnl >= 0 ? "+" : ""}$${unrealizedPnl.toFixed(2)}`;
      const ucls = "td-value td-pnl " + (unrealizedPnl >= 0 ? "up" : "down");
      _setEl("td-unrealized-pnl", uval, ucls);
    } else {
      setText("td-unrealized-pnl", "—");
    }
    const requested = Number(p.requested_shares) || 0;
    const filled = Number(p.filled_shares) || 0;
    setText("td-fill", p.mode === "live" && requested > 0 ? `${(filled / requested * 100).toFixed(1)}% (${filled.toFixed(2)} / ${requested.toFixed(2)} shrs)` : "—");
    const entryTs = Number(p.entry_ts) || 0;
    setText("td-elapsed", entryTs > 0 ? (() => { const e = Math.max(0, Date.now() / 1000 - entryTs); return `${Math.floor(e / 60)}m ${Math.floor(e % 60)}s`; })() : "—");
    if (s.wins_streak != null) _setEl("td-win-streak", String(s.wins_streak), "td-value td-win" + (s.wins_streak > 0 ? "" : " muted"));
    if (s.losses_streak != null) _setEl("td-loss-streak", String(s.losses_streak), "td-value td-loss" + (s.losses_streak > 0 ? "" : " muted"));
    setText("td-wins-recent", s.wins_recent != null ? `${s.wins_recent}/10` : "—");
    const hbOk = s.heartbeat_last_ok;
    setText("td-heartbeat", hbOk ? `OK (${Math.round((Date.now()/1000 - hbOk) / 60)}m ago)` : (s.heartbeat_failures > 0 ? `${s.heartbeat_failures} failures` : "—"));
  } else {
    setText("strat-pending", s.entered_this_candle ? "Entered · waiting resolve" : "No position");
    const hbOk = s.heartbeat_last_ok;
    setText("td-heartbeat", hbOk ? `OK (${Math.round((Date.now()/1000 - hbOk) / 60)}m ago)` : (s.heartbeat_failures > 0 ? `${s.heartbeat_failures} failures` : "—"));
  }
}

let _lastStratTradesHTML = "";
let _lastLogHTML = "";
let _lastStratParamsHTML = "";
const TAPE_BATCH = 30;
let _tapeEl = null;
let _tapeKeys = [];
let _tapeLimit = TAPE_BATCH;

function updateStrategyTrades(trades) {
  if (Array.isArray(trades)) {
    const existing = new Map(liveStrategyTrades.map(t => [t.slug || `${t.t}_${t.side}`, t]));
    for (const t of trades) {
      if (t) existing.set(t.slug || `${t.t}_${t.side}`, t);
    }
    liveStrategyTrades = [...existing.values()].sort((a, b) => Number(a.t) - Number(b.t));
  }

  const countEl = document.getElementById("equity-trades-count");
  if (countEl) {
    const ct = `${liveStrategyTrades.length} trade${liveStrategyTrades.length === 1 ? "" : "s"}`;
    if (countEl.textContent !== ct) countEl.textContent = ct;
  }

  const el = document.getElementById("strat-trades");
  if (!el) return;
  let html;
  if (!liveStrategyTrades.length) {
    html = '<div class="placeholder">No Signull trades yet</div>';
  } else {
    const sortedDesc = [...liveStrategyTrades].reverse();
    html = sortedDesc.slice(0, 20).map(t => {
      const cls = t.won ? "win" : "loss";
      const pnl = tradePnl(t);
      const fee = Number(t.entry_fee || 0);
      const fillInfo = t.filled_shares != null && t.mode === "live" ? `fill=${t.filled_shares}shrs` : "";
      const tip = [t.won ? "WIN" : "LOSS", `side=${t.side}`, `winner=${t.winner || "?"}`, t.resolve_source ? `via ${t.resolve_source}` : "", fillInfo, fee > 0 ? `fee=${fee.toFixed(4)}` : "", `stake=$${Number(t.stake).toFixed(2)}`, t.title || t.slug || ""].filter(Boolean).join(" · ");
      return `<div class="strat-trade ${cls}" title="${esc(tip)}">
        <span class="st-side">${String(t.side || "").toUpperCase()}</span>
        <span class="st-px">@${Number(t.entry_price).toFixed(2)}</span>
        <span class="st-sz">${t.size_label || ""} $${Number(t.stake).toFixed(2)}</span>
        <span class="st-pnl">${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}</span>
        <span class="st-eq">$${Number(t.equity_after).toFixed(2)}</span>
        ${fee > 0 ? `<span class="st-fee">-$${fee.toFixed(4)}</span>` : ""}
        ${t.filled_shares != null && t.mode === "live" ? `<span class="st-fill">${t.filled_shares}shrs</span>` : ""}
      </div>`;
    }).join("");
  }
  if (_lastStratTradesHTML !== html) { el.innerHTML = html; _lastStratTradesHTML = html; }
  needsRedraw = true;
}

function flashEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function refreshTradeDetail() {
  const d = latestSnapshot;
  if (!d) return;
  const s = d.strategy || {};
  const p = s.pending;
  if (!p) {
    const hbOk = s.heartbeat_last_ok;
    setText("td-heartbeat", hbOk ? `OK (${Math.round((Date.now()/1000 - hbOk) / 60)}m ago)` : (s.heartbeat_failures > 0 ? `${s.heartbeat_failures} failures` : "—"));
    return;
  }
  const stake = Number(p.stake) || 0;
  const entryPrice = Number(p.entry_price) || 1;
  const shares = entryPrice > 0 ? stake / entryPrice : 0;
  const prices = d.prices || {};
  const side = String(p.side || "").toUpperCase();
  const currentPrice = side === "UP" ? prices.up : (side === "DOWN" ? prices.down : null);
  const currentValue = currentPrice != null && currentPrice > 0 ? shares * currentPrice : null;
  const unrealizedPnl = currentValue != null ? currentValue - stake : null;
  setText("td-current-value", currentValue != null ? `$${currentValue.toFixed(2)}` : "—");
  if (unrealizedPnl != null) {
    const pnlEl = document.getElementById("td-unrealized-pnl");
    if (pnlEl) {
      const txt = `${unrealizedPnl >= 0 ? "+" : ""}$${unrealizedPnl.toFixed(2)}`;
      const cls = "td-value td-pnl " + (unrealizedPnl >= 0 ? "up" : "down");
      if (pnlEl.textContent !== txt) pnlEl.textContent = txt;
      if (pnlEl.className !== cls) pnlEl.className = cls;
    }
  }
  setText("td-elapsed", (() => {
    const ts = Number(p.entry_ts) || 0;
    if (!ts) return "—";
    const elapsed = Math.max(0, Date.now() / 1000 - ts);
    return `${Math.floor(elapsed / 60)}m ${Math.floor(elapsed % 60)}s`;
  })());
  const hbOk = s.heartbeat_last_ok;
  setText("td-heartbeat", hbOk ? `OK (${Math.round((Date.now()/1000 - hbOk) / 60)}m ago)` : (s.heartbeat_failures > 0 ? `${s.heartbeat_failures} failures` : "—"));
}

function tickCountdown() {
  // Local 5m boundary — refresh UI even if the server is still on the old slug.
  const win = clockWindowStart();
  if (localWindowStart != null && win !== localWindowStart) {
    if (marketIsStale(countdownBase?.market) || !countdownBase?.market) {
      // Same slug shape as the server stub so hydrate is not a second reset.
      const asset = (cachedConfig?.asset || "btc").toLowerCase();
      const endMs = (win + CANDLE_SEC) * 1000;
      const provisional = {
        slug: `${asset}-updown-5m-${win}`,
        end_date: new Date(endMs).toISOString(),
        candle_start_ts: win,
        candle_duration_sec: CANDLE_SEC,
        seconds_to_close: clockSecsToClose(),
        provisional: true,
        title: countdownBase?.market?.title || "New candle",
      };
      detectCandleChange(provisional);
      countdownBase = { market: provisional };
    }
  }
  localWindowStart = win;

  const market = countdownBase?.market;
  const raw = marketSecsToClose(market);
  const secs = Math.max(0, Math.round(raw ?? clockSecsToClose()));
  const stale = marketIsStale(market) || !!market?.provisional;
  renderCountdown(secs, stale);
  updateTimerRing(secs, market, stale);

  // Poll hard near boundary / while market is stale so the real slug lands fast.
  const wantFast = !isLive && (secs <= 20 || stale || feedReconnecting);
  if (wantFast && !tickCountdown._fast) {
    tickCountdown._fast = true;
    clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, POLL_FAST_MS);
  } else if (!wantFast && tickCountdown._fast) {
    tickCountdown._fast = false;
    clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, POLL_MS);
  }
}

function renderCountdown(secs, stale) {
  const el = document.getElementById("countdown");
  const label = document.querySelector(".timer-label");
  // Never freeze on 0:00 for half a minute — clock fallback already gives secs > 0
  // after rollover. "Loading" only when truly at the exact boundary.
  const atBoundary = secs <= 0;
  const loading = !!stale && secs > 290;

  if (el) {
    const t = atBoundary ? "0:00" : `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    if (el.textContent !== t) el.textContent = t;
    el.classList.toggle("rolling", atBoundary || loading);
  }
  if (label) {
    const t = atBoundary ? "rolling over" : (loading ? "loading market" : "to close");
    if (label.textContent !== t) label.textContent = t;
  }
}

function updateTimerRing(secs, market, stale) {
  const ring = document.getElementById("ring-fg");
  if (!ring) return;
  const total = market?.candle_duration_sec || CANDLE_SEC;
  const atBoundary = secs <= 0;
  const elapsed = atBoundary ? 1 : Math.max(0, Math.min(1, 1 - secs / total));
  ring.style.strokeDashoffset = String(RING_LEN * (1 - elapsed));
  ring.classList.toggle("urgent", !atBoundary && secs < 30);
  ring.classList.toggle("rolling", atBoundary || !!stale);
}

function updateWallet(d) {
  const a = d.account || {};
  const s = d.strategy || {};
  const connected = !!a.connected;

  const balLabel = document.getElementById("wallet-balance-label");
  if (balLabel) {
    if (balLabel.textContent !== "CLOB USDC") balLabel.textContent = "CLOB USDC";
  }

  const liveBal = a.balance_usdc;
  setText("wallet-balance", liveBal != null && Number.isFinite(Number(liveBal)) ? `$${Number(liveBal).toFixed(2)}` : "—");
  setText("wallet-onchain-usdc", a.onchain_usdc != null ? `$${Number(a.onchain_usdc).toFixed(2)}` : "—");
  const isGasless = a.signature_type !== 0;
  const gasText = a.gas_pol != null
    ? `${Number(a.gas_pol).toFixed(4)} POL${a.gas_low ? " · low" : (isGasless ? " (gasless)" : "")}`
    : "—";
  setText("wallet-gas", gasText);
  { const gasEl = document.getElementById("wallet-gas"); if (gasEl) gasEl.classList.toggle("warn", !!a.gas_low); }
  setText("wallet-available", a.available_usdc != null ? `$${Number(a.available_usdc).toFixed(2)}` : "—");
  setText("wallet-reserved", a.reserved_usdc != null ? `$${Number(a.reserved_usdc).toFixed(2)}` : "—");
  const fmtAllowance = (val) => {
    if (val == null) return "—";
    const num = Number(val);
    if (!Number.isFinite(num)) return "—";
    if (num >= 1e9) return "Unlimited ($1.00B+)";
    return `$${num.toFixed(2)}`;
  };
  setText("wallet-allowance", fmtAllowance(a.allowance_usdc));

  setText("wallet-funder", a.funder_address || "—");
  setText("wallet-signer", a.signer_address || "—");
  setText("wallet-funder-short", fmtAddr(a.funder_address));
  setText("wallet-signer-short", fmtAddr(a.signer_address));
  setText("wallet-gas-addr", a.gas_address || "—");
  setText("wallet-type", a.signature_label || "—");
  setText("wallet-strategy", s.name || cachedConfig?.strategy_name || getActiveSimLabel());
  const thr = s.params?.threshold;
  setText("wallet-threshold", thr != null ? `${(Number(thr) * 100).toFixed(0)}¢ limit` : "—");

  const ageEl = document.getElementById("wallet-updated");
  if (ageEl) {
    if (a.updated_at) {
      const age = Math.max(0, Date.now() / 1000 - Number(a.updated_at));
      const t = age < 1.5 ? "just now" : `${age.toFixed(1)}s ago`;
      if (ageEl.textContent !== t) ageEl.textContent = t;
    } else {
      setText("wallet-updated", "—");
    }
  }

  updateEquityMeta(liveBal);
  setExplorerLinks(a);
  renderOpenOrders(d.open_orders || a.open_orders || []);
  renderPositions(d.positions || a.positions || []);

  const statusEl = document.getElementById("wallet-status");
  if (statusEl) {
    let stxt, scls;
    if (!a.has_wallet && !a.funder_address && !a.signer_address) { stxt = "No wallet configured"; scls = "wallet-pill off"; }
    else if (connected) { stxt = "Live trading"; scls = "wallet-pill ok"; }
    else if (a.has_wallet) { stxt = "CLOB offline"; scls = "wallet-pill off"; }
    else { stxt = "Not verified"; scls = "wallet-pill off"; }
    if (statusEl.textContent !== stxt) statusEl.textContent = stxt;
    if (statusEl.className !== scls) statusEl.className = scls;
  }

  const issuesEl = document.getElementById("wallet-issues");
  if (issuesEl) {
    if (a.issues?.length) {
      const h = a.issues.map(t => `<div class="wallet-issue">${esc(t)}</div>`).join("");
      if (issuesEl.innerHTML !== h) { issuesEl.innerHTML = h; issuesEl.classList.remove("hidden"); }
    } else {
      if (!issuesEl.classList.contains("hidden")) issuesEl.classList.add("hidden");
    }
  }

  const tipsEl = document.getElementById("wallet-tips");
  if (tipsEl) {
    if (a.tips?.length) {
      const h = a.tips.map(t => `<div class="wallet-tip">${esc(t)}</div>`).join("");
      if (tipsEl.innerHTML !== h) { tipsEl.innerHTML = h; tipsEl.classList.remove("hidden"); }
    } else {
      if (!tipsEl.classList.contains("hidden")) tipsEl.classList.add("hidden");
    }
  }

  updateStrategyParams(s);
}

let liveStrategies = [];
let userEditingLiveParams = false;

function initLiveStrategyControl() {
  const sel = document.getElementById("live-strategy-select");
  const applyBtn = document.getElementById("btn-apply-live-strategy");
  if (!sel) return;

  if (applyBtn) {
    applyBtn.addEventListener("click", applyLiveStrategy);
  }
  sel.addEventListener("change", () => {
    const activeId = cachedConfig?.strategy || latestSnapshotStrategy?.id;
    userEditingLiveParams = sel.value !== activeId;
    onLiveStrategyChange();
  });

  fetch("/api/strategies")
    .then(r => r.json())
    .then(data => {
      liveStrategies = data.strategies || [];
      if (!liveStrategies.length) {
        renderLiveParamsBox({});
        const desc = document.getElementById("live-strategy-desc");
        if (desc) desc.textContent = "No strategies found.";
        return;
      }
      sel.innerHTML = liveStrategies.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
      const activeId = latestSnapshotStrategy?.id || cachedConfig?.strategy || liveStrategies[0].id;
      sel.value = activeId;
      onLiveStrategyChange();
    })
    .catch(err => {
      console.error("Failed to load live strategies", err);
    });
}

function onLiveStrategyChange() {
  const sel = document.getElementById("live-strategy-select");
  if (!sel) return;
  const selectedId = sel.value;
  const strat = liveStrategies.find(s => s.id === selectedId);
  const descEl = document.getElementById("live-strategy-desc");
  if (descEl) {
    descEl.textContent = strat ? strat.description : "No description available.";
  }

  const activeId = latestSnapshotStrategy?.id || cachedConfig?.strategy;
  const activeParams = latestSnapshotStrategy?.params || cachedConfig?.strategy_params || {};
  const defaultParams = strat?.default_params || {};
  const currentParams = selectedId === activeId
    ? { ...defaultParams, ...activeParams }
    : defaultParams;

  renderLiveParamsBox(currentParams);
}

function liveParamLabel(k) {
  const labels = {
    threshold: "threshold (model P)",
    risk_pct: "risk per trade",
    min_seconds_elapsed: "min seconds",
    max_seconds_elapsed: "max seconds",
    live_sample_sec: "live sample sec",
    late_entry_seconds: "late entry seconds",
    target_delta: "target delta",
    taker_fee_rate: "taker fee rate",
  };
  return labels[k] || k.replace(/_/g, " ");
}

function renderLiveParamsBox(params) {
  const box = document.getElementById("live-params-box");
  if (!box) return;

  const entries = Object.entries(params || {});
  if (!entries.length) {
    box.innerHTML = '<div class="placeholder">No parameters for this strategy</div>';
    return;
  }

  box.innerHTML = entries.map(([k, v]) => {
    const step = paramStepVal(v);
    const min = k === "threshold" ? "0.5" : (k.includes("pct") || k.includes("rate") ? "0" : "");
    const max = k === "threshold" ? "0.99" : (k.includes("pct") || k.includes("rate") ? "1" : "");
    return `
      <div class="live-param-chip">
        <label title="${esc(k)}">${esc(liveParamLabel(k))}</label>
        <input type="number" step="${step}" ${min !== "" ? `min="${min}"` : ""} ${max !== "" ? `max="${max}"` : ""} data-live-param="${esc(k)}" value="${esc(String(v))}" />
      </div>
    `;
  }).join("");

  box.querySelectorAll("input").forEach(input => {
    input.addEventListener("focus", () => { userEditingLiveParams = true; });
    input.addEventListener("input", () => { userEditingLiveParams = true; });
  });
}

function paramStepVal(v) {
  const n = Number(v);
  if (Number.isFinite(n)) {
    if (Number.isInteger(n)) return "1";
    if (Math.abs(n) >= 1) return "0.05";
    return "0.01";
  }
  return "any";
}

async function applyLiveStrategy() {
  const sel = document.getElementById("live-strategy-select");
  const applyBtn = document.getElementById("btn-apply-live-strategy");
  const msgEl = document.getElementById("live-strat-update-msg");
  if (!sel) return;

  const strategyId = sel.value;
  const params = {};
  document.querySelectorAll("[data-live-param]").forEach(el => {
    const key = el.dataset.liveParam;
    const v = parseFloat(el.value);
    if (!Number.isNaN(v)) {
      params[key] = v;
    }
  });

  if (applyBtn) {
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying...";
  }
  if (msgEl) {
    msgEl.textContent = "";
    msgEl.className = "strat-update-msg";
  }

  try {
    const res = await fetch("/api/strategy/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy_id: strategyId, params }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText || "Failed to update strategy");
    }

    const data = await res.json();
    userEditingLiveParams = false;
    if (cachedConfig) {
      cachedConfig.strategy = data.strategy_id;
      cachedConfig.strategy_name = data.strategy_name;
      cachedConfig.strategy_params = data.params;
    }
    if (latestSnapshotStrategy) {
      latestSnapshotStrategy.id = data.strategy_id;
      latestSnapshotStrategy.name = data.strategy_name;
      latestSnapshotStrategy.params = data.params;
    }
    updateSimCardHeader();
    needsRedraw = true;

    if (msgEl) {
      msgEl.textContent = "Applied";
      msgEl.className = "strat-update-msg ok";
      setTimeout(() => {
        if (msgEl.textContent === "Applied") msgEl.textContent = "";
      }, 3500);
    }

    pollStatus();
    fetch("/api/config").then(r => r.json()).then(c => { cachedConfig = c; }).catch(() => {});
  } catch (e) {
    if (msgEl) {
      msgEl.textContent = e.message;
      msgEl.className = "strat-update-msg err";
    }
  } finally {
    if (applyBtn) {
      applyBtn.disabled = false;
      applyBtn.textContent = "Apply Changes";
    }
  }
}

function getActiveSimLabel() {
  const activeName = latestSnapshotStrategy?.name || cachedConfig?.strategy_name || "";
  const activeId = latestSnapshotStrategy?.id || cachedConfig?.strategy || "";

  const match = activeName.match(/(?:SIM|Signull)\s*(\d+\.\d+)/i);
  if (match) {
    return `SIM ${match[1]}`;
  }

  const idMatch = activeId.match(/signull_(\d+)_(\d+)/i);
  if (idMatch) {
    return `SIM ${idMatch[1]}.${idMatch[2]}`;
  }

  if (activeName) {
    return activeName.split("(")[0].trim();
  }

  return "SIM 1.1";
}

function updateSimCardHeader() {
  const label = getActiveSimLabel();
  setText("sim-card-title", `${label} Model Probability`);
}

function updateStrategyParams(s) {
  if (s && Object.keys(s).length) {
    latestSnapshotStrategy = s;
  }
  updateSimCardHeader();
  needsRedraw = true;
  const grid = document.getElementById("strat-params-grid");
  const nameEl = document.getElementById("strat-params-name");

  const stratName = s?.name || cachedConfig?.strategy_name || cachedConfig?.strategy || "Signull 1.1 (Always In)";
  if (nameEl && nameEl.textContent !== stratName) nameEl.textContent = stratName;

  const activeId = s?.id || cachedConfig?.strategy;
  const sel = document.getElementById("live-strategy-select");
  if (sel && activeId && !userEditingLiveParams && sel.value !== activeId) {
    if (Array.from(sel.options).some(opt => opt.value === activeId)) {
      sel.value = activeId;
      onLiveStrategyChange();
    }
  }

  const params = (s && s.params && Object.keys(s.params).length > 0)
    ? s.params
    : (cachedConfig?.strategy_params || {});

  const keys = Object.keys(params);
  if (!grid) return;
  let html;
  if (!keys.length) {
    html = '<div class="placeholder">No parameters loaded</div>';
  } else {
    html = keys.map(k => {
      const rawVal = params[k];
      let displayVal = rawVal;
      if (typeof rawVal === "number") {
        if (Number.isInteger(rawVal)) displayVal = rawVal.toString();
        else if (k.includes("pct") || k.includes("rate") || k.includes("threshold")) displayVal = `${(rawVal * 100).toFixed(rawVal * 100 % 1 === 0 ? 0 : 1)}%`;
        else if (k.includes("usdc") || k.includes("stake")) displayVal = `$${rawVal.toFixed(2)}`;
        else if (k.includes("seconds") || k.includes("sec")) displayVal = `${rawVal.toFixed(0)}s`;
        else displayVal = rawVal.toFixed(3);
      } else if (typeof rawVal === "boolean") displayVal = rawVal ? "True" : "False";
      const label = k.replace(/_/g, " ");
      return `<div class="param-card">
        <span class="param-key" title="${esc(k)}">${esc(label)}</span>
        <span class="param-val mono" title="${esc(String(rawVal))}">${esc(String(displayVal))}</span>
      </div>`;
    }).join("");
  }
  if (_lastStratParamsHTML !== html) { grid.innerHTML = html; _lastStratParamsHTML = html; }
}

function updateEquityMeta(equity, initial) {
  if (!equityHistory.length) {
    setText("equity-chart-meta", "Waiting for balance data");
    return;
  }
  const latest = equityHistory[equityHistory.length - 1];
  const current = Number.isFinite(Number(latest.v)) ? Number(latest.v) : (equity != null ? Number(equity) : null);
  if (current == null) {
    setText("equity-chart-meta", "Waiting for balance data");
    return;
  }
  const start = initial != null ? Number(initial) : Number(equityHistory[0].v);
  const change = current - start;
  const pctChange = start ? (change / start) * 100 : 0;
  setText(
    "equity-chart-meta",
    `$${current.toFixed(2)} · ${change >= 0 ? "+" : "−"}${fmtUsdCompact(Math.abs(change))} (${pctChange >= 0 ? "+" : ""}${pctChange.toFixed(2)}%)`
  );
}

let _lastBookRender = 0;
function updateOrderbooks(books) {
  cachedBooks = books;
  if (!obRenderPending) {
    obRenderPending = true;
    requestAnimationFrame(() => {
      obRenderPending = false;
      const now = Date.now();
      if (now - _lastBookRender < 300) return;
      _lastBookRender = now;
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

  let html;
  if (!book || !hasLevels) {
    if (book?.best_bid != null && book?.best_ask != null) {
      html = '<div class="placeholder">Depth loading…</div>';
    } else {
      html = '<div class="placeholder">Waiting for book…</div>';
    }
  } else {
    const asks = [...(book.asks || [])].sort((a, b) => b.price - a.price).slice(0, 10);
    const bids = [...(book.bids || [])].sort((a, b) => b.price - a.price).slice(0, 10);
    const maxSize = Math.max(...asks.map(l => l.size), ...bids.map(l => l.size), 1);
    let askTotal = 0;
    let bidTotal = 0;
    html = '<div class="book-section asks-section">';
    asks.forEach(l => { askTotal += l.size; html += bookRow(l, maxSize, "ask", askTotal); });
    html += '</div>';
    html += `<div class="book-spread-row">
      <span class="spread-lbl">Spread</span>
      <span class="spread-val">${fmt(book.spread)}</span>
    </div>`;
    html += '<div class="book-section bids-section">';
    bids.forEach(l => { bidTotal += l.size; html += bookRow(l, maxSize, "bid", bidTotal); });
    html += '</div>';
  }
  if (_renderedActiveBook !== html) {
    body.innerHTML = html;
    _renderedActiveBook = html;
  }
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

function tapeKey(t) {
  return t.slug || `${t.t}_${t.side}`;
}

function tapeRowHTML(t) {
  const won = !!t.won;
  const pnl = tradePnl(t);
  const time = new Date(t.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return `<div class="tape-row">
    <span class="tape-time">${time}</span>
    <span class="tape-side ${won ? "buy" : "sell"}">${won ? "WIN" : "LOSS"}</span>
    <span class="tape-outcome ${(t.side || "").toLowerCase()}">${String(t.side || "").toUpperCase()}</span>
    <span class="tape-price">@${Number(t.entry_price).toFixed(2)} $${Number(t.stake).toFixed(2)}</span>
    <span class="tape-size" style="color:${won ? "#0ecb81" : "#f6465d"}">${won ? "+" : "−"}$${Math.abs(pnl).toFixed(2)}</span>
  </div>`;
}

function bindTapeScroll(el) {
  if (el === _tapeEl) return;
  _tapeEl = el;
  _tapeKeys = [];
  el.addEventListener("scroll", () => {
    if (_tapeLimit >= liveStrategyTrades.length) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) {
      _tapeLimit += TAPE_BATCH;
      updateTape();
    }
  }, { passive: true });
}

function updateTape() {
  const el = document.getElementById("trade-tape");
  if (!el) return;
  bindTapeScroll(el);

  const strategyTrades = liveStrategyTrades;
  const ct = `${strategyTrades.length} trade${strategyTrades.length === 1 ? "" : "s"}`;
  setText("tape-count", ct);

  if (!strategyTrades.length) {
    if (_tapeKeys.length || !el.querySelector(".placeholder")) {
      el.innerHTML = '<div class="placeholder">Monitor active — waiting for first signal entry…</div>';
      _tapeKeys = [];
      _tapeLimit = TAPE_BATCH;
    }
    return;
  }

  const desc = [...strategyTrades].reverse();
  const desired = desc.slice(0, Math.min(_tapeLimit, desc.length));
  const desiredKeys = desired.map(tapeKey);

  if (desiredKeys.length === _tapeKeys.length && desiredKeys.every((k, i) => k === _tapeKeys[i])) {
    return;
  }

  const ph = el.querySelector(".placeholder");
  if (ph) ph.remove();

  let offset = _tapeKeys.length ? desiredKeys.indexOf(_tapeKeys[0]) : -1;
  let reusable = offset >= 0;
  if (reusable) {
    const overlap = Math.min(_tapeKeys.length, desiredKeys.length - offset);
    for (let i = 0; i < overlap; i++) {
      if (desiredKeys[offset + i] !== _tapeKeys[i]) { reusable = false; break; }
    }
  }

  if (!reusable) {
    el.innerHTML = desired.map(tapeRowHTML).join("");
    _tapeKeys = desiredKeys;
    return;
  }

  const oldLen = _tapeKeys.length;
  const keepOld = Math.max(0, Math.min(oldLen, desiredKeys.length - offset));
  const prependCount = offset;
  const newTailCount = desiredKeys.length - offset - keepOld;

  if (prependCount > 0) {
    const prevHeight = el.scrollHeight;
    const prevScroll = el.scrollTop;
    el.insertAdjacentHTML("afterbegin", desired.slice(0, prependCount).map(tapeRowHTML).join(""));
    const delta = el.scrollHeight - prevHeight;
    if (prevScroll > 0) el.scrollTop = prevScroll + delta;
  }
  if (newTailCount > 0) {
    el.insertAdjacentHTML("beforeend", desired.slice(offset + keepOld).map(tapeRowHTML).join(""));
  }
  for (let i = oldLen; i > keepOld; i--) {
    if (el.lastElementChild) el.removeChild(el.lastElementChild);
  }

  _tapeKeys = desiredKeys;
}

function updateLog(entries) {
  const el = document.getElementById("activity-log");
  if (!el) return;
  let html;
  if (!entries?.length) {
    html = '<div class="placeholder">No activity</div>';
  } else {
    html = entries.slice(0, 30).map(e => {
      const lvl = (e.level || "info").toLowerCase();
      return `<div class="activity-item ${lvl}">
        <span class="activity-dot"></span>
        <span class="activity-time">${e.time || ""}</span>
        <span class="activity-msg">${esc(e.message)}</span>
      </div>`;
    }).join("");
  }
  if (_lastLogHTML !== html) { el.innerHTML = html; _lastLogHTML = html; }
}

function updateBtcPanel(btc) {
  if (!btc) return;
  lastBtcSnapshot = btc;

  const prevBeat = priceToBeat;
  if (btc.price_to_beat != null) {
    const nextBeat = Number(btc.price_to_beat);
    // Only adopt beat changes that look like a real open lock — ignore noise.
    if (priceToBeat == null || Math.abs(nextBeat - priceToBeat) > 1e-9) {
      priceToBeat = nextBeat;
      if (prevBeat !== priceToBeat) recomputeBtcDeltas();
    }
  }

  const beatPrefix = btc.beat_estimated ? "Beat ~" : "Beat";
  const beatText = btc.price_to_beat != null ? `${beatPrefix} ${fmtUsd(btc.price_to_beat)}` : "Beat —";

  const spot = currentBtcSpot(btc);
  const binanceAge = spot.binanceAge;
  const livePrice = spot.value;
  if (livePrice != null) {
    setText("btc-spot", fmtUsd(livePrice));
    if (setText("hero-btc-price", fmtUsd(livePrice))) flashEl("hero-btc-price");
  }
  if (btc.price_to_beat != null) {
    setText("btc-beat", beatText);
    setText("hero-btc-beat", beatText);
  }

  // Seed is also applied in applySnapshot after history merge.

  let delta = null;
  if (priceToBeat != null && livePrice != null) {
    delta = Number(livePrice) - priceToBeat;
  } else if (btc.delta != null) {
    delta = Number(btc.delta);
  }

  const deltaText = delta != null
    ? `${delta >= 0 ? "▲" : "▼"} ${fmtUsd(Math.abs(delta))}${
        priceToBeat ? ` (${(Math.abs(delta) / priceToBeat * 100).toFixed(3)}%)` : ""
      }`
    : "Δ —";
  const deltaCls = delta != null ? (delta >= 0 ? "up" : "down") : "";

  if (delta != null) {
    smooth.btcDelta = lerp(smooth.btcDelta, delta, 0.45);
    const stale = spot.source === "chainlink" ? " · oracle fallback"
      : (spot.source === "stale-binance" || binanceAge > 3 ? " · lag" : "");
    setText("btc-chart-meta", `${fmtDelta(delta)} · ${chartWindowLabel()}${stale}`);
    setClass("btc-chart-meta", deltaCls);
  } else if (livePrice != null) {
    setText("btc-chart-meta", fmtUsd(livePrice));
  } else {
    setText("btc-chart-meta", "—");
  }

  for (const id of ["btc-delta", "hero-btc-delta"]) {
    setText(id, deltaText);
    setClass(id, deltaCls);
  }
  needsRedraw = true;
}

function updateBotButtons(running) {
  const start = document.querySelector(".btn-start");
  const stop = document.querySelector(".btn-stop");
  if (start) {
    if (start.disabled !== !!running) start.disabled = running;
    start.classList.toggle("disabled", !!running);
  }
  if (stop) {
    if (stop.disabled === !!running) stop.disabled = !running;
    stop.classList.toggle("disabled", !running);
  }
}

// Canvas context cache — avoid re-creating on every frame
let _canvasCache = {};

function setupCanvas(canvas) {
  if (!canvas) return null;
  const key = canvas.id || canvas;
  const cached = _canvasCache[key];
  if (cached && cached.w === canvas.parentElement?.clientWidth && cached.h === canvas.parentElement?.clientHeight) {
    return cached;
  }
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
  const result = { ctx, w, h, _key: key };
  _canvasCache[key] = result;
  return result;
}

function clearCanvasCache() {
  _canvasCache = {};
}

function chartNow() {
  const oddsT = history.length ? history[history.length - 1].t : 0;
  const btcT = btcHistory.length ? btcHistory[btcHistory.length - 1].t : 0;
  return Math.max(lastServerTs || 0, oddsT, btcT, Date.now() / 1000 - 0.5);
}

function chartWindowSec() {
  // Locked to candle duration (300s). Never changes once the candle is known.
  return CANDLE_SEC;
}

function chartWindowLabel() {
  return "5m";
}

// Y-ranges computed each frame so charts auto-scale to latest data
function clearCachedYRanges() {}

function getPriceYRange(candleStart, history) {
  const vals = [];
  for (const p of history) {
    if (p.up != null) vals.push(p.up);
    if (p.down != null) vals.push(p.down);
  }
  let yMin = 0;
  let yMax = 1;
  if (vals.length >= 2) {
    yMin = Math.max(0, Math.min(...vals) - 0.05);
    yMax = Math.min(1, Math.max(...vals) + 0.05);
    if (yMax - yMin < 0.10) {
      const mid = (yMax + yMin) / 2;
      yMin = Math.max(0, mid - 0.05);
      yMax = Math.min(1, mid + 0.05);
    }
  } else {
    yMin = 0;
    yMax = 1;
  }
  return { yMin, yMax };
}

function getBtcYRange(candleStart, btcHistory, beat) {
  const vals = [];
  for (const p of btcHistory) {
    if (p.v != null) vals.push(p.v);
  }
  if (beat != null) vals.push(beat);
  if (vals.length < 2) {
    return { yMin: (beat || 78000) - 200, yMax: (beat || 78000) + 200 };
  }
  let yMin = Math.min(...vals);
  let yMax = Math.max(...vals);
  const span = yMax - yMin || 1;
  const padY = Math.max(span * 0.10, beat != null ? Math.max(20, beat * 0.0003) : 20);
  yMin -= padY;
  yMax += padY;
  return { yMin, yMax };
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
  const now = chartNow();
  const tMin = candleWindowStart();
  const tMax = Math.max(tMin + CANDLE_SEC, now + 2);
  const windowSec = tMax - tMin;

  let visible = history.filter(p => p.up != null && p.down != null);
  if (visible.length < 2) {
    ctx.fillStyle = "#06090f";
    ctx.fillRect(0, 0, W, H);
    setText("chart-meta", "—");
    return true;
  }

  const candleStart = tMin;
  const yRange = getPriceYRange(candleStart, history);

  const xS = t => pad.l + ((t - tMin) / windowSec) * plotW;
  const yS = v => pad.t + plotH - ((v - yRange.yMin) / (yRange.yMax - yRange.yMin)) * plotH;

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
    ctx.fillText((yRange.yMax - ((yRange.yMax - yRange.yMin) / 4) * i).toFixed(2), pad.l - 4, y + 3);
  }

  if (yRange.yMin < 0.5 && yRange.yMax > 0.5) {
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
  setText("chart-meta", `Δ ${fmt(last.up - last.down)} · ${chartWindowLabel()}`);
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

function drawBtcChart() {
  const canvas = document.getElementById("btc-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;
  const pad = { l: 62, r: 12, t: 12, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  const now = chartNow();
  const tMin = candleWindowStart();
  const tMax = Math.max(tMin + CANDLE_SEC, now + 2);
  const windowSec = tMax - tMin;

  let visible = btcHistory.filter(p => p.v != null).map(p => ({ t: p.t, v: Number(p.v) }));

  if (visible.length === 1) {
    visible = [
      { t: Math.max(tMin, visible[0].t - 1), v: visible[0].v },
      visible[0],
    ];
  }

  if (visible.length < 2) {
    return true;
  }

  const beat = priceToBeat != null ? Number(priceToBeat) : null;
  const candleStart = tMin;
  const yRange = getBtcYRange(candleStart, btcHistory, beat);

  const xS = t => pad.l + ((t - tMin) / windowSec) * plotW;
  const yS = price => pad.t + plotH - ((price - yRange.yMin) / (yRange.yMax - yRange.yMin)) * plotH;

  if (beat != null) {
    const yBeat = yS(beat);
    ctx.fillStyle = "rgba(14, 203, 129, 0.05)";
    ctx.fillRect(pad.l, pad.t, plotW, Math.max(0, yBeat - pad.t));
    ctx.fillStyle = "rgba(246, 70, 93, 0.05)";
    ctx.fillRect(pad.l, yBeat, plotW, pad.t + plotH - yBeat);
    ctx.strokeStyle = "rgba(240, 185, 11, 0.75)";
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(pad.l, yBeat);
    ctx.lineTo(W - pad.r, yBeat);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(240, 185, 11, 0.9)";
    ctx.textAlign = "left";
    ctx.font = "9px JetBrains Mono, monospace";
    ctx.fillText(`Beat ${fmtUsdCompact(beat)}`, pad.l + 4, yBeat - 5);
  }

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
    const tickVal = yRange.yMax - ((yRange.yMax - yRange.yMin) / 4) * i;
    ctx.fillText(fmtUsdCompact(tickVal), pad.l - 4, y + 3);
  }

  const last = visible[visible.length - 1];
  const lineColor = beat != null && last.v >= beat ? "#0ecb81" : "#f6465d";

  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    const y = yS(p.v);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = lineColor;
  ctx.beginPath();
  ctx.arc(xS(last.t), yS(last.v), 3.5, 0, Math.PI * 2);
  ctx.fill();

  return true;
}

function drawSimChart() {
  const canvas = document.getElementById("sim-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;
  const pad = { l: 48, r: 12, t: 12, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  const now = chartNow();
  const tMin = candleWindowStart();
  const tMax = Math.max(tMin + CANDLE_SEC, now + 2);
  const windowSec = tMax - tMin;

  let visible = simHistory.filter(p => p.v != null).map(p => ({ t: p.t, v: Number(p.v) }));

  if (!visible.length && lastBtcSnapshot && lastBtcSnapshot.sim_prob != null) {
    visible.push({ t: now, v: Number(lastBtcSnapshot.sim_prob) });
  }

  if (visible.length === 1) {
    visible = [
      { t: Math.max(tMin, visible[0].t - 1), v: visible[0].v },
      visible[0],
    ];
  }

  if (visible.length < 2) {
    setText("sim-spot-prob", "P(Up) —");
    setText("sim-chart-meta", "Waiting for data…");
    return true;
  }

  const yMin = 0.0;
  const yMax = 1.0;

  const xS = t => pad.l + ((t - tMin) / windowSec) * plotW;
  const yS = prob => pad.t + plotH - ((prob - yMin) / (yMax - yMin)) * plotH;

  // Grid lines: 0%, 25%, 50%, 75%, 100%
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
    const tickVal = 1.0 - (i / 4.0);
    ctx.fillText(`${(tickVal * 100).toFixed(0)}%`, pad.l - 4, y + 3);
  }

  // 50% threshold line (dotted)
  const y50 = yS(0.5);
  ctx.strokeStyle = "rgba(0, 242, 254, 0.35)";
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(pad.l, y50);
  ctx.lineTo(W - pad.r, y50);
  ctx.stroke();
  ctx.setLineDash([]);

  // Gradient area fill
  const areaGrad = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  areaGrad.addColorStop(0, "rgba(0, 242, 254, 0.22)");
  areaGrad.addColorStop(1, "rgba(79, 172, 254, 0.01)");
  ctx.fillStyle = areaGrad;
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    const y = yS(p.v);
    if (i === 0) {
      ctx.moveTo(x, pad.t + plotH);
      ctx.lineTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  const lastX = xS(visible[visible.length - 1].t);
  ctx.lineTo(lastX, pad.t + plotH);
  ctx.closePath();
  ctx.fill();

  // Vibrant line
  const lineGrad = ctx.createLinearGradient(pad.l, 0, W - pad.r, 0);
  lineGrad.addColorStop(0, "#00f2fe");
  lineGrad.addColorStop(1, "#4facfe");

  ctx.strokeStyle = lineGrad;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    const y = yS(p.v);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // End point pulse dot
  const last = visible[visible.length - 1];
  const lastProb = last.v;
  ctx.fillStyle = "#00f2fe";
  ctx.beginPath();
  ctx.arc(xS(last.t), yS(lastProb), 3.5, 0, Math.PI * 2);
  ctx.fill();

  setText("sim-spot-prob", `P(Up) ${(lastProb * 100).toFixed(1)}%`);
  setText("sim-chart-meta", `${getActiveSimLabel()} · ${chartWindowLabel()}`);

  return true;
}

function initEquityChartInteractivity() {
  if (equityChartEventsBound) return;
  const canvas = document.getElementById("live-equity-chart");
  const container = document.getElementById("equity-chart-container");
  if (!canvas || !container) return;

  equityChartEventsBound = true;

  const btnIn = document.getElementById("eq-zoom-in");
  if (btnIn) btnIn.addEventListener("click", () => zoomEquityChart(0.75, 0.5));

  const btnOut = document.getElementById("eq-zoom-out");
  if (btnOut) btnOut.addEventListener("click", () => zoomEquityChart(1.33, 0.5));

  const btnReset = document.getElementById("eq-zoom-reset");
  if (btnReset) btnReset.addEventListener("click", () => resetEquityZoom());

  container.addEventListener("wheel", e => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const padL = 58, padR = 16;
    const plotW = rect.width - padL - padR;
    const mouseX = e.clientX - rect.left;
    let ratio = 0.5;
    if (plotW > 0) {
      ratio = Math.max(0.05, Math.min(0.95, (mouseX - padL) / plotW));
    }
    const factor = e.deltaY < 0 ? 0.8 : 1.25;
    zoomEquityChart(factor, ratio);
  }, { passive: false });

  container.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    eqDragState.isDragging = true;
    eqDragState.startX = e.clientX;
    const allT = equityHistory.map(p => Number(p.t)).filter(Number.isFinite);
    if (allT.length) {
      const fullMin = allT[0];
      const fullMax = allT[allT.length - 1];
      eqDragState.startMin = eqViewState.isUserZoomed ? eqViewState.xMin : fullMin;
      eqDragState.startMax = eqViewState.isUserZoomed ? eqViewState.xMax : Math.max(fullMax, fullMin + 1);
    }
  });

  window.addEventListener("mousemove", e => {
    const rect = canvas.getBoundingClientRect();
    const inBounds = e.clientX >= rect.left && e.clientX <= rect.right &&
                     e.clientY >= rect.top && e.clientY <= rect.bottom;

    if (eqDragState.isDragging) {
      const dx = e.clientX - eqDragState.startX;
      const padL = 58, padR = 16;
      const plotW = rect.width - padL - padR;
      if (plotW > 0 && eqDragState.startMin != null && eqDragState.startMax != null) {
        const span = eqDragState.startMax - eqDragState.startMin;
        const dt = -(dx / plotW) * span;
        const allT = equityHistory.map(p => Number(p.t)).filter(Number.isFinite);
        const fullMax = allT.length ? allT[allT.length - 1] : 0;

        eqViewState.xMin = eqDragState.startMin + dt;
        eqViewState.xMax = eqDragState.startMax + dt;
        eqViewState.isUserZoomed = true;
        eqViewState.autoScroll = (eqViewState.xMax >= fullMax - 2);
        needsRedraw = true;
      }
    }

    if (inBounds) {
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;
      eqHoverState.active = true;
      eqHoverState.x = mouseX;
      eqHoverState.y = mouseY;
      needsRedraw = true;
    } else if (!eqDragState.isDragging && eqHoverState.active) {
      eqHoverState.active = false;
      const tooltip = document.getElementById("equity-tooltip");
      if (tooltip) tooltip.classList.add("hidden");
      needsRedraw = true;
    }
  });

  window.addEventListener("mouseup", () => {
    if (eqDragState.isDragging) {
      eqDragState.isDragging = false;
    }
  });

  container.addEventListener("mouseleave", () => {
    if (!eqDragState.isDragging) {
      eqHoverState.active = false;
      const tooltip = document.getElementById("equity-tooltip");
      if (tooltip) tooltip.classList.add("hidden");
      needsRedraw = true;
    }
  });

  container.addEventListener("dblclick", () => {
    resetEquityZoom();
  });

  let lastTouchDist = 0;
  container.addEventListener("touchstart", e => {
    if (e.touches.length === 1) {
      eqDragState.isDragging = true;
      eqDragState.startX = e.touches[0].clientX;
      const allT = equityHistory.map(p => Number(p.t)).filter(Number.isFinite);
      if (allT.length) {
        const fullMin = allT[0];
        const fullMax = allT[allT.length - 1];
        eqDragState.startMin = eqViewState.isUserZoomed ? eqViewState.xMin : fullMin;
        eqDragState.startMax = eqViewState.isUserZoomed ? eqViewState.xMax : Math.max(fullMax, fullMin + 1);
      }
    } else if (e.touches.length === 2) {
      lastTouchDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
    }
  }, { passive: true });

  container.addEventListener("touchmove", e => {
    if (e.touches.length === 1 && eqDragState.isDragging) {
      const dx = e.touches[0].clientX - eqDragState.startX;
      const rect = canvas.getBoundingClientRect();
      const padL = 58, padR = 16;
      const plotW = rect.width - padL - padR;
      if (plotW > 0 && eqDragState.startMin != null) {
        const span = eqDragState.startMax - eqDragState.startMin;
        const dt = -(dx / plotW) * span;
        const allT = equityHistory.map(p => Number(p.t)).filter(Number.isFinite);
        const fullMax = allT.length ? allT[allT.length - 1] : 0;
        eqViewState.xMin = eqDragState.startMin + dt;
        eqViewState.xMax = eqDragState.startMax + dt;
        eqViewState.isUserZoomed = true;
        eqViewState.autoScroll = (eqViewState.xMax >= fullMax - 2);
        needsRedraw = true;
      }
    } else if (e.touches.length === 2) {
      const dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      if (lastTouchDist > 0) {
        const factor = lastTouchDist / dist;
        zoomEquityChart(factor, 0.5);
      }
      lastTouchDist = dist;
    }
  }, { passive: true });

  container.addEventListener("touchend", () => {
    eqDragState.isDragging = false;
    lastTouchDist = 0;
  });
}

function zoomEquityChart(factor, targetRatio = 0.5) {
  if (!equityHistory.length) return;
  const allT = equityHistory.map(p => Number(p.t)).filter(Number.isFinite);
  if (!allT.length) return;
  const fullMin = allT[0];
  const fullMax = allT[allT.length - 1];
  let curMin = eqViewState.isUserZoomed ? eqViewState.xMin : fullMin;
  let curMax = eqViewState.isUserZoomed ? eqViewState.xMax : Math.max(fullMax, fullMin + 1);

  let span = curMax - curMin;
  let newSpan = Math.max(10, span * factor);
  if (newSpan >= (fullMax - fullMin) * 1.5) {
    resetEquityZoom();
    return;
  }
  let targetT = curMin + span * targetRatio;
  let newMin = targetT - newSpan * targetRatio;
  let newMax = targetT + newSpan * (1 - targetRatio);

  eqViewState.xMin = newMin;
  eqViewState.xMax = newMax;
  eqViewState.isUserZoomed = true;
  eqViewState.autoScroll = (newMax >= fullMax - 5);
  needsRedraw = true;
}

function initLightweightEquityChart() {
  const container = document.getElementById("equity-chart-container");
  if (!container || lwEquityChart) return;

  const oldCanvas = document.getElementById("live-equity-chart");
  if (oldCanvas) oldCanvas.style.display = "none";

  const chartDiv = document.createElement("div");
  chartDiv.id = "lw-equity-chart-div";
  chartDiv.style.width = "100%";
  chartDiv.style.height = "100%";
  chartDiv.style.position = "absolute";
  chartDiv.style.top = "0";
  chartDiv.style.left = "0";
  chartDiv.style.right = "0";
  chartDiv.style.bottom = "0";
  container.appendChild(chartDiv);

  lwEquityChart = LightweightCharts.createChart(chartDiv, {
    layout: {
      background: { type: 'solid', color: '#06090f' },
      textColor: '#94a3b8',
      fontSize: 11,
      fontFamily: 'Inter, system-ui, -apple-system, sans-serif',
    },
    grid: {
      vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
      horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: {
        color: 'rgba(99, 102, 241, 0.5)',
        width: 1,
        style: LightweightCharts.LineStyle.Dashed,
        labelBackgroundColor: '#6366f1',
      },
      horzLine: {
        color: 'rgba(99, 102, 241, 0.5)',
        width: 1,
        style: LightweightCharts.LineStyle.Dashed,
        labelBackgroundColor: '#6366f1',
      },
    },
    rightPriceScale: {
      borderColor: 'rgba(255, 255, 255, 0.08)',
      scaleMargins: { top: 0.15, bottom: 0.15 },
      autoScale: true,
    },
    timeScale: {
      borderColor: 'rgba(255, 255, 255, 0.08)',
      timeVisible: true,
      secondsVisible: true,
      rightOffset: 5,
    },
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: true,
    },
    handleScale: {
      axisPressedMouseMove: true,
      mouseWheel: true,
      pinch: true,
    },
  });

  lwEquitySeries = lwEquityChart.addAreaSeries({
    topColor: 'rgba(99, 102, 241, 0.4)',
    bottomColor: 'rgba(99, 102, 241, 0.02)',
    lineColor: '#6366f1',
    lineWidth: 2,
    priceFormat: {
      type: 'price',
      precision: 2,
      minMove: 0.01,
    },
  });

  const resizeObserver = new ResizeObserver(entries => {
    if (!entries || !entries.length) return;
    const { width, height } = entries[0].contentRect;
    if (width > 0 && height > 0) {
      lwEquityChart.applyOptions({ width, height });
    }
  });
  resizeObserver.observe(container);

  const btnReset = document.getElementById("eq-zoom-reset");
  if (btnReset) {
    btnReset.addEventListener("click", () => {
      resetEquityZoom();
    });
  }
}

function updateLightweightEquityChart() {
  if (!lwEquityChart) {
    if (window.LightweightCharts) {
      initLightweightEquityChart();
    }
  }
  if (!lwEquitySeries) return;

  if (!equityHistory.length) {
    lwEquitySeries.setData([]);
    return;
  }

  const pointsByTime = new Map();
  for (const p of equityHistory) {
    if (p && Number.isFinite(Number(p.t)) && Number.isFinite(Number(p.v))) {
      const t = Math.floor(Number(p.t));
      const v = Number(p.v);
      pointsByTime.set(t, { time: t, value: v });
    }
  }

  const chartData = [...pointsByTime.values()].sort((a, b) => a.time - b.time);
  lwEquitySeries.setData(chartData);

  if (chartData.length >= 2) {
    const firstVal = chartData[0].value;
    const lastVal = chartData[chartData.length - 1].value;
    const isUp = lastVal >= firstVal;
    lwEquitySeries.applyOptions({
      lineColor: isUp ? '#10b981' : '#ef4444',
      topColor: isUp ? 'rgba(16, 185, 129, 0.4)' : 'rgba(239, 68, 68, 0.4)',
      bottomColor: isUp ? 'rgba(16, 185, 129, 0.02)' : 'rgba(239, 68, 68, 0.02)',
    });
  }

  if (liveStrategyTrades && liveStrategyTrades.length) {
    const markers = [];
    for (const tr of liveStrategyTrades) {
      if (tr && Number.isFinite(Number(tr.t))) {
        const trTime = Math.floor(Number(tr.t));
        const won = !!tr.won;
        const pnl = tradePnl(tr);
        markers.push({
          time: trTime,
          position: won ? "belowBar" : "aboveBar",
          color: won ? "#10b981" : "#ef4444",
          shape: won ? "arrowUp" : "arrowDown",
          text: won ? `+${fmtUsd(pnl)}` : `-${fmtUsd(Math.abs(pnl))}`,
        });
      }
    }
    markers.sort((a, b) => a.time - b.time);
    try {
      lwEquitySeries.setMarkers(markers);
    } catch (_) {}
  }
}

function resetEquityZoom() {
  if (lwEquityChart) {
    lwEquityChart.timeScale().fitContent();
  }
  eqViewState.isUserZoomed = false;
  eqViewState.autoScroll = true;
  eqViewState.xMin = null;
  eqViewState.xMax = null;
  needsRedraw = true;
}

function drawEquityChart() {
  if (window.LightweightCharts) {
    initLightweightEquityChart();
    updateLightweightEquityChart();
    return true;
  }
  initEquityChartInteractivity();
  const canvas = document.getElementById("live-equity-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;
  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  const visible = equityHistory
    .filter(p => Number.isFinite(Number(p.t)) && Number.isFinite(Number(p.v)))
    .map(p => ({ ...p, t: Number(p.t), v: Number(p.v) }));
  if (!visible.length) return true;

  const pad = { l: 58, r: 16, t: 28, b: 24 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const first = visible[0];
  const last = visible[visible.length - 1];
  const fullMin = first.t;
  const fullMax = Math.max(last.t, fullMin + 1);

  let tMin, tMax;
  if (!eqViewState.isUserZoomed || eqViewState.autoScroll) {
    tMin = fullMin;
    tMax = fullMax;
  } else {
    tMin = eqViewState.xMin != null ? eqViewState.xMin : fullMin;
    tMax = eqViewState.xMax != null ? eqViewState.xMax : fullMax;
  }

  const initial = Number.isFinite(equityInitial) ? equityInitial : Number(first.v);
  const inView = visible.filter(p => p.t >= tMin - 60 && p.t <= tMax + 60);
  const chartPoints = inView.length ? inView : visible;

  const vals = [...chartPoints.map(p => p.v), initial];
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const spread = Math.max(hi - lo, Math.max(Math.abs(hi) * 0.01, 0.01));
  const yMin = lo - spread * 0.15;
  const yMax = hi + spread * 0.15;

  const xS = t => pad.l + ((t - tMin) / (tMax - tMin)) * plotW;
  const yS = v => pad.t + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  // Grid
  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono, monospace";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    ctx.fillText(fmtUsdCompact(yMax - ((yMax - yMin) / 4) * i), pad.l - 4, y + 3);
  }

  const positive = Number(last.v) >= Number(first.v);
  const color = positive ? "#0ecb81" : "#f6465d";
  const baseline = Math.max(pad.t, Math.min(pad.t + plotH, yS(initial)));

  // Area fill gradient
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  if (positive) {
    grad.addColorStop(0, "rgba(14, 203, 129, 0.22)");
    grad.addColorStop(1, "rgba(14, 203, 129, 0.0)");
  } else {
    grad.addColorStop(0, "rgba(246, 70, 93, 0.22)");
    grad.addColorStop(1, "rgba(246, 70, 93, 0.0)");
  }

  ctx.fillStyle = grad;
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    const y = yS(p.v);
    if (!i) ctx.moveTo(x, y);
    else {
      const prev = visible[i - 1];
      ctx.lineTo(x, yS(prev.v));
      ctx.lineTo(x, y);
    }
  });
  ctx.lineTo(xS(last.t), baseline); ctx.lineTo(xS(first.t), baseline); ctx.closePath(); ctx.fill();

  // Equity step stroke line
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    const y = yS(p.v);
    if (!i) ctx.moveTo(x, y);
    else {
      const prev = visible[i - 1];
      ctx.lineTo(x, yS(prev.v));
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();

  // Last point marker
  if (xS(last.t) >= pad.l && xS(last.t) <= W - pad.r) {
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(xS(last.t), yS(last.v), 4, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1; ctx.stroke();
  }

  // Draw Trade Markers taken by the engine
  let hoveredTrade = null;
  let minDist = 22;

  liveStrategyTrades.forEach(tr => {
    const trT = Number(tr.t);
    if (isNaN(trT) || trT < tMin || trT > tMax) return;
    const x = xS(trT);
    if (x < pad.l - 5 || x > W - pad.r + 5) return;

    let eqVal = Number(tr.equity_after);
    if (!Number.isFinite(eqVal)) {
      const closest = visible.reduce((best, p) => Math.abs(p.t - trT) < Math.abs(best.t - trT) ? p : best, visible[0]);
      eqVal = closest ? closest.v : initial;
    }
    const y = Math.max(pad.t + 10, Math.min(pad.t + plotH - 10, yS(eqVal)));

    const won = Boolean(tr.won);
    const side = String(tr.side || "UP").toUpperCase();
    const pnl = tradePnl(tr);
    const pnlStr = pnl !== 0 ? `${pnl > 0 ? "+" : ""}$${pnl.toFixed(1)}` : "";

    const badgeColor = won ? "#0ecb81" : "#f6465d";
    const badgeBg = won ? "rgba(14, 203, 129, 0.9)" : "rgba(246, 70, 93, 0.9)";
    const arrow = won ? "▲" : "▼";
    const label = `${arrow} ${side} ${pnlStr}`;

    ctx.font = "bold 9px JetBrains Mono, monospace";
    const tw = ctx.measureText(label).width;
    const bw = tw + 8;
    const bh = 15;
    const bx = x - bw / 2;
    const by = won ? y - 20 : y + 6;

    ctx.strokeStyle = badgeColor;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x, by + (won ? bh : 0));
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = badgeBg;
    ctx.beginPath();
    ctx.roundRect(bx, by, bw, bh, 3);
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.fillStyle = "#ffffff";
    ctx.textAlign = "center";
    ctx.fillText(label, x, by + 11);

    if (eqHoverState.active) {
      const dist = Math.hypot(eqHoverState.x - x, eqHoverState.y - (by + bh / 2));
      if (dist < minDist) {
        minDist = dist;
        hoveredTrade = tr;
      }
    }
  });

  // Crosshair & Tooltip
  const tooltip = document.getElementById("equity-tooltip");
  if (eqHoverState.active) {
    const mouseX = Math.max(pad.l, Math.min(W - pad.r, eqHoverState.x));
    const mouseY = Math.max(pad.t, Math.min(H - pad.b, eqHoverState.y));

    ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(mouseX, pad.t); ctx.lineTo(mouseX, H - pad.b); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(pad.l, mouseY); ctx.lineTo(W - pad.r, mouseY); ctx.stroke();
    ctx.setLineDash([]);

    const hoverT = tMin + ((mouseX - pad.l) / plotW) * (tMax - tMin);
    const hoverVal = yMax - ((mouseY - pad.t) / plotH) * (yMax - yMin);

    const tStr = new Date(hoverT * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    ctx.fillStyle = "#1e293b";
    ctx.fillRect(mouseX - 26, H - pad.b, 52, 16);
    ctx.strokeStyle = "#475569"; ctx.strokeRect(mouseX - 26, H - pad.b, 52, 16);
    ctx.fillStyle = "#f8fafc"; ctx.font = "9px JetBrains Mono, monospace"; ctx.textAlign = "center";
    ctx.fillText(tStr, mouseX, H - pad.b + 11);

    const valStr = fmtUsdCompact(hoverVal);
    ctx.fillStyle = "#1e293b";
    ctx.fillRect(0, mouseY - 8, pad.l - 2, 16);
    ctx.strokeStyle = "#475569"; ctx.strokeRect(0, mouseY - 8, pad.l - 2, 16);
    ctx.fillStyle = "#f8fafc"; ctx.textAlign = "right";
    ctx.fillText(valStr, pad.l - 4, mouseY + 4);

    if (tooltip) {
      tooltip.classList.remove("hidden");
      const container = document.getElementById("equity-chart-container");
      const containerW = container ? container.clientWidth : W;

      if (hoveredTrade) {
        const won = Boolean(hoveredTrade.won);
        const pnl = tradePnl(hoveredTrade);
        const eqAfter = Number(hoveredTrade.equity_after);
        tooltip.innerHTML = `
          <div class="eq-tooltip-header">
            <span>TRADE SETTLED</span>
            <span class="eq-tooltip-badge ${won ? "win" : "loss"}">${won ? "WIN" : "LOSS"} ${String(hoveredTrade.side || "").toUpperCase()}</span>
          </div>
          <div class="eq-tooltip-row">
            <span class="eq-tooltip-label">Market</span>
            <span class="eq-tooltip-val">${esc(hoveredTrade.title || hoveredTrade.slug || "BTC 5M")}</span>
          </div>
          <div class="eq-tooltip-row">
            <span class="eq-tooltip-label">Entry / Stake</span>
            <span class="eq-tooltip-val">@${Number(hoveredTrade.entry_price || 0).toFixed(2)} (${hoveredTrade.size_label || ""} $${Number(hoveredTrade.stake || 0).toFixed(2)})</span>
          </div>
          <div class="eq-tooltip-row">
            <span class="eq-tooltip-label">PnL</span>
            <span class="eq-tooltip-val" style="color:${won ? '#0ecb81' : '#f6465d'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>
          </div>
          <div class="eq-tooltip-row">
            <span class="eq-tooltip-label">Equity After</span>
            <span class="eq-tooltip-val">$${Number.isFinite(eqAfter) ? eqAfter.toFixed(2) : "—"}</span>
          </div>
          ${hoveredTrade.reason ? `<div class="eq-tooltip-row"><span class="eq-tooltip-label">Signal</span><span class="eq-tooltip-val" style="font-size:9px;color:#94a3b8">${esc(hoveredTrade.reason)}</span></div>` : ""}
        `;
      } else {
        const closestPoint = visible.reduce((best, p) => Math.abs(p.t - hoverT) < Math.abs(best.t - hoverT) ? p : best, visible[0]);
        const dateStr = new Date(hoverT * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        tooltip.innerHTML = `
          <div class="eq-tooltip-header">
            <span>EQUITY POINT</span>
            <span style="color:#64748b">${dateStr}</span>
          </div>
          <div class="eq-tooltip-row">
            <span class="eq-tooltip-label">Balance</span>
            <span class="eq-tooltip-val" style="color:${color}">${closestPoint ? '$' + Number(closestPoint.v).toFixed(2) : '—'}</span>
          </div>
        `;
      }

      let tooltipX = mouseX + 12;
      if (tooltipX + 220 > containerW) tooltipX = mouseX - 230;
      let tooltipY = mouseY - 20;
      if (tooltipY < 10) tooltipY = 10;
      tooltip.style.left = `${Math.max(10, tooltipX)}px`;
      tooltip.style.top = `${tooltipY}px`;
    }
  } else if (tooltip) {
    tooltip.classList.add("hidden");
  }

  // Time labels
  ctx.fillStyle = "#5a6d8a";
  ctx.font = "9px JetBrains Mono, monospace";
  ctx.textAlign = "left";
  ctx.fillText(new Date(tMin * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), pad.l, H - 7);
  ctx.textAlign = "right";
  ctx.fillText(new Date(tMax * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), W - pad.r, H - 7);

  return true;
}

function drawEquityGridChart() {
  const canvas = document.getElementById("equity-grid-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;
  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  const visible = equityHistory
    .filter(p => Number.isFinite(Number(p.t)) && Number.isFinite(Number(p.v)))
    .map(p => ({ ...p, t: Number(p.t), v: Number(p.v) }));
  if (!visible.length) return true;

  const pad = { l: 52, r: 12, t: 12, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const first = visible[0];
  const last = visible[visible.length - 1];
  const tMin = first.t;
  const tMax = Math.max(last.t, tMin + 1);

  const initial = Number.isFinite(equityInitial) ? equityInitial : Number(first.v);
  const vals = [...visible.map(p => p.v), initial];
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const spread = Math.max(hi - lo, Math.max(Math.abs(hi) * 0.01, 0.1));
  const yMin = lo - spread * 0.15;
  const yMax = hi + spread * 0.15;

  const xS = t => pad.l + ((t - tMin) / (tMax - tMin)) * plotW;
  const yS = v => pad.t + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono, monospace";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    ctx.fillText(fmtUsdCompact(yMax - ((yMax - yMin) / 4) * i), pad.l - 4, y + 3);
  }

  const positive = Number(last.v) >= Number(first.v);
  const color = positive ? "#0ecb81" : "#f6465d";
  const baseline = Math.max(pad.t, Math.min(pad.t + plotH, yS(initial)));

  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  if (positive) {
    grad.addColorStop(0, "rgba(14, 203, 129, 0.22)");
    grad.addColorStop(1, "rgba(14, 203, 129, 0.0)");
  } else {
    grad.addColorStop(0, "rgba(246, 70, 93, 0.22)");
    grad.addColorStop(1, "rgba(246, 70, 93, 0.0)");
  }
  ctx.fillStyle = grad;
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    if (i === 0) { ctx.moveTo(x, yS(p.v)); return; }
    ctx.lineTo(x, yS(visible[i - 1].v));
    ctx.lineTo(x, yS(p.v));
  });
  ctx.lineTo(xS(last.t), baseline);
  ctx.lineTo(xS(first.t), baseline);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  visible.forEach((p, i) => {
    const x = xS(p.t);
    if (i === 0) ctx.moveTo(x, yS(p.v));
    else { ctx.lineTo(x, yS(visible[i - 1].v)); ctx.lineTo(x, yS(p.v)); }
  });
  ctx.stroke();

  const lastX = xS(last.t);
  if (lastX >= pad.l && lastX <= W - pad.r) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(lastX, yS(last.v), 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  liveStrategyTrades.forEach(tr => {
    const trT = Number(tr.t);
    if (isNaN(trT) || trT < tMin || trT > tMax) return;
    const x = xS(trT);
    if (x < pad.l - 5 || x > W - pad.r + 5) return;
    let eqVal = Number(tr.equity_after);
    if (!Number.isFinite(eqVal)) eqVal = initial;
    const y = Math.max(pad.t + 6, Math.min(pad.t + plotH - 6, yS(eqVal)));
    const won = Boolean(tr.won);
    const badgeColor = won ? "#0ecb81" : "#f6465d";
    ctx.fillStyle = badgeColor;
    ctx.beginPath();
    ctx.arc(x, y, 3.5, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = "#5a6d8a";
  ctx.font = "9px JetBrains Mono, monospace";
  ctx.textAlign = "left";
  ctx.fillText(new Date(tMin * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), pad.l, H - 7);
  ctx.textAlign = "right";
  ctx.fillText(new Date(tMax * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), W - pad.r, H - 7);
  return true;
}

function renderLoop() {
  if (viewActive) {
    const needsAnim = isLive || smooth.up != null || smooth.btcDelta != null || smooth.down != null;
    if (needsRedraw || needsAnim) {
      if (lastBtcSnapshot) {
        if (seedBtcLivePoint(lastBtcSnapshot)) needsRedraw = true;
      }
      drawPriceChart();
      drawBtcChart();
      drawSimChart();
      drawEquityGridChart();
      needsRedraw = false;
    }
  }
  if (viewActive) requestAnimationFrame(renderLoop);
}

// Pause rendering when tab is hidden to save CPU/battery
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    viewActive = false;
  } else {
    viewActive = true;
    clearCanvasCache();
    needsRedraw = true;
  }
});

function setText(id, val) {
  const el = document.getElementById(id);
  if (!el) return false;
  const next = val == null ? "" : String(val);
  if (el.textContent === next) return false;
  el.textContent = next;
  return true;
}

function tradePnl(t) {
  if (!t) return 0;
  if (t.pnl != null) return Number(t.pnl) || 0;
  const stake = Number(t.stake) || 0;
  const entry = Number(t.entry_price) || 0;
  const fee = Number(t.entry_fee) || 0;
  if (entry <= 0 || stake <= 0) return 0;
  return t.won ? (stake / entry - stake - fee) : (-stake - fee);
}

function startPing() {
  stopPing();
  pingTimer = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    lastPingAt = Date.now();
    try { ws.send(JSON.stringify({ type: "ping", t: lastPingAt })); } catch (_) {}
  }, 2000);
}

function stopPing() {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

function handlePong(d) {
  const sent = Number(d.t || lastPingAt);
  if (sent) lastRttMs = Date.now() - sent;
  if (lastRttMs != null) setText("net-text", `${Math.round(lastRttMs)}ms`);
  setClass("net-status", "status-item live");
}

function initNetWatch() {
  window.addEventListener("online", () => { netOnline = true; updateNetBanner(); });
  window.addEventListener("offline", () => { netOnline = false; updateNetBanner(); });
  updateNetBanner();
}

let _lastBannerText = "";
function updateNetBanner() {
  const banner = document.getElementById("net-banner");
  if (!banner) return;
  const down = !netOnline || (lastUpdateAt > 0 && !isLive);
  banner.classList.toggle("hidden", !down);
  if (down) {
    const t = !netOnline
      ? "No internet — live prices are frozen. The bot thread is still running on this machine."
      : "Dashboard disconnected — live prices are frozen. The bot thread is still running on the server.";
    if (_lastBannerText !== t) { banner.textContent = t; _lastBannerText = t; }
  } else {
    _lastBannerText = "";
  }
}

function initStrategyCollapse() {
  const panel = document.getElementById("live-model-panel");
  const toggle = document.getElementById("live-model-toggle");
  if (!panel || !toggle) return;
  panel.classList.add("collapsed");
  toggle.addEventListener("click", () => {
    panel.classList.toggle("collapsed");
    const btn = document.getElementById("live-model-collapse-btn");
    if (btn) btn.textContent = panel.classList.contains("collapsed") ? "Show" : "Hide";
  });
}

function initHealthPopover() {
  const btn = document.getElementById("health-toggle");
  const pop = document.getElementById("health-popover");
  if (!btn || !pop) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    pop.classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    if (!pop.classList.contains("hidden") && !pop.contains(e.target) && e.target !== btn) {
      pop.classList.add("hidden");
    }
  });
}

function updateHealthPopover(d) {
  const h = d.health || {};
  const set = (id, val) => setText(id, val);
  set("hp-uptime", h.uptime_sec != null ? formatUptime(h.uptime_sec) : "—");
  set("hp-lag", h.loop_lag_ms != null ? `${Number(h.loop_lag_ms).toFixed(1)} ms` : "—");
  set("hp-bot", (h.bot_alive || d.running) ? "alive" : "stopped");
  const feed = h.feed || d.feed || {};
  set("hp-clob", feed.connected ? `${feed.updates_per_sec ?? "—"}/s` : (feed.reconnecting ? "reconnecting" : "down"));
  const spot = h.spot || {};
  set("hp-spot", spot.connected ? (spot.age_sec != null ? `${spot.age_sec.toFixed(1)}s ago` : "live") : "down");
  const rpc = h.rpc || {};
  set("hp-rpc", rpc.ok ? `block ${rpc.block ?? "—"} · ${rpc.rtt_ms ?? "—"}ms` : (rpc.error || "down"));
  const wal = h.wallet || {};
  set("hp-wallet", wal.connected || wal.ok ? (wal.age_sec != null ? `${wal.age_sec.toFixed(1)}s ago` : "ready") : (wal.error || "off"));
}

function formatUptime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function initCopyButtons() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const id = btn.getAttribute("data-copy");
    const el = document.getElementById(id);
    const text = el ? el.textContent.trim() : "";
    if (!text || text === "—") return;
    if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = "Copy"; }, 1200);
  });
}

function setExplorerLinks(a) {
  const set = (id, addr) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (addr && addr.startsWith("0x")) {
      const href = `https://polygonscan.com/address/${addr}`;
      if (el.href !== href) el.href = href;
      if (el.classList.contains("hidden")) el.classList.remove("hidden");
    } else {
      el.removeAttribute("href");
      if (!el.classList.contains("hidden")) el.classList.add("hidden");
    }
  };
  set("wallet-funder-link", a.funder_address);
  set("wallet-signer-link", a.signer_address);
}

function renderOpenOrders(orders) {
  const el = document.getElementById("wallet-orders");
  if (!el) return;
  if (!orders.length) {
    const html = '<div class="placeholder">No open orders</div>';
    if (_renderedOpenOrders !== html) { el.innerHTML = html; _renderedOpenOrders = html; }
    return;
  }
  const html = orders.slice(0, 12).map(o => {
    const id = o.id || o.order_id || o.orderID || o.orderId || "";
    const side = String(o.side || o.outcome || "").toUpperCase();
    const price = o.price != null ? Number(o.price).toFixed(2)
      : o.pricePerShare != null ? (Number(o.pricePerShare) / 1e18).toFixed(2)
      : "—";
    const rawSize = o.original_size || o.originalSize || o.size || o.quantity || o.originalQuantity || 0;
    const size = typeof rawSize === "number" ? (rawSize >= 1e12 ? (rawSize / 1e18).toFixed(2) : rawSize.toFixed(2)) : String(rawSize);
    const rawMatched = o.size_matched || o.sizeMatched || o.matched_size || o.matchedSize || o.takerAmount || o.taker_amount || 0;
    const matched = typeof rawMatched === "number" ? (rawMatched >= 1e12 ? (rawMatched / 1e18).toFixed(2) : rawMatched.toFixed(2)) : String(rawMatched);
    const status = o.status || "";
    const statusBadge = status ? `<span class="order-status ${status.toLowerCase()}">${esc(status)}</span>` : "";
    const cancel = id
      ? `<button type="button" class="btn btn-cancel-order" data-order-id="${esc(String(id))}">Cancel</button>`
      : "";
    return `<div class="inv-row">
      <span class="inv-side">${esc(side)}</span>
      <span class="inv-px">@${esc(String(price))}</span>
      <span class="inv-sz">${esc(String(size))} / ${esc(String(matched))}</span>
      ${statusBadge}
      ${cancel}
    </div>`;
  }).join("");
  if (_renderedOpenOrders !== html) {
    el.innerHTML = html;
    _renderedOpenOrders = html;
  }
  el.querySelectorAll("[data-order-id]").forEach(btn => {
    btn.addEventListener("click", () => cancelOrder(btn.getAttribute("data-order-id")));
  });
}

function renderPositions(positions) {
  const el = document.getElementById("wallet-positions");
  if (!el) return;
  if (!positions.length) {
    const html = '<div class="placeholder">No positions</div>';
    if (_renderedPositions !== html) { el.innerHTML = html; _renderedPositions = html; }
    return;
  }
  const html = positions.slice(0, 12).map(p => {
    const title = p.title || p.slug || p.market || "Position";
    const size = p.size != null ? Number(p.size).toFixed(2) : "—";
    const avg = p.avgPrice || p.avg_price || p.price;
    return `<div class="inv-row">
      <span class="inv-title">${esc(String(title))}</span>
      <span class="inv-sz">${esc(String(size))}</span>
      <span class="inv-px">${avg != null ? "@" + Number(avg).toFixed(2) : ""}</span>
    </div>`;
  }).join("");
  if (_renderedPositions !== html) {
    el.innerHTML = html;
    _renderedPositions = html;
  }
}

async function verifyWallet() {
  const btn = document.getElementById("btn-verify-wallet");
  const msg = document.getElementById("wallet-verify-msg");
  if (btn) { btn.disabled = true; btn.textContent = "Verifying…"; }
  if (msg) { msg.textContent = ""; msg.className = "wallet-verify-msg"; }
  try {
    const res = await fetch("/api/wallet/verify");
    const data = await res.json();
    if (msg) {
      msg.textContent = data.ok ? "Wallet ready" : (data.issues && data.issues[0]) || "Not ready";
      msg.className = "wallet-verify-msg " + (data.ok ? "ok" : "err");
    }
    pollStatus();
  } catch (e) {
    if (msg) {
      msg.textContent = e.message || "Verify failed";
      msg.className = "wallet-verify-msg err";
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Verify wallet"; }
  }
}

async function cancelOrder(orderId) {
  if (!orderId) return;
  try {
    const res = await fetch("/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_id: orderId }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    pollStatus();
  } catch (e) {
    console.error("cancel failed", e);
  }
}

function setClass(id, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  const active = cls.split(/\s+/);
  const current = Array.from(el.classList);
  current.filter(c => !active.includes(c)).forEach(c => el.classList.remove(c));
  active.filter(c => !current.includes(c)).forEach(c => el.classList.add(c));
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

window.addEventListener("resize", () => {
  if (viewActive) {
    clearCanvasCache();
    needsRedraw = true;
  }
});

window.SignullLive = { init, setActive };
})();
