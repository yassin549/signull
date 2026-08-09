/* Signull shell — Live / Backtest mode toggle (keeps both views mounted). */

(function () {
  const MODE_KEY = "signull.dashboard.mode";
  const VALID = new Set(["live", "backtest"]);

  let mode = "live";
  let reducedMotion = false;

  function preferredMode() {
    const hash = (location.hash || "").replace(/^#/, "").toLowerCase();
    if (VALID.has(hash)) return hash;
    try {
      const stored = localStorage.getItem(MODE_KEY);
      if (VALID.has(stored)) return stored;
    } catch (_) {}
    return "live";
  }

  function setChrome(next) {
    document.querySelectorAll(".mode-btn").forEach((btn) => {
      const on = btn.dataset.mode === next;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });

    document.querySelectorAll(".live-only").forEach((el) => {
      el.classList.toggle("hidden", next !== "live");
    });
    document.querySelectorAll(".bt-only").forEach((el) => {
      el.classList.toggle("hidden", next !== "backtest");
    });

    const liveView = document.getElementById("view-live");
    const btView = document.getElementById("view-backtest");
    if (liveView) {
      const on = next === "live";
      liveView.classList.toggle("active", on);
      if (on) liveView.removeAttribute("hidden");
      else liveView.setAttribute("hidden", "");
    }
    if (btView) {
      const on = next === "backtest";
      btView.classList.toggle("active", on);
      if (on) btView.removeAttribute("hidden");
      else btView.setAttribute("hidden", "");
    }

    document.body.dataset.mode = next;
    document.title = next === "backtest" ? "Signull · Backtest" : "Signull · Live";
  }

  function setMode(next, { persist = true, pushHash = true } = {}) {
    if (!VALID.has(next) || next === mode) {
      // Still notify panels so first paint / redraw can run.
      if (next === mode) {
        notify(next, true);
      }
      return;
    }
    mode = next;
    setChrome(next);

    if (persist) {
      try { localStorage.setItem(MODE_KEY, next); } catch (_) {}
    }
    if (pushHash) {
      const want = "#" + next;
      if (location.hash !== want) {
        history.replaceState(null, "", want);
      }
    }

    notify(next, false);
  }

  function notify(next, same) {
    // Live: keep WS alive; only pause canvas paint when hidden.
    if (window.SignullLive && typeof window.SignullLive.setActive === "function") {
      window.SignullLive.setActive(next === "live");
    }
    // Backtest: redraw equity when shown; leave SSE running if mid-run.
    if (window.SignullBacktest && typeof window.SignullBacktest.setActive === "function") {
      window.SignullBacktest.setActive(next === "backtest");
    }
    void same;
  }

  function bindToggle() {
    const root = document.getElementById("mode-toggle");
    if (!root) return;
    root.addEventListener("click", (e) => {
      const btn = e.target.closest(".mode-btn");
      if (!btn || !btn.dataset.mode) return;
      setMode(btn.dataset.mode);
    });
  }

  function bindHash() {
    window.addEventListener("hashchange", () => {
      const hash = (location.hash || "").replace(/^#/, "").toLowerCase();
      if (VALID.has(hash)) setMode(hash, { pushHash: false });
    });
  }

  function bindKeys() {
    window.addEventListener("keydown", (e) => {
      if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "l" || e.key === "L") setMode("live");
      if (e.key === "b" || e.key === "B") setMode("backtest");
    });
  }

  function init() {
    reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion) document.documentElement.classList.add("reduce-motion");

    bindToggle();
    bindHash();
    bindKeys();

    // Init both modules once so state/WS/SSE stay resident across toggles.
    if (window.SignullLive && typeof window.SignullLive.init === "function") {
      window.SignullLive.init();
    }
    if (window.SignullBacktest && typeof window.SignullBacktest.init === "function") {
      window.SignullBacktest.init();
    }

    const initial = preferredMode();
    mode = initial === "live" ? "backtest" : "live"; // force first apply
    setMode(initial, { persist: true, pushHash: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.SignullShell = { setMode, getMode: () => mode };
})();
