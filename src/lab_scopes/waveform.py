"""Shared waveform container."""

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Waveform:
    """One acquired trace plus calibration metadata."""

    channel: str
    raw: np.ndarray
    voltage: np.ndarray
    time: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def points(self) -> int:
        return len(self.voltage)
