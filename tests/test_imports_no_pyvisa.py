import importlib
import sys


def test_package_imports_without_pyvisa():
    sys.modules.pop("pyvisa", None)
    import lab_scopes  # noqa: F401
    import lab_scopes.lecroy  # noqa: F401

    assert "pyvisa" not in sys.modules


def test_lecroy_scope_module_does_not_import_pyvisa():
    importlib.import_module("lab_scopes.lecroy.scope")
    assert "pyvisa" not in sys.modules
