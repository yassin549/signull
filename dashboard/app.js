/* Signull dashboard */

let ws, reconnectTimer, pollTimer, lastVersion = -1;
let history = [];
let btcHistory = [];
let equityHistory = [];
let equityInitial = null;
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
const POLL_FAST_MS = 250;
const smooth = { up: null, down: null, btcDelta: null };
let needsRedraw = true;
let lastServerTs = null;
let countdownBase = null;
let activeCandleSlug = null;
let feedReconnecting = false;
let localWindowStart = null;

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
    clearInterval(pollTimer);
    pollTimer = null;
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

  lastVersion = d.version ?? lastVersion;
  lastUpdateAt = Date.now();
  setSyncStatus(true);

  const equityIncoming = d.equity_history || [];
  if (isFull) {
    equityHistory = [];
    mergeEquityHistory(equityIncoming);
  }
  else if (equityIncoming.length) mergeEquityHistory(equityIncoming);

  onUpdate(d);

  const incoming = d.price_history || [];
  if (isFull) {
    history = incoming.slice();
  } else if (incoming.length) {
    mergeHistory(incoming);
  }

  const btcIncoming = d.btc_history || [];
  if (isFull) {
    // Server is authoritative on full poll — empty is OK right after rollover.
    btcHistory = (btcIncoming || []).map(normalizeBtcPoint);
  } else if (btcIncoming.length) {
    mergeBtcHistory(btcIncoming);
  }
  // Live tail after history merge so it never blocks server points.
  if (d.btc) seedBtcLivePoint(d.btc);

  // Shared wall-clock "now" for both charts (odds + BTC must share the axis).
  const oddsT = history.length ? history[history.length - 1].t : null;
  const btcT = btcHistory.length ? btcHistory[btcHistory.length - 1].t : null;
  lastServerTs = Math.max(
    oddsT || 0,
    btcT || 0,
    d.feed?.last_update_at || 0,
    d.btc?.updated_at || 0,
    Date.now() / 1000 - 1
  );
  needsRedraw = true;
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
  equityHistory = [...byT.values()].sort((a, b) => a.t - b.t).slice(-6000);
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
    btcHistory = incoming.map(normalizeBtcPoint);
    return;
  }
  // Index by rounded time so slower server samples can update/extend even if
  // the client live-tail timestamp is slightly ahead.
  const byT = new Map();
  for (const p of btcHistory) byT.set(Math.round(p.t * 20) / 20, p);
  for (const raw of incoming) {
    const pt = normalizeBtcPoint(raw);
    const key = Math.round(pt.t * 20) / 20;
    const prev = byT.get(key);
    if (!prev || (pt.v != null && (prev.v == null || pt.t >= prev.t))) {
      byT.set(key, pt);
    }
  }
  btcHistory = [...byT.values()].sort((a, b) => a.t - b.t);
  if (btcHistory.length > 6000) btcHistory = btcHistory.slice(-6000);
}

