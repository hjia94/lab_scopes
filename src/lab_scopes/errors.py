"""Common exceptions for lab_scopes."""


class ScopeError(RuntimeError):
    """Base class for oscilloscope driver errors."""


class ScopeConnectionError(ScopeError):
    """Raised when a scope connection cannot be established."""


class ScopeTimeoutError(ScopeError):
    """Raised when scope communication times out."""


class ScopeProtocolError(ScopeError):
    """Raised when a scope returns malformed protocol data."""
