"""Legacy LeCroy names for old scripts."""

from .scope import LeCroyScope, LeCroy_Scope
from .header import LeCroyHeader, LeCroy_Scope_Header
from .constants import EXPANDED_TRACE_NAMES, KNOWN_TRACE_NAMES, WAVEDESC_SIZE

__all__ = [
    "LeCroyScope",
    "LeCroyHeader",
    "LeCroy_Scope",
    "LeCroy_Scope_Header",
    "WAVEDESC_SIZE",
    "EXPANDED_TRACE_NAMES",
    "KNOWN_TRACE_NAMES",
]
