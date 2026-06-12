"""Suite-wide pytest hooks.

These back the MUTATING / SCOPE_IP knobs in test_lecroy_scope_real.py. They
originally lived in that module, but pytest only honors hooks defined in
conftest.py or plugins, so the marker registration and the MUTATING=True
filtering were silently inert (and every run warned about an unknown
"mutating" marker).
"""

import pytest

_REAL_SCOPE_FILE = "test_lecroy_scope_real.py"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "mutating: state-mutating tests run only when MUTATING=True"
    )


def pytest_collection_modifyitems(config, items):
    """In MUTATING=True mode, run only the state-mutating real-scope tests.

    Scoped to test_lecroy_scope_real.py: the knob selects which *hardware*
    tests run; it must not skip the software-only unit tests collected
    alongside them.
    """
    try:
        import test_lecroy_scope_real as real_mod
    except Exception:
        return
    if not real_mod.MUTATING:
        return
    skip = pytest.mark.skip(reason="MUTATING=True; running only state-mutating tests")
    for item in items:
        if item.path.name != _REAL_SCOPE_FILE:
            continue
        if "mutating" not in {m.name for m in item.iter_markers()}:
            item.add_marker(skip)
