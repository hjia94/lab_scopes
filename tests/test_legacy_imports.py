def test_legacy_import_modules_resolve():
    from LeCroy_Scope import EXPANDED_TRACE_NAMES, WAVEDESC_SIZE, LeCroy_Scope
    from LeCroy_Scope_Header import LeCroy_Scope_Header
    from read_scope_data import read_trc_data_simplified
    from rigol_dho800 import RigolDHO800
    from rigol_scope import RigolScope

    assert WAVEDESC_SIZE == 346
    assert EXPANDED_TRACE_NAMES["C1"] == "Channel1"
    assert LeCroy_Scope.__name__ == "LeCroyScope"
    assert LeCroy_Scope_Header.__name__ == "LeCroyHeader"
    assert callable(read_trc_data_simplified)
    assert RigolDHO800.__name__ == "RigolDHO800"
    assert RigolScope.__name__ == "RigolScope"
