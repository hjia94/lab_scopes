"""Rigol scope drivers."""

from .dho800 import RIGOL_WAVEDESC_SIZE, RigolDHO800, RigolScopeError, Waveform
from .legacy import RigolScope

__all__ = ["RIGOL_WAVEDESC_SIZE", "RigolDHO800", "RigolScope", "RigolScopeError", "Waveform"]
