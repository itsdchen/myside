"""
Per-symbol preferred parameter ranges for alpha_rel_wide_mm sweeps.

Derived from prior search results (sweeps 1 and the focused CL/SILVER refine
on 2026-05-03). Used by alpha_relwide_autosearch._build_symbol_spec_entry to
override / inject dim values when a symbol matches.

Override semantics: any leaf name appearing here replaces the preset's spec
for that path with {"values": <list>}. Names not present in any active preset
are appended as new dims (still rooted at ["pktraders", "ordex", <name>]).

To regenerate this file from search results, see the analysis scripts in
the autosearch sweep summary / discussion threads — there is no automated
promotion path yet.
"""

# Each value of an entry is the explicit list of values to sweep. Keys are
# the leaf parameter name (the last element of the override path).
ALPHA_RELWIDE_PREFERRED_DIMS = {
    "xyz:CL": {
        # Sweep A converged on tight quotes (4 bps), slow vol_norm, low alpha_mult.
        "place_thresh": [0.0003, 0.0004, 0.0006, 0.0008],
        "place_thresh_mode": [1, 2],
        "place_thresh_spread_coef": [0.5, 0.75, 1.0],
        "cancel_buffer_frac": [0.1, 0.25, 0.5],
        "lmr_time_const_s": [60],
        "lmr_ema_mult": [1.0],
        "lmr_local_only": [True],
        "pcurvicity": [0.5, 1, 2.5],
        "pcurvicity_coef": [0.5, 0.75],
        "ladder_one_sided": [True],
        "alpha_mult": [0.3, 0.5, 0.7, 0.9],
        "vol_norm_coef": [2, 4],
        "vol_norm_tdc": [40],
        # Momentum block (universal across symbols for the supplementary sweep).
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
    "xyz:COPPER": {
        # First sweep symstats-driven place_thresh; top-30 favored 0.00043032.
        "place_thresh": [0.00021516, 0.00032274, 0.00043032, 0.0005379],
        "place_thresh_mode": [1, 2],
        "place_thresh_spread_coef": [0.25, 0.5, 1.0, 2.0],
        "cancel_buffer_frac": [0.5, 0.75],
        "lmr_time_const_s": [60],
        "lmr_ema_mult": [1.0],
        "lmr_local_only": [True],
        "pcurvicity": [0.5, 1, 2.5],
        "pcurvicity_coef": [0.25, 0.5, 2],
        "ladder_one_sided": [True],
        "alpha_mult": [0.5, 0.9, 1.0],
        "vol_norm_coef": [1, 2, 4],
        "vol_norm_tdc": [10, 40],
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
    "xyz:GOLD": {
        # GOLD top-30 had place_thresh strongly at the lower bound (0.0002, 26/30).
        # GOLD also uniquely prefers fast LMR (lmr_time_const_s=4, opposite of others).
        # vol_norm_coef restored to include 4, pcurvicity_coef swapped 0.5 -> 0.25,
        # alpha_mult swapped 1.1 -> 2.0 to test the bimodal hypothesis (sweep_1
        # healthy distribution had 0.5 (9) and 2.0 (6) as the two strongest values).
        "place_thresh": [0.0001, 0.000125, 0.00015, 0.000175, 0.0002, 0.00025],
        "place_thresh_mode": [1, 2],
        "place_thresh_spread_coef": [0.4, 0.75, 1.0, 2.0],
        "cancel_buffer_frac": [0.1, 0.25, 0.5],
        "lmr_time_const_s": [4],
        "lmr_ema_mult": [1.0],
        # Reverted to True after analysis showed v2's regression came from
        # OTHER narrowings (missing 0.0003, missing 0.1, locked vol_norm_tdc),
        # not from lmr_local_only=True. v1 healthy was 16 True / 14 False —
        # within noise. True kept for consistency with other symbols.
        "lmr_local_only": [True],
        "pcurvicity": [1],
        "pcurvicity_coef": [0.25, 0.75, 2],
        "ladder_one_sided": [True],
        "alpha_mult": [0.5, 0.9, 1.0, 2.0],
        "vol_norm_coef": [0.25, 0.5, 1, 2, 4],
        "vol_norm_tdc": [4, 10],
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
    "xyz:NATGAS": {
        "place_thresh": [0.00011636, 0.00017453, 0.0002, 0.00023271, 0.00029089],
        # NATGAS top-30 strongly leaned mode 1 (23/7 split).
        "place_thresh_mode": [1],
        "place_thresh_spread_coef": [0.25, 0.4, 1.0],
        "cancel_buffer_frac": [0.1, 0.5, 0.75],
        "lmr_time_const_s": [60],
        "lmr_ema_mult": [1.0],
        "lmr_local_only": [True],
        "pcurvicity": [0.5, 1],
        "pcurvicity_coef": [0.25, 0.75],
        "ladder_one_sided": [True],
        "alpha_mult": [0.5, 0.9, 1.1],
        "vol_norm_coef": [0.5, 1, 2],
        "vol_norm_tdc": [10, 40],
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
    "xyz:SILVER": {
        # Sweep A converged on very tight quotes (0.0002 dominant) and sticky orders.
        "place_thresh": [0.0002, 0.0003, 0.0004, 0.0005, 0.00065],
        "place_thresh_mode": [1, 2],
        "place_thresh_spread_coef": [0.25, 0.4, 0.5],
        "cancel_buffer_frac": [0.5, 0.75],
        "lmr_time_const_s": [60],
        "lmr_ema_mult": [1.0],
        "lmr_local_only": [True],
        "pcurvicity": [0.5, 1, 2.5],
        "pcurvicity_coef": [0.25, 0.5],
        "ladder_one_sided": [True],
        "alpha_mult": [0.7, 0.9, 1.0, 1.1],
        "vol_norm_coef": [0.5, 1, 2],
        # Locked to 10 after sweep_b's [4, 40] regressed vs sweep_a's [4, 10].
        # 10 was the strong central value across symbols; sweep_1 SILVER had
        # 4 (14) and 10 (10) tied so 10 alone is a safe single-value lock.
        "vol_norm_tdc": [10],
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
    "xyz:SP500": {
        "place_thresh": [0.00005, 0.00008, 0.0001, 0.00013],
        "place_thresh_mode": [1, 2],
        "place_thresh_spread_coef": [0.4, 0.5, 0.75, 1.0],
        "cancel_buffer_frac": [0.25, 0.5, 0.75],
        "lmr_time_const_s": [60],
        "lmr_ema_mult": [1.0],
        "lmr_local_only": [True],
        "pcurvicity": [0.5, 1],
        "pcurvicity_coef": [0.25, 0.75, 2],
        "ladder_one_sided": [True],
        "alpha_mult": [0.5, 1.0, 1.1],
        "vol_norm_coef": [0.5, 1],
        "vol_norm_tdc": [10, 40],
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
    "xyz:XYZ100": {
        # XYZ100 top-30 leaned mode 1 (19/11) and lmr_local_only True (26/30); lock both.
        "place_thresh": [0.00005, 0.0001, 0.0002, 0.0003, 0.0004],
        "place_thresh_mode": [1],
        "place_thresh_spread_coef": [0.25, 1.0, 2.0],
        "cancel_buffer_frac": [0.25, 0.5],
        "lmr_time_const_s": [60],
        "lmr_ema_mult": [1.0],
        "lmr_local_only": [True],
        "pcurvicity": [0.5, 1],
        "pcurvicity_coef": [0.25, 2],
        "ladder_one_sided": [True],
        "alpha_mult": [0.5, 0.9, 1.0, 1.1],
        "vol_norm_coef": [0.5, 2, 4],
        "vol_norm_tdc": [10, 40],
        "pred_momentum_coef": [0, 1, 2],
        "pred_momentum_tdc": [4, 30],
        "curv_impulse_coef": [0, 2],
        "curv_impulse_tdc": [60],
    },
}
