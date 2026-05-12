"""Legacy import shim for ``from LeCroy_Scope import ...``."""

from lab_scopes.lecroy.legacy import (
    EXPANDED_TRACE_NAMES,
    KNOWN_TRACE_NAMES,
    WAVEDESC_SIZE,
    LeCroyScope,
    LeCroy_Scope,
)
from lab_scopes.lecroy.scope import Fake_Scope

__all__ = [
    "LeCroyScope",
    "LeCroy_Scope",
    "Fake_Scope",
    "WAVEDESC_SIZE",
    "EXPANDED_TRACE_NAMES",
    "KNOWN_TRACE_NAMES",
]
