"""Signull — Polymarket 5M Up/Down trading bot."""

import logging
import sys

import uvicorn

from src.bot import TradingBot
from src.config import BotConfig
from src.server import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = BotConfig.from_env()
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == "server":
        app = create_app(config)
        print(f"Dashboard: http://{config.server_host}:{config.server_port}")
        uvicorn.run(app, host=config.server_host, port=config.server_port, log_level="info")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        from src.backtest_server import create_backtest_app

        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
        host = config.server_host
        app = create_backtest_app()
        print(f"Backtest dashboard: http://{host}:{port}")
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "train-sizer":
        from src.ml.train_sizer import train_tcn_sizer

        logging.getLogger().setLevel(logging.INFO)
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 500
        asset = config.asset
        print(f"Training TCN sizer on {count} {asset.upper()} candles…")
        meta = train_tcn_sizer(asset=asset, candle_count=count)
        print(f"Done — {meta['samples']} samples, val loss {meta['best_val_loss']:.5f}")
        print(f"Model: {meta['model_path']}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "backtest-run":
        from src.backtest.data import fetch_candles
        from src.backtest.engine import run_backtest
        from src.backtest.registry import get_strategy

        strategy_id = sys.argv[2] if len(sys.argv) > 2 else "prob_70"
        count = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        strategy = get_strategy(strategy_id)
        candles = fetch_candles(asset=config.asset, count=count)
        result = run_backtest(strategy, candles, initial_capital=100.0)
        r = result.to_dict()
        print(f"\n=== {r['strategy_name']} ({count} candles) ===")
        print(f"Start:    ${r['initial_capital']:.2f}")
        print(f"End:      ${r['ending_capital']:.2f}")
        print(f"Return:   {r['total_return_pct']:+.2f}%")
        print(f"Trades:   {r['candles_traded']} (W{r['wins']} / L{r['losses']})")
        print(f"Win rate: {r['win_rate']:.1f}%")
        print(f"Max DD:   -{r['max_drawdown_pct']:.2f}%")
        print(f"Elapsed:  {r['elapsed_ms']:.0f} ms")
        return

    bot = TradingBot(config)
    bot.run()


if __name__ == "__main__":
    main()