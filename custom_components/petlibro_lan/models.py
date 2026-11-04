"""Device models and serial numbers; no Home Assistant imports."""
from __future__ import annotations

import re
from dataclasses import dataclass

SERIAL_RE = re.compile(r"^(?:AF|WF|LS|PL)[A-Za-z0-9_-]{2,62}$")
MODEL_RE = re.compile(r"^PL[A-Z]{2}\d{3}$")

FEEDER = "feeder"
FOUNTAIN = "fountain"


@dataclass(frozen=True)
class ModelInfo:
    name: str
    kind: str
    max_portions: int = 0  # 0: manual feeding not supported
    light_switch: bool = False  # indicator light via lightSwitch
    lock_key: str | None = None  # attribute that locks the physical button
    battery: bool = False


MODELS: dict[str, ModelInfo] = {
    "PLAF103": ModelInfo("Granary Feeder", FEEDER, 12, True, "disableHardwareButton"),
    "PLAF301": ModelInfo("One RFID Feeder", FEEDER, 5, False, "childLockSwitch", True),
    "PLAF109": ModelInfo("Polar Feeder", FEEDER),
    "PLAF203": ModelInfo("Granary Camera Feeder", FEEDER),
    "PLWF106": ModelInfo("Dockstream 2 Fountain", FOUNTAIN),
    "PLWF116": ModelInfo("Dockstream 2 Cordless Fountain", FOUNTAIN, battery=True),
    "PLWF305": ModelInfo("Dockstream RFID Fountain", FOUNTAIN),
}


def valid_serial(value: object) -> bool:
    return isinstance(value, str) and SERIAL_RE.match(value) is not None


def valid_model(value: object) -> bool:
    return isinstance(value, str) and MODEL_RE.match(value) is not None


def model_info(model: str, serial: str = "") -> ModelInfo | None:
    """Known model, or a read-only guess from the serial prefix (AF feeder, WF fountain)."""
    if (info := MODELS.get(model)) is not None:
        return info
    if serial.startswith("AF"):
        return ModelInfo(f"Feeder {model}", FEEDER)
    if serial.startswith("WF"):
        return ModelInfo(f"Fountain {model}", FOUNTAIN)
    return None


def mask_serial(serial: str) -> str:
    """A serial for display: everything but the last four characters hidden."""
    return "*" * max(0, len(serial) - 4) + serial[-4:]
