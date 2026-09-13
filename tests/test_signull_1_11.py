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
    def test_confidence_scales_between_bounds(self):
        from strategies.signull_1_11 import Signull11Strategy

        strategy = Signull11Strategy(
            {"min_risk_pct": 0.02, "max_risk_pct": 0.10, "asset": "btc"}
        )
        # Low confidence -> minimum risk
        strategy._last_confidence = 0.5
        low_stake, _, low_frac = strategy.stake_override(
            None, None, None, equity=100.0, initial=100.0
        )
        # Full confidence -> maximum risk
        strategy._last_confidence = 1.0
        high_stake, _, high_frac = strategy.stake_override(
            None, None, None, equity=100.0, initial=100.0
        )
        assert low_stake == pytest.approx(2.0)
        assert high_stake == pytest.approx(10.0)
        assert low_frac < high_frac

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
