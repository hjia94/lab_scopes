"""LeCroy scope driver and offline WAVEDESC parser."""

from .wavedesc import LeCroyHeader, LeCroyWavedesc, LeCroy_Scope_Header
from .scope import LeCroyNoDataError, LeCroyScope, LeCroy_Scope
from .constants import EXPANDED_TRACE_NAMES, KNOWN_TRACE_NAMES, WAVEDESC_SIZE

__all__ = [
    "LeCroyWavedesc",
    "LeCroyHeader",          # deprecated alias for LeCroyWavedesc
    "LeCroy_Scope_Header",   # deprecated alias for LeCroyWavedesc
    "LeCroyNoDataError",
    "LeCroyScope",
    "LeCroy_Scope",
    "WAVEDESC_SIZE",
    "EXPANDED_TRACE_NAMES",
    "KNOWN_TRACE_NAMES",
]
