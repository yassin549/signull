import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.sizing import compute_stake

config = BotConfig.from_env()
print("Config from env:")
print("  use_fixed_stake:", config.use_fixed_stake)
print("  fixed_stake_usdc:", config.fixed_stake_usdc)
print("  order_size_usdc:", config.order_size_usdc)

stake = compute_stake(0.10, 100.0, 100.0, fixed_stake=config.fixed_stake_usdc)
print("Computed stake:", stake)

assert config.use_fixed_stake is True
assert config.fixed_stake_usdc == 1.0
assert stake == 1.0
print("SUCCESS: Fixed $1.00 bet budget verified!")
