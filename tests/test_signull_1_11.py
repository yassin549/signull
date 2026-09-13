"""Tests for the Signull 1.11 AI (OpenRouter) model plumbing."""

from __future__ import annotations

import json

import pytest

from src.ai_engine import parse_decision


class TestParseDecision:
    def test_clean_json(self):
        side, confidence, reasoning, factors = parse_decision(
            '{"decision": "up", "confidence": 0.72, "reasoning": "momentum", '
            '"key_factors": ["trend", "beat"]}'
        )
        assert side == "up"
        assert confidence == pytest.approx(0.72)
        assert reasoning == "momentum"
        assert factors == ["trend", "beat"]

    def test_fenced_json_and_percent_confidence(self):
        side, confidence, _, _ = parse_decision(
            '```json\n{"decision":"down","confidence":85,"reasoning":"x"}\n```'
        )
        assert side == "down"
        assert confidence == pytest.approx(0.85)

    def test_prose_fallback(self):
        side, confidence, _, _ = parse_decision("My final call is that it goes up.")
        assert side == "up"
        assert 0.5 <= confidence <= 1.0

    def test_unparseable_raises(self):
        with pytest.raises(ValueError):
            parse_decision("I cannot decide right now.")


class TestStrategyRegistration:
    def test_registry_lists_ai_as_live_only(self):
        from src.backtest.registry import get_strategy, list_strategies

        entry = next(s for s in list_strategies() if s["id"] == "signull_1_11")
        assert entry["live_only"] is True
        strategy = get_strategy("signull_1_11", {"asset": "btc"})
        assert strategy.meta.id == "signull_1_11"


class TestConfidenceSizing:
    def test_confidence_scales_between_dollar_bounds(self):
        from strategies.signull_1_11 import Signull11Strategy

        strategy = Signull11Strategy(
            {"min_stake_usd": 1.0, "max_stake_usd": 2.0, "asset": "btc"}
        )
        strategy._last_confidence = 0.5
        low_stake, _, _ = strategy.stake_override(
            None, None, None, equity=100.0, initial=100.0
        )
        strategy._last_confidence = 1.0
        high_stake, _, _ = strategy.stake_override(
            None, None, None, equity=100.0, initial=100.0
        )
        assert low_stake == pytest.approx(1.0)
        assert high_stake == pytest.approx(2.0)

    def test_theoretical_pnl_recorded_on_close(self):
        from strategies.signull_1_11 import Signull11Strategy

        strategy = Signull11Strategy({"asset": "btc", "min_stake_usd": 1.0, "max_stake_usd": 2.0})
        strategy._ai_history.append({
            "slug": "c9",
            "side": "up",
            "confidence": 1.0,
            "entry_price": 0.5,
            "stake": None,
            "winner": None,
            "won": None,
            "pnl": None,
        })
        strategy._stake_by_slug["c9"] = 2.0
        strategy.register_closed_candle("c9", [(1, 0.6, 0.4), (2, 0.6, 0.4), (3, 0.6, 0.4)])
        entry = strategy._ai_history[-1]
        assert entry["won"] is True
        assert entry["winner"] == "up"
        assert entry["pnl"] == pytest.approx(1.98)
        snapshot = strategy.ai_snapshot()
        assert snapshot["pnl_total"] == pytest.approx(1.98)
        assert snapshot["trades"] == 1
        assert snapshot["wins"] == 1

    def test_prior_outcome_resolved_without_fills(self):
        from strategies.signull_1_11 import Signull11Strategy

        strategy = Signull11Strategy({"asset": "btc"})
        strategy._ai_history.append({
            "slug": "btc-updown-5m-1",
            "side": "up",
            "entry_price": 0.5,
            "stake": 1.0,
            "winner": None,
            "won": None,
            "pnl": None,
        })
        strategy._actual_winner = lambda slug, ticks: (  # type: ignore[assignment]
            "up", {"start": 1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "vol": 1.0}
        )
        resolved = strategy._resolve_entry_outcome(strategy._ai_history[-1])
        assert resolved["won"] is True
        assert resolved["winner"] == "up"
        assert resolved["pnl"] == pytest.approx(0.99)

        strategy._ai_history.append({
            "slug": "btc-updown-5m-2",
            "side": "up",
            "entry_price": 0.5,
            "stake": 2.0,
            "winner": None,
            "won": None,
            "pnl": None,
        })
        strategy._actual_winner = lambda slug, ticks: (  # type: ignore[assignment]
            "down", None
        )
        resolved = strategy._resolve_entry_outcome(strategy._ai_history[-1])
        assert resolved["won"] is False
        assert resolved["pnl"] < 0

    def test_outcome_feedback_text(self):
        from strategies.signull_1_11 import Signull11Strategy

        strategy = Signull11Strategy({"asset": "btc"})
        strategy._ai_history.append({"slug": "c1", "side": "up", "won": None})
        strategy.register_closed_candle(
            "c1", [(1, 0.6, 0.4), (2, 0.55, 0.45), (3, 0.7, 0.3)]
        )
        strategy.on_trade_settled(True)
        with strategy._state_lock:
            outcome = dict(strategy._outcomes["c1"])
            outcome["side"] = "up"
        text = strategy._outcome_text(outcome)
        assert "WIN" in text
        assert "UP" in text

    def test_settle_with_preexisting_winner(self):
        from strategies.signull_1_11 import Signull11Strategy

        strategy = Signull11Strategy({"asset": "btc"})
        strategy._ai_history.append(
            {"slug": "c2", "side": "down", "won": None, "winner": "down"}
        )
        strategy.register_closed_candle("c2", [(1, 0.4, 0.6), (2, 0.3, 0.7)])
        strategy.on_trade_settled(True)  # must not raise
        assert strategy._ai_history[-1]["won"] is True


