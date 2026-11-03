"""Import modules of the integration without running its __init__ (which needs Home Assistant)."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "petlibro_lan"
NAME = "petlibro_lan_under_test"


def load(name: str):
    pkg = sys.modules.setdefault(NAME, types.ModuleType(NAME))
    pkg.__path__ = [str(PKG)]
    full = f"{NAME}.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return sys.modules[full]
