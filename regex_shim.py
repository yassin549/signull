"""Minimal regex compatibility shim using stdlib re (bypasses blocked _regex DLL)."""
import re as _re
import types
import sys as _sys


def _inject():
    mod = types.ModuleType("regex")
    mod.__file__ = __file__
    mod.__version__ = "0.0.0-shim"
    mod.__package__ = "regex"
    mod.__path__ = []

    for attr in dir(_re):
        if not attr.startswith("_"):
            setattr(mod, attr, getattr(_re, attr))

    for sub in ("_regex", "_regex_core", "_main"):
        sub_mod = types.ModuleType(f"regex.{sub}")
        sub_mod.__file__ = __file__
        sub_mod.__package__ = "regex"
        _sys.modules[f"regex.{sub}"] = sub_mod

    _sys.modules["regex"] = mod
    mod._regex = _sys.modules["regex._regex"]
    mod._regex_core = _sys.modules["regex._regex_core"]
    mod._main = _sys.modules["regex._main"]


_inject()