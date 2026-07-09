"""Discover strategy modules in the strategies/ folder."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Type

import strategies
from strategies.base import Strategy

def _discover() -> dict[str, Type[Strategy]]:
    """Scan strategies/ on every call; reload modules so param edits apply live."""
    found: dict[str, Type[Strategy]] = {}
    pkg_path = Path(strategies.__file__).parent

    for mod_info in pkgutil.iter_modules([str(pkg_path)]):
        if mod_info.name.startswith("_") or mod_info.name == "base":
            continue
        name = f"strategies.{mod_info.name}"
        module = importlib.import_module(name)
        module = importlib.reload(module)
        cls_name = getattr(module, "STRATEGY_CLASS", None)
        if not cls_name:
            continue
        cls = getattr(module, cls_name, None)
        if cls is None or not issubclass(cls, Strategy):
            continue
        found[cls.meta.id] = cls

    return found


def list_strategies() -> list[dict]:
    out = []
    # Signull 1.0 first (canonical strategy), then alphabetical
    order = {"signull_1_0": 0, "smart_sizer": 1}
    for sid, cls in sorted(
        _discover().items(),
        key=lambda x: (order.get(x[0], 1), x[1].meta.name),
    ):
        out.append({
            "id": sid,
            "name": cls.meta.name,
            "description": cls.meta.description,
            "default_params": cls.meta.default_params,
        })
    return out


def get_strategy(strategy_id: str, params: dict | None = None) -> Strategy:
    reg = _discover()
    if strategy_id not in reg:
        raise KeyError(f"Unknown strategy: {strategy_id}")
    return reg[strategy_id](params)