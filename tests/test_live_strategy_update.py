import pytest
from fastapi.testclient import TestClient

from src.config import BotConfig
from src.server import create_app, BotService
from src.bot import TradingBot


def test_bot_update_strategy():
    config = BotConfig.from_env()
    bot = TradingBot(config)
    
    assert bot.strategy.meta.id == "signull_1_5"
    
    # Update to signull_1_1 with custom params
    new_strat = bot.update_strategy("signull_1_1", {"threshold": 0.65, "risk_pct": 0.15})
    assert new_strat.meta.id == "signull_1_1"
    assert bot.strategy.params["threshold"] == 0.65
    assert bot.strategy.params["risk_pct"] == 0.15
    assert bot.config.strategy_id == "signull_1_1"
    assert bot.config.strategy_params()["threshold"] == 0.65
    assert bot.config.strategy_params()["risk_pct"] == 0.15


def test_bot_service_update_strategy():
    config = BotConfig.from_env()
    service = BotService(config)
    
    res = service.update_strategy("signull_1_0", {"threshold": 0.72})
    assert res["ok"] is True
    assert res["strategy_id"] == "signull_1_0"
    assert res["params"]["threshold"] == 0.72


def test_api_strategy_update_endpoint():
    config = BotConfig.from_env()
    app = create_app(config)
    client = TestClient(app)
    
    # Verify GET /api/config initial strategy
    res = client.get("/api/config")
    assert res.status_code == 200
    cfg = res.json()
    assert "strategy" in cfg
    assert "strategy_params" in cfg

    # Post strategy update
    update_res = client.post("/api/strategy/update", json={
        "strategy_id": "signull_1_4",
        "params": {"threshold": 0.80, "risk_pct": 0.25}
    })
    assert update_res.status_code == 200
    data = update_res.json()
    assert data["ok"] is True
    assert data["strategy_id"] == "signull_1_4"
    assert data["params"]["threshold"] == 0.80
    assert data["params"]["risk_pct"] == 0.25

    # Check updated /api/config
    res2 = client.get("/api/config")
    assert res2.status_code == 200
    cfg2 = res2.json()
    assert cfg2["strategy"] == "signull_1_4"
    assert cfg2["strategy_params"]["threshold"] == 0.80
    assert cfg2["strategy_params"]["risk_pct"] == 0.25


def test_api_strategy_update_invalid():
    config = BotConfig.from_env()
    app = create_app(config)
    client = TestClient(app)

    res = client.post("/api/strategy/update", json={
        "strategy_id": "non_existent_strategy_id",
        "params": {}
    })
    assert res.status_code == 404
