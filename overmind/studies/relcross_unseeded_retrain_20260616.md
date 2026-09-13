# RelCross unseeded retrain + OOS validation — 2026-06-16

End-to-end retrain of the RelCross deploy across the full HIP3 universe.
Covered both **seeded** symbols (refresh of already-deployed configs in the
±10/20% neighborhood) and **unseeded** symbols (wider grid over a v3 anchor
template built from `saved_strats/<sym>/`).

Scratch root: `~/scratch/relcross_retrain_20260609/`.
Final deploy configs: `~/scratch/relcross_retrain_20260609/golive/pk_cx_gf{n}_1.json`.

## Scope

- **57 symbols deployed** across gf0/gf1/gf2/gf3/gf3_kor.
- 36 seeded + 14 first-batch unseeded + 7 thin/ad-hoc batch (AVGO, DELL, IBM,
  DKNG, HIMS, LLY, NOW, QNT, URNM, etc.).
- 13 skipped during retrain (BX, COST, GOLD, PLATINUM, EUR, GBP, JP225, EWJ,
  EWT, PALLADIUM signal too weak / too few trades); COPPER and GME further
  pulled after OOS validation revealed failures.
- All deploys use lag-aware sim, precog miss tracking with
  `precog_fastest_possible_rtt=0.4`, BTC heartbeat in `extra_subs`.

## Bug — silent acct-file contamination across same-day sweeps

When running multiple `SimVariations --remote` sweeps in the same day with
the **same workdir basename** (e.g. each at `<sym>/sweep`), they generate
identical `experiment_id`s (`<date>_<host>_simvar-<slug>_<binhash>`),
share `runs/<run_id>/` dirs on the remote workers, and the per-host
`rsync_pull` merges stale acct files from a prior sweep into the current
sweep's `fetched_to`. With variant assignments differing between sweeps,
**~67% of acct files in subsequent sweeps were silently overwritten** by
stale data from earlier sweeps.

Audit: BIRD onward had 335/500 contaminated; BB had 16.7% contamination;
PLATINUM had 0/500 clean variants (all "results" were stale GOLD/COPPER).
Reported `avg_pnl`/`sharpe`/`pct_positive` in `results.txt` were derived
from contaminated acct files, so picks were arbitrarily wrong.

**Mitigation**: use a per-sweep-unique workdir basename so each invocation
gets a distinct `experiment_id`. The launcher uses `<sym>/sweep_<sym>` now.
The deeper bug — `_fetch_run` rsync-pulls into a shared local dir without
`--update` and without `cleanup_after_fetch=True` — is not yet fixed in
`overmind/swarmhost/experiment.py`. Memory:
`feedback_simvariations_unique_workdir_slug.md`.

## Bug — sim winsorization out-of-bounds on 1-trading-day variants

`compute_acct_stats` set `limit_idx = 1` for any variant whose date count
was ≤ 4 days, then called `iloc[limit_idx]`. If `len(all_net_pnls) == 1`
(a variant traded only one day), this raised `IndexError` and crashed the
entire sweep. Fixed in commit `6a7229d7`:

```python
limit_idx = min(1, len(all_net_pnls)-1)
```

## OOS validation (20260605-20260614, 6 weekdays for most)

Re-ran each deployed config single-variant against the post-training window.

- **55/59 symbols positive in OOS** (later trimmed to 57 after COPPER + GME
  pulled).
- Total IS sum: $3,537/day. Total OOS sum: $1,356/day (**38% retention** —
  expected with a 4-week training window).

### Standouts (>$60/d OOS)
| sym | IS pnl | OOS pnl | OOS shp | OOS pct+ |
|---|--:|--:|--:|--:|
| SKHX | $269 | $122.60 | 2.23 | 1.00 |
| SMSN | $258 | $89.17 | 1.80 | 1.00 |
| QNT | $108 | $84.12 | 1.16 | 0.83 |
| BB | $133 | $79.72 | 2.17 | 1.00 |
| HYUNDAI | $73 | $63.17 | 1.31 | 1.00 |
| DKNG | $17 | $60.10 | 1.61 | 1.00 |

### Losses pulled
- **COPPER**: IS $11.95/d sharpe 1.24 → OOS **-$58/d** sharpe -0.97 (2089
  trades/day). Re-search on the OOS period only showed the grid is bimodal:
  IS winners trade heavily (786 trades/d) and lose huge in OOS; OOS winners
  barely trade (10/d, mostly inactive) and lose in IS. No variant viable in
  both periods — likely regime shift in HL-vs-spot basis. Pulled.
- **GME**: IS $56/d → OOS -$2.29/d, 1/6 days positive. Pulled.

### Watch list (>90% IS→OOS drop, still positive)
MSTR, DRAM, SNDK, NATGAS, CRCL, HOOD, MRVL, CBRS, PURRDAT. Likely some
combination of IS-overfit and post-training regime change.

## Param takeaways across the 57 deployed configs

### Strong consensus
- `premium_ema_coef = 1.0` is modal (29/57, 51%). gf1 mega-caps unanimous on
  1.0. Lower values (0.6-0.8) cluster in gf3/gf3_kor. **The hard cap at 1.0
  rule is vindicated.**
