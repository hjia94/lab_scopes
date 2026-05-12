"""Legacy import shim for ``from LeCroy_Scope_Header import ...``."""

from lab_scopes.lecroy import LeCroyHeader, LeCroy_Scope_Header
from lab_scopes.lecroy.constants import (
    EXPANDED_TRACE_NAMES,
    KNOWN_TRACE_NAMES,
    WAVEDESC,
    WAVEDESC_FMT,
    WAVEDESC_SIZE,
)

__all__ = [
    "LeCroyHeader",
    "LeCroy_Scope_Header",
    "WAVEDESC",
    "WAVEDESC_FMT",
    "WAVEDESC_SIZE",
    "EXPANDED_TRACE_NAMES",
    "KNOWN_TRACE_NAMES",
]