function normalizeBtcPoint(p) {
  if (!p) return { t: 0 };
  const out = { t: Number(p.t) };
  if (p.v != null && !isNaN(Number(p.v))) out.v = Number(p.v);
  // Prefer absolute price vs locked beat when both known — never trust a stale d.
  if (out.v != null && priceToBeat != null) {
    out.d = out.v - priceToBeat;
  } else if (p.d != null && !isNaN(Number(p.d))) {
    out.d = Number(p.d);
  }
  return out;
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

/**
 * Push a live sample from the latest btc panel tick.
 * Always uses wall clock so a frozen server updated_at cannot pin the series.
 */
function seedBtcLivePoint(btc) {
  if (!btc) return;
  // Prefer Binance; fall back to Chainlink if the fast tape is silent.
  const v = btc.price != null ? Number(btc.price)
    : (btc.chainlink != null ? Number(btc.chainlink) : null);
  if (v == null || isNaN(v)) return;

  const now = Date.now() / 1000;
  let d = null;
  if (priceToBeat != null) d = v - priceToBeat;
  else if (btc.delta != null) d = Number(btc.delta);

  if (!btcHistory.length) {
    btcHistory.push({ t: now, v, d });
    return;
  }
  const last = btcHistory[btcHistory.length - 1];
  if (now - last.t < 0.045) {
    btcHistory[btcHistory.length - 1] = { t: now, v, d: d != null ? d : last.d };
  } else {
    btcHistory.push({ t: now, v, d: d != null ? d : last.d });
    if (btcHistory.length > 6000) btcHistory = btcHistory.slice(-6000);
  }
}

const onUpdate = safe(function onUpdate(d) {
  detectCandleChange(d.market);
  updateTopbar(d);
  updateHero(d);
  updateStrategy(d);
  updateWallet(d);
  updateOrderbooks(d.orderbooks || {});
  updateTape(d.trades || []);
  updateStrategyTrades(d.strategy_trades || []);
  updateLog(d.activity || []);
  updateBtcPanel(d.btc);
  updateBotButtons(d.running);
});

function detectCandleChange(market) {
  const slug = market?.slug;
  if (!slug || slug === activeCandleSlug) return;
  const isRollover = activeCandleSlug !== null;
  activeCandleSlug = slug;
  if (isRollover) resetCandleUI(market?.provisional ? "soft" : "full");
  countdownBase = { market };
}

function resetCandleUI(mode = "full") {
  // Soft: keep last odds prices briefly while provisional window loads.
  // BTC series always resets on candle change — keeping the previous window
  // rebased onto the new open beat pinned Δ near 0 and looked "stuck".
  history = [];
  btcHistory = [];
  priceToBeat = null;
  smooth.btcDelta = null;
  lastTradeId = null;
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
  setText("signal-side", "HOLD");
  setClass("signal-side", "signal-badge hold");
  setText("signal-reason", mode === "soft" ? "Rolling into new candle…" : "New candle — warming up");
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

function updateStrategy(d) {
  const s = d.strategy || {};
  setText("strat-name", s.name || "Signull 1.0");
  if (s.equity != null) {
    setText("strat-equity", `Equity $${Number(s.equity).toFixed(2)}`);
  }
  if (s.return_pct != null) {
    const r = Number(s.return_pct);
    const el = document.getElementById("strat-return");
    if (el) {
      el.textContent = `${r >= 0 ? "+" : ""}${r.toFixed(1)}%`;
      el.className = "strat-ret " + (r >= 0 ? "up" : "down");
    }
  }
  const p = s.pending;
  if (p) {
    setText(
      "strat-pending",
      `${(p.mode || "paper").toUpperCase()} ${String(p.side).toUpperCase()} @ ${Number(p.entry_price).toFixed(2)} · ${p.size_label} $${Number(p.stake).toFixed(2)}`
    );
  } else {
    setText("strat-pending", s.entered_this_candle ? "Entered · waiting resolve" : "No position");
  }
}

function updateStrategyTrades(trades) {
  const el = document.getElementById("strat-trades");
  if (!el) return;
  if (!trades.length) {
    el.innerHTML = '<div class="placeholder">No Signull trades yet</div>';
    return;
  }
  el.innerHTML = trades.slice(0, 20).map(t => {
    const cls = t.won ? "win" : "loss";
    const pnl = Number(t.pnl || 0);
    const tip = [
      t.won ? "WIN" : "LOSS",
      `side=${t.side}`,
      `winner=${t.winner || "?"}`,
      t.resolve_source ? `via ${t.resolve_source}` : "",
      t.title || t.slug || "",
    ].filter(Boolean).join(" · ");
    return `<div class="strat-trade ${cls}" title="${esc(tip)}">
      <span class="st-side">${String(t.side || "").toUpperCase()}</span>
      <span class="st-px">@${Number(t.entry_price).toFixed(2)}</span>
      <span class="st-sz">${t.size_label || ""} $${Number(t.stake).toFixed(2)}</span>
      <span class="st-pnl">${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}</span>
      <span class="st-eq">$${Number(t.equity_after).toFixed(2)}</span>
    </div>`;
  }).join("");
}

function flashEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function tickCountdown() {
  // Local 5m boundary — refresh UI even if the server is still on the old slug.
  const win = clockWindowStart();
  if (localWindowStart != null && win !== localWindowStart) {
    if (marketIsStale(countdownBase?.market) || !countdownBase?.market) {
      // Inject a provisional market so the timer jumps to ~5:00 immediately.
      const endMs = (win + CANDLE_SEC) * 1000;
      const provisional = {
        slug: `local-${win}`,
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
    el.textContent = atBoundary
      ? "0:00"
      : `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    el.classList.toggle("rolling", atBoundary || loading);
  }
  if (label) {
    if (atBoundary) label.textContent = "rolling over";
    else if (loading) label.textContent = "loading market";
    else label.textContent = "to close";
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
  const isPaper = (a.mode || d.mode || "paper") !== "live";

  const balLabel = document.getElementById("wallet-balance-label");
  if (balLabel) balLabel.textContent = isPaper ? "Paper equity" : "USDC Balance";

  const equity = a.paper_equity != null ? a.paper_equity : a.balance_usdc;
  if (a.paper_initial != null && Number.isFinite(Number(a.paper_initial))) {
    equityInitial = Number(a.paper_initial);
  }
  setText("wallet-balance", equity != null ? `$${Number(equity).toFixed(2)}` : "—");
  setText("wallet-funder", fmtAddr(a.funder_address));
  setText("wallet-signer", fmtAddr(a.signer_address));
  setText("wallet-type", a.signature_label || "—");
  setText("wallet-mode", (a.mode || d.mode || "paper").toUpperCase());
  setText("wallet-strategy", s.name || "Signull 1.0");
  const thr = s.params?.threshold;
  setText("wallet-threshold", thr != null ? `${(Number(thr) * 100).toFixed(0)}¢ limit` : "—");
  updateEquityMeta(equity, a.paper_initial);

  const statusEl = document.getElementById("wallet-status");
  if (statusEl) {
    if (isPaper) {
      statusEl.textContent = "Paper · live markets";
      statusEl.className = "wallet-pill ok";
    } else {
      statusEl.textContent = connected ? "Connected" : "Not connected";
      statusEl.className = "wallet-pill " + (connected ? "ok" : "off");
    }
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

function updateEquityMeta(equity, initial) {
  if (!equityHistory.length || equity == null) {
    setText("equity-chart-meta", "Waiting for balance data");
    return;
  }
  const start = initial != null ? Number(initial) : Number(equityHistory[0].v);
  const change = Number(equity) - start;
  const pctChange = start ? (change / start) * 100 : 0;
  setText(
    "equity-chart-meta",
    `${change >= 0 ? "+" : "−"}${fmtUsdCompact(Math.abs(change))} (${pctChange >= 0 ? "+" : ""}${pctChange.toFixed(2)}%)`
  );
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

  // Prefer fast Binance (price); Chainlink only as fallback for the hero number.
  const binanceAge = btc.updated_at != null ? (Date.now() / 1000 - Number(btc.updated_at)) : 999;
  const livePrice = (btc.price != null && binanceAge < 5)
    ? btc.price
    : (btc.price != null ? btc.price : btc.chainlink);
  if (livePrice != null) {
    setText("btc-spot", fmtUsd(livePrice));
    setText("hero-btc-price", fmtUsd(livePrice));
    flashEl("hero-btc-price");
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
    const stale = binanceAge > 3 ? " · lag" : "";
    setText("btc-chart-meta", `${fmtDelta(delta)} · ${chartWindowLabel()}${stale}`);
    const metaEl = document.getElementById("btc-chart-meta");
    if (metaEl) metaEl.className = deltaCls;
  } else if (livePrice != null) {
    setText("btc-chart-meta", fmtUsd(livePrice));
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

function chartNow() {
  // Shared clock for both canvases so BTC and odds stay correlated in time.
  const oddsT = history.length ? history[history.length - 1].t : 0;
  const btcT = btcHistory.length ? btcHistory[btcHistory.length - 1].t : 0;
  return Math.max(lastServerTs || 0, oddsT, btcT, Date.now() / 1000 - 0.5);
}

function chartWindowSec() {
  const now = chartNow();
  let earliest = now;
  if (history.length) earliest = Math.min(earliest, history[0].t);
  if (btcHistory.length) earliest = Math.min(earliest, btcHistory[0].t);
  if (!history.length && !btcHistory.length) return CANDLE_SEC;
  const span = now - earliest;
  return Math.min(CANDLE_SEC, Math.max(45, span + 2));
}

function chartWindowLabel() {
  const windowSec = chartWindowSec();
  return windowSec >= CANDLE_SEC - 1 ? "5m" : `${Math.round(windowSec)}s`;
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
  const now = chartNow();
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

function btcPointDelta(p, baseline) {
  // Always prefer absolute spot vs locked beat — avoids a frozen d=0 series
  // when v later moves (or when an old d was computed against a wrong beat).
  if (p.v != null && baseline != null) return p.v - baseline;
  if (p.v != null && priceToBeat != null) return p.v - priceToBeat;
  if (p.d != null) return p.d;
  return null;
}

function drawBtcChart() {
  const canvas = document.getElementById("btc-chart");
  if (!canvas) return false;
  const setup = setupCanvas(canvas);
  if (!setup) return false;
  const { ctx, w: W, h: H } = setup;

  ctx.fillStyle = "#06090f";
  ctx.fillRect(0, 0, W, H);

  if (!btcHistory.length) {
    return true;
  }

  const pad = { l: 52, r: 12, t: 12, b: 26 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  // Same time window as the odds chart so movements line up visually.
  const windowSec = chartWindowSec();
  const now = chartNow();
  const tMin = now - windowSec;

  // Baseline for Δ: official beat when known, else first visible absolute price
  // so we still draw a continuous series during beat loading / soft rollover.
  let baseline = priceToBeat;
  const inWindow = btcHistory.filter(p => p.t >= tMin && (p.v != null || p.d != null));
  if (baseline == null && inWindow.length) {
    const firstV = inWindow.find(p => p.v != null);
    if (firstV) baseline = firstV.v;
  }

  let visible = inWindow
    .map(p => ({ t: p.t, d: btcPointDelta(p, baseline), v: p.v }))
    .filter(p => p.d != null);

  // Single point: synthesize a flat start so we still show a line.
  if (visible.length === 1) {
    visible = [
      { t: Math.max(tMin, visible[0].t - 1), d: visible[0].d },
      visible[0],
    ];
  }

  if (visible.length < 2) {
    return true;
  }

  // Live tail (smooth) — same trick as the odds chart.
  if (smooth.btcDelta != null) {
    visible = [...visible, { t: now, d: smooth.btcDelta }];
  } else {
    // Extend last sample to "now" so the line doesn't leave a gap at the right edge.
    const last = visible[visible.length - 1];
    if (now - last.t > 0.05) {
      visible = [...visible, { t: now, d: last.d }];
    }
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
  ctx.fillText(priceToBeat != null ? "Beat 0" : "ref 0", pad.l + 4, y0 - 5);

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
  ctx.lineCap = "round";
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

function drawEquityChart() {
  const canvas = document.getElementById("equity-chart");
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

  const pad = { l: 58, r: 12, t: 8, b: 22 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const first = visible[0];
  const last = visible[visible.length - 1];
  const tMin = first.t;
  const tMax = Math.max(last.t, tMin + 1);
  // Equity changes only at settlement.  Keeping the initial balance in the
  // domain stops minor moves from being magnified into a full-height climb.
  const initial = Number.isFinite(equityInitial) ? equityInitial : Number(first.v);
  const vals = [...visible.map(p => p.v), initial];
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const spread = Math.max(hi - lo, Math.max(Math.abs(hi) * 0.01, 0.01));
  const yMin = lo - spread * 0.2;
  const yMax = hi + spread * 0.2;
  const xS = t => pad.l + ((t - tMin) / (tMax - tMin)) * plotW;
  const yS = v => pad.t + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  ctx.strokeStyle = "#1a2438";
  ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono, monospace";
  for (let i = 0; i <= 3; i++) {
    const y = pad.t + (plotH / 3) * i;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = "#5a6d8a";
    ctx.textAlign = "right";
    ctx.fillText(fmtUsdCompact(yMax - ((yMax - yMin) / 3) * i), pad.l - 4, y + 3);
  }

  const positive = Number(last.v) >= Number(first.v);
  const color = positive ? "#0ecb81" : "#f6465d";
  const baseline = yS(initial);
  ctx.fillStyle = positive ? "rgba(14, 203, 129, 0.12)" : "rgba(246, 70, 93, 0.12)";
  ctx.beginPath();
  visible.forEach((p, i) => {
    if (!i) ctx.moveTo(xS(p.t), yS(p.v));
    else {
      const prev = visible[i - 1];
      ctx.lineTo(xS(p.t), yS(prev.v));
      ctx.lineTo(xS(p.t), yS(p.v));
    }
  });
  ctx.lineTo(xS(last.t), baseline); ctx.lineTo(xS(first.t), baseline); ctx.closePath(); ctx.fill();

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  visible.forEach((p, i) => {
    if (!i) ctx.moveTo(xS(p.t), yS(p.v));
    else {
      const prev = visible[i - 1];
      ctx.lineTo(xS(p.t), yS(prev.v));
      ctx.lineTo(xS(p.t), yS(p.v));
    }
  });
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(xS(last.t), yS(last.v), 3.5, 0, Math.PI * 2); ctx.fill();

  ctx.fillStyle = "#5a6d8a";
  ctx.font = "9px JetBrains Mono, monospace";
  ctx.textAlign = "left";
  ctx.fillText(new Date(first.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), pad.l, H - 7);
  ctx.textAlign = "right";
  ctx.fillText(new Date(last.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), W - pad.r, H - 7);
  return true;
}

function renderLoop() {
  // Keep redrawing while live so both charts advance the right edge smoothly.
  const animating = isLive || smooth.up != null || smooth.btcDelta != null || smooth.down != null;
  if (needsRedraw || animating) {
    drawPriceChart();
    drawBtcChart();
    drawEquityChart();
    if (!animating) needsRedraw = false;
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
