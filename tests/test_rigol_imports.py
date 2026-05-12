def test_rigol_clean_and_legacy_imports():
    from lab_scopes.rigol import RIGOL_WAVEDESC_SIZE, RigolDHO800, RigolScope
    from rigol_dho800 import RigolDHO800 as LegacyRigolDHO800
    from rigol_scope import RigolScope as LegacyRigolScope

    assert RIGOL_WAVEDESC_SIZE == 256
    assert RigolDHO800 is LegacyRigolDHO800
    assert RigolScope is LegacyRigolScope