class TestPureReasoningContext:
    def test_ohlcv_block_contains_only_candles(self):
        from strategies.signull_1_11 import Signull11Strategy
        from strategies.base import CandleContext, TickContext

        strategy = Signull11Strategy({"asset": "btc"})
        bars = [
            {"start": 1000 + i * 300, "open": 100.0 + i, "high": 101.0 + i,
             "low": 99.0 + i, "close": 100.5 + i, "vol": 10.0}
            for i in range(6)
        ]
        strategy._fetch_bars = lambda now: bars  # type: ignore[assignment]
        tick = TickContext(t=2000, up=0.5, down=0.5, seconds_into_candle=3.0, seconds_to_close=297.0)
        candle = CandleContext(slug="btc-updown-5m-1000", title="x", start_ts=1000, end_ts=1300, winner="")
        block = strategy._ohlcv_block(tick, candle)
        assert "OHLCV" in block
        assert "SIM" not in block.upper()
        assert "ODDS" not in block.upper()
        assert "ACCOUNT" not in block.upper()
        # Exactly five completed candles are shown.
        assert block.count("\n- ") == 5


class TestModelList:
    def test_normalize_marks_free_and_paid(self):
        from src.openrouter import _normalize_model

        free = _normalize_model(
            {"id": "x:free", "name": "X", "pricing": {"prompt": "0", "completion": "0"}}
        )
        assert free["free"] is True
        assert free["id"] == "x:free"

        paid = _normalize_model(
            {"id": "y", "name": "Y", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
        )
        assert paid["free"] is False

    def test_normalize_handles_missing_pricing(self):
        from src.openrouter import _normalize_model

        model = _normalize_model({"id": "z"})
        assert model["id"] == "z"
        assert model["name"] == "z"
        assert model["free"] is False


class TestAISettings:
    def test_round_trip_and_masking(self, tmp_path, monkeypatch):
        import src.ai_settings as settings

        monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "openrouter.json")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        public = settings.public_ai_settings()
        assert public["api_key_set"] is False

        settings.save_ai_settings(api_key="sk-or-v1-abcdefghijklmnop", model="foo/bar:free")
        public = settings.public_ai_settings()
        assert public["api_key_set"] is True
        assert public["api_key_masked"].startswith("sk-or-")
        assert "abcdefghijklmnop" not in public["api_key_masked"]
        assert public["model"] == "foo/bar:free"

        settings.save_ai_settings(api_key="")
        assert settings.public_ai_settings()["api_key_set"] is False


class TestEnginePersistence:
    def test_transcript_persists_across_instances(self, tmp_path, monkeypatch):
        import src.ai_engine as engine_mod

        monkeypatch.setattr(engine_mod, "STATE_PATH", tmp_path / "ai_conversation.json")
        engine = engine_mod.AIDecisionEngine()

        class FakeClient:
            def complete(self, *args, **kwargs):
                return {
                    "content": json.dumps(
                        {"decision": "up", "confidence": 0.7, "reasoning": "r"}
                    ),
                    "model": "fake",
                    "usage": {"total_tokens": 12},
                    "latency_ms": 5.0,
                }

        engine._client = FakeClient()  # type: ignore[assignment]
        engine.decide("first context")
        assert engine.snapshot()["usage"]["calls"] == 1

        reloaded = engine_mod.AIDecisionEngine()
        reloaded._client = FakeClient()  # type: ignore[assignment]
        snap = reloaded.snapshot()
        assert snap["usage"]["calls"] == 1
        # system + user + assistant
        assert len(snap["messages"]) == 3