- All deploys use `lag_aware_latency=true`, `risk.precog_miss=true`,
  `risk.precog_fastest_possible_rtt=0.4`, BTC heartbeat sub.

### Strong but not universal
- `feed_lag_widen_coef`: **77% on** (44/57); `1.5` is the modal pick
  (35%). **gf2 (US equity big-caps) loves it** (11/22 at 1.5). **gf3
  (smaller equity) frequently turns it off** (8/17 at 0). Hypothesis:
  fast-mover symbols benefit from widening during HL congestion; slow
  movers don't.
- `precog_miss_max_adjust`: split **30 off / 27 active**. gf1 mega-caps
  lean active (5/7 at 0.85); gf3 leans off (10/17 at 1.0).

### Bimodal axes (avoid the middle)
- `premium_tdc_s`: heavily bimodal — long (240/300/1200, 32 syms / 56%)
  vs short (7/15/30, 16 syms / 28%). The 60-120 middle is sparse.
- `base_cross_thresh`: clusters at 0.0001 (11), 0.001 (7), 0.0005 (6).
  gf2 skews tight (0.0001); gf3 has bimodal cluster.

### Mostly symbol-specific
- `vol_norm_tdc`: trimodal 15/60/240 roughly even. No cross-symbol rule.
- `vol_norm_coef`: 1.5 modal, range 1.0-2.0.
- `exit_adjust`: 0.5 (18) and 0.7 (19) cover 65%. 0.3 lower end picked
  only twice — aggressive exits rarely win.
- `remote_mom_coef`: 10/57 chose 0 (off). Median ~0.18, range 0-0.3.

### Machine-group personalities
| group | premium_ema_coef | premium_tdc_s mode | feed_lag_widen | precog response |
|---|---|---|---|---|
| gf0 (commodity) | 1.0 (6/7) | 300 | heavy (1.5 mode) | 50/50 |
| gf1 (mega-caps) | **1.0 (7/7)** | varied | heavy (1.5 mode) | mostly on |
| gf2 (US equity) | varied | 240 mode | heavy (1.5 mode) | 50/50 |
| gf3 (smaller US/ETF) | varied | 240 mode | **often off** (8/17) | mostly off |
| gf3_kor | low (0.6 mode) | 240/300 | mostly off | mostly off |

## Implications for the next sweep

1. **Drop premium_tdc_s 60/120** from the grid — symbols rarely pick them.
2. **Make `feed_lag_widen_coef` essentially a binary on/off arm** rather
   than a continuous axis. Values >0 cluster at 1.0/1.5 with no clear
   reason to prefer either; 0 is a distinct regime.
3. **Hard cap `premium_ema_coef` at 1.0** — already in the rule.
4. **Investigate gf2 vs gf3 feed_lag_widen disagreement** — likely a
   fundamental property (latency exposure, move size) worth a study.
5. **For thin-data symbols** (AVGO/DELL/IBM/NOW/QNT, 3-5 day windows),
   the IS picks landed in the equity cluster recipe (thresh 0.0001-0.0005,
   exit_adjust 0.5-0.7, vnc 1.0-2.0) — not anomalous. Risk is data window,
   not weird params. Deploy small and let live data refine.
6. **Session per symbol, not one launcher window for everything.** The
   20260629 relaunch used a single US-day `pk_template` across all 56
   syms; the 4 gf3_kor syms (HYUNDAI/KR200/SKHX/SMSN) silently produced
   empty `results.txt` because their instrument doesn't trade during
   09:35-17:00 NY. Mirror the RelWide autosearch spec pattern
   (`~/scratch/relwide_retrains/*/spec.py`) which keys `symbols` by
   `"xyz:SYM|SESSION"` and carries a `session` field per sym. The
   RelCross launcher should:
   - group syms by machine assignment (gf0 24h / gf1-3 US day /
     gf3_kor Asia / gf3_jp), or drive off a `symbols` dict with a
     `session` field per entry, and
   - set the pktrader's `start_t`/`end_t` from the session before
     dispatch (see machine settings in `golive/build_per_machine.py`), and
   - verify the lag-aware heartbeat sym is active during the target
     session (HYPE trades 24h so it's fine for HL sessions; would need
     a different heartbeat if a session ever had no HYPE traffic).
7. **OOS window must be forward, not backward.** The initial 20260629
   OOS pass used 20260525-20260531 (week *before* the sweep window) —
   that measures adjacency-consistency, not generalization. Redo with
   dates strictly *after* the sweep end (e.g., 20260627-20260630)
   even if the equity-day count is smaller.

## Safety stance for deploy

All 57 configs initially deployed with `size_mult=1` regardless of the
prior production size_mult (35/57 had been >1 in their prior deploy or
seed source). Backups of pre-override files at
`~/scratch/relcross_retrain_20260609/golive/backup_orig_size_mults/`.
Ramp back up symbol-by-symbol after a few days of live behavior.

## Related commits

- `4e2ac712` — secmaster: add xyz:NOW
- `6a7229d7` — sim: fix winsorization out-of-bounds for variants with 1
  trading day
