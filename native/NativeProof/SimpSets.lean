module

public meta import Lean.Meta.Tactic.Simp.RegisterCommand

public meta section

/-! Separate adapter rule sets, initialized before their first registrations. -/
register_simp_attr nativeproof_state
register_simp_attr nativeproof_word
register_simp_attr nativeproof_region
register_simp_attr nativeproof_frame
register_simp_attr nativeproof_logic
