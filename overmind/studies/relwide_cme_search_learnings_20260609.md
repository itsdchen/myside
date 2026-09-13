# RelWide CME Search Learnings

> **Historical record.** Use `overmind/playbooks/cme_retrains_playbook.md`
> for current date selection, binary/data gates, and fresh-versus-resume
> policy. Commands below document past experiments only.

Date: 2026-06-09

This study consolidates the RelWideMM2 CME/HIP3 search work across GOLD,
SILVER, CL, BRENTOIL, XYZ100, and SP500. It is meant to preserve the iteration
path, value system, commands, candidate reads, and failure modes so a future
session can resume without re-reading scratch breadcrumbs.

Canonical scratch root:
`/home/david/scratch/relwide_cme`

Companion restart note:
`overmind/studies/relwide_cme_continuity_20260608.md`

## Executive Read

The work started as a broad six-symbol clone-and-retarget RelWideMM2 search.
SP500 was the first robust winner. SILVER later became a strong winner after
focused refinement and stress-day work. BRENTOIL and GOLD became practical but
less clean. CL originally looked bad in corrected OOS, then later produced a
stronger corrected-clock pocket and finally a high-quality refinement row.
XYZ100 was initially flat-to-mildly negative but now has a modest positive,
high-positivity validation candidate.

Best current live-test candidates for the user's immediate CL/XYZ request:

| symbol | config | read |
|---|---|---|
| `xyz:CL` | `/home/david/scratch/relwide_cme/consolidated_prelive/cl/configs/sv9_v1449302_best_quality_286_turnover_pk_full.json` | Strong refinement row: `avg_pnl=31.31`, `pct_positive=0.94`, `286` trades/day, `17` dates. |
| `xyz:XYZ100` | `/home/david/scratch/relwide_cme/consolidated_prelive/xyz100/configs/sv5_v984697_best_positive_170_turnover_pk_full.json` | Promising validation row: `avg_pnl=4.18`, `pct_positive=0.80`, `170` trades/day, `15` dates. |

Best corrected-OOS package before the late CL/XYZ work:
`/home/david/scratch/relwide_cme/consolidated_prelive/live_package_20260602_best_oos_cme_tradecall_no_cl/configs/pk_go_live_best_oos_cme_tradecall_no_cl.json`

That package selected SILVER, SP500, BRENTOIL, and GOLD, and excluded CL because
every saved CL candidate was still negative at that moment.

## Our Value System

The target is not "highest sim_score row." The target is a deployable,
auditable candidate whose edge survives the ways these searches usually fool
us.

Primary values:

- Correct tradecall wiring before trusting any number.
- OOS and corrected-clock evidence outrank raw in-sample sweep results.
- `avg_pnl` / `net_pnl` after commission is the main economic metric.
- `closed_pnl` is useful, but do not promote on closed PnL alone.
- Turnover is good only when net PnL, positivity, and daily path also work.
- Prefer broad positive parameter regions over isolated lucky rows.
- Prefer high positive-day fraction and acceptable worst-day loss.
- Inspect daily rows, not just aggregate rows.
- Stress-day behavior matters, especially for SILVER and CL.
- Save provenance: source vars, source PK, corrected PK, sim results, daily
  rows, validation/audit, and promotion summary.
- Candidate labels should encode what they are: best net, practical turnover,
  stress-day resilient, validation only, etc.

Operational values:

- Use `tmux` for long remote sweeps.
- Use `--resume`; do not delete workdirs unless intentionally starting over.
- Check `tmux ls` and `pgrep -af SimVariations` before restarting to avoid
  duplicate dispatch.
- Keep scratch outputs under `consolidated_prelive` once they are worth saving.
- Store restart commands in a repo study, not only in scratch.

## Non-Negotiable Tradecall Guardrail

The biggest discovered failure mode was an invalid tradecall clock.

Earlier configs had correct CME `remote_mid_*` signals but their
`FinalTempo` tradecall callbacks still subscribed to `BTC / Hyperliquid`. That
means the strategy made trade decisions on BTC final callbacks rather than on
the relevant CME remote symbol callbacks.

For every CME/HIP3 RelWide config, validate:

1. `ordex.remote_sig` points to the intended `remote_mid_*` signal.
2. `remote_mid_*` uses the intended CME symbol on `TopBookCme`.
3. `ordex.trade_caller` points to a `FinalTempo` whose `symbol` and `markets`
   match that CME remote symbol/book.
4. BTC may appear as the relative signal if intended, but BTC must not be the
   tradecall tempo unless that is explicitly the experiment.
5. Save the parsed validation next to the candidate/package.

The corrected-vs-invalid OOS comparison proved this mattered:

| run | net_pnl | closed_pnl | commission | trades |
|---|---:|---:|---:|---:|
| old BTC-clocked invalid | -257.05 | -187.57 | 69.48 | 5023 |
| corrected CME-clocked | 464.17 | 507.22 | 43.04 | 2435 |
| delta | 721.22 | 694.79 | -26.44 | -2588 |

Reference:
`/home/david/scratch/relwide_cme/consolidated_prelive/live_package_20260602_cme_tradecall/reports/postmortem_tradecall_clock.md`

## Iteration Path

### 1. Six-symbol clone-and-retarget search

Initial goal: find profitable RelWideMM2 ordex params for CME-cleared symbols
by cloning an existing RelWideMM2 donor, retargeting the CME remote feed, and
sweeping params.

Donor map:

| symbol | base config | clone from | remote | scratch dir |
|---|---|---|---|---|
| GOLD | `pk_coverage_commodities` | `xyz:COPPER` | GC | `GOLD/` |
| SILVER | `pk_coverage_commodities` | `xyz:COPPER` | SI | `SILVER/` |
| CL | `pk_commodities_cov0313` | `xyz:BRENTOIL` | CL | `CL/` |
| BRENTOIL | `pk_commodities_cov0313` | self | existing | `BRENTOIL/` |
| XYZ100 | `pk_commodities_cov0313` | self | existing | `XYZ100/` |
| SP500 | `pk_commodities_cov0313` | `xyz:XYZ100` | ES | `SP500/` |

Early command pattern:

```bash
coverage_autosearch.py --scan-thresh /home/david/scratch/relwide_cme/<SYM>/spec.py
coverage_autosearch.py --run-spec /home/david/scratch/relwide_cme/<SYM>/spec.py
```

Important tool fixes during this phase:

- Retarget remote after filtering to the target pktrader.
- Narrow remote-symbol substitution to quoted symbol values and suffixes.
- Force `enabled=True` for cloned configs whose donor trader is disabled.
- Use explicit dense threshold scans before full grids.

### 2. Baseline threshold scans

The 2026-05-21 v3 scan used baseline sizing:

- `tgt_order_notional=1000`
- `tgt_maxpos_notional=10000`
- all rung mults near 1
- `place_thresh_mode=1`
- 15-point place-threshold scan dense from sub-1bp through 10bp+

Key result:

| symbol | read |
|---|---|
| SP500 | Clear winner; positive at all thresholds and positive above 200 trades/day. |
| XYZ100 | Mildly negative to flat; tuning candidate, not dead. |
| GOLD | Lossy at volume, near break-even only at very low turnover. |
| CL | Lossy at volume, positive only wide/low-turnover. |
| BRENTOIL | Similar to CL: positive only wide/low-turnover. |
| SILVER | Bad COPPER clone transfer; lost everywhere initially. |

SP500 became the first real target because it had a broad positive operating
band, not a single row.

### 3. Stage-2 and stage-2b parameter sweeps

Stage-2 grid:

- `tgt_maxpos_notional`: `6000..20000`
- `premium_tdc_s`: `[4, 8, 16, 32, 64, 120]`
- `place_thresh`: `[0.00003, 0.00005, 0.00007, 0.0001]`
- 192 variants/symbol

One important error was caught: the first stage-2 attempt used thresholds
`0.0003..0.001`, which was 10x too wide for the intended 0.3-1bp SP500 band.
Those results were discarded and rerun with the corrected decimal.

Stage-2 result:

- SP500 had 142/192 variants both positive and above 200 trades/day.
- XYZ100 still had no positive variants, but losses were mild.
- GOLD, CL, BRENTOIL, and SILVER lost heavily wherever they had volume.

Stage-2b widened symbol-specific threshold bands:

- SP500: 0.3-1bp
- XYZ100: 1-2bp
- GOLD: 1-3bp
- CL/BRENTOIL/SILVER: 5-15bp

New dims:

- `pred_momentum_coef`: `[3, 10, 30]`
- `vol_norm_coef`: `[0, 1, 3]`

The work then shifted from one broad pass to symbol-specific refinement because
the non-SP500 symbols required very different operating bands.

### 4. Candidate consolidation and corrected OOS

Saved candidates were collected under:
`/home/david/scratch/relwide_cme/consolidated_prelive`

All-candidate corrected OOS report:
`/home/david/scratch/relwide_cme/consolidated_prelive/all_candidates_cme_tradecall_oos_20260602/reports/README.md`

Corrected OOS window:
2026-05-20 through 2026-06-01.

Best by symbol from that OOS pass:

| symbol | candidate | net | closed | trades | pos days | worst day |
|---|---|---:|---:|---:|---:|---|
| `xyz:SILVER` | `silver__sv4_v1993_alt_fast_momentum_pk_full` | 339.46 | 341.53 | 438 | 8/9 | -18.67 |
| `xyz:SP500` | `sp500__sv3_v4941_near_200_turnover_pk_full` | 248.15 | 285.44 | 1570 | 9/9 | +1.60 |
| `xyz:BRENTOIL` | `brent__sv2_v237_best_practical_pk_full` | 116.54 | 119.01 | 428 | 6/9 | -33.56 |
| `xyz:GOLD` | `gold__sv8_v246_best_net_pk_full` | 60.90 | 74.38 | 209 | 5/9 | -19.27 |
| `xyz:CL` | `cl__sv1_v1175_best_288_turnover_pk_full` | -83.28 | -79.09 | 734 | 4/9 | -79.39 |

This created the recommended no-CL live package:
`/home/david/scratch/relwide_cme/consolidated_prelive/live_package_20260602_best_oos_cme_tradecall_no_cl`

Selected package candidates:

| symbol | candidate | rationale |
|---|---|---|
| `xyz:SILVER` | `silver__sv4_v1993_alt_fast_momentum_pk_full` | Best corrected OOS net among all candidates, 8/9 positive. |
| `xyz:SP500` | `sp500__sv3_v4941_near_200_turnover_pk_full` | Best SP500 corrected OOS net, 9/9 positive, high turnover. |
| `xyz:BRENTOIL` | `brent__sv2_v237_best_practical_pk_full` | Best Brent corrected OOS net among saved Brent candidates. |
| `xyz:GOLD` | `gold__sv8_v246_best_net_pk_full` | Best Gold corrected OOS net, but less clean than SP500/SILVER. |

CL was excluded at this point. That read was correct for the saved candidate
set, but was later superseded by new CL work.

### 5. CL rescue and refinement

CL initially had many in-sample positive pockets that failed corrected OOS.
The first meaningful CL rescue candidate was:

`/home/david/scratch/relwide_cme/consolidated_prelive/cl/reports/sv4_v85649_best_cme_tradecall_oos.md`

Corrected OOS for `sv_4/v85649`:

| net | closed | trades | positive days | worst day |
|---:|---:|---:|---:|---|
| 265.33 | 270.25 | 845 | 7/9 | 20260521 -37.24 |

Tradecall validation:

- remote signal: `CL / ['TopBookCme']`
- tradecall: `CL / ['TopBookCme']`

This showed CL was not dead, but it also made us more careful: old CL rows were
not trustworthy unless corrected-clock and daily-path evidence existed.

The later CL `sv_9` refinement was restarted/resumed on 2026-06-08:

```bash
cd /home/david/tradefi/retraded_3

tmux new-session -d -s cl_sv9_restart_20260608 \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir /home/david/scratch/relwide_cme/david_sims/cl/sv_9 \
  --conf /home/david/scratch/relwide_cme/david_sims/cl/refinements/vars_refine_quality.json \
  --pk /home/david/scratch/relwide_cme/david_sims/cl/pk_full_cme_tradecall.json \
  --start 20260508 --end 20260602 \
  --random-sample 3000 --chunk-size 50 --remote --resume"
```

Monitoring commands used:

```bash
tmux ls
pgrep -af SimVariations
cat /home/david/scratch/relwide_cme/david_sims/cl/sv_9/progress.json
tail -n 20 /home/david/scratch/relwide_cme/david_sims/cl/sv_9/results.txt
```

The run completed:

- `3000/3000` variants scored
- `38250/38250` jobs completed
- `3` job failures
- best row had `n_dates_ok=17`

Saved CL `sv_9/v1449302`:

| avg_pnl | avg_closed | sharpe | positive | trades/day | dates | sim_score |
|---:|---:|---:|---:|---:|---:|---:|
| 31.31 | 32.99 | 0.77 | 0.94 | 286 | 17 | 70.22 |

Params:

- `tgt_maxpos_notional=14000`
- `place_thresh=0.00028`
- `cancel_buffer=0.4`
- `premium_tdc_s=20`
- `per_order_widen_frac=0.75`
- `pred_momentum_tdc=5`
- `pred_momentum_coef=0.5`
- `vol_norm_coef=0.5`
- `vol_norm_tdc=10`
- `front_rung_spacing_mult=1.5`
- `per_backlevel_rung_spacing_mult=3`
- `ladder_one_sided=True`

Saved artifacts:

- `/home/david/scratch/relwide_cme/consolidated_prelive/cl/reports/sv9_v1449302_best_quality_286_turnover.md`
- `/home/david/scratch/relwide_cme/consolidated_prelive/cl/configs/sv9_v1449302_best_quality_286_turnover_pk.json`
- `/home/david/scratch/relwide_cme/consolidated_prelive/cl/configs/sv9_v1449302_best_quality_286_turnover_pk_full.json`
- `/home/david/scratch/relwide_cme/consolidated_prelive/cl/configs/sv9_v1449302_best_quality_286_turnover_sim_results.json`
- `/home/david/scratch/relwide_cme/consolidated_prelive/cl/configs/sv9_v1449302_best_quality_286_turnover_promotion_summary.json`

Read: this is the best current CL candidate for a live test.

### 6. XYZ100 refinement

XYZ100 began as a flat-to-mildly-negative symbol. It was not rejected because
losses were modest and the symbol seemed tunable.

The `sv_5` refinement was restarted/resumed on 2026-06-08:

```bash
cd /home/david/tradefi/retraded_3

tmux new-session -d -s xyz100_sv5_restart_20260608 \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5 \
  --conf /home/david/scratch/relwide_cme/david_sims/xyz100/refinements/vars_refine_quality.json \
  --pk /home/david/scratch/relwide_cme/david_sims/xyz100/pk_full_cme_tradecall.json \
  --start 20260504 --end 20260523 \
  --random-sample 2000 --chunk-size 50 --remote --resume"
```

Monitoring commands used:

```bash
tmux ls
pgrep -af SimVariations
cat /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5/progress.json
tail -n 20 /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5/results.txt
```

The run completed:

- `2000/2000` variants scored
- `21750/21750` jobs completed
- `1` job failure
- best row had `n_dates_ok=15`

Saved XYZ100 `sv_5/v984697`:

| avg_pnl | avg_closed | sharpe | positive | trades/day | dates | sim_score |
|---:|---:|---:|---:|---:|---:|---:|
| 4.18 | 5.19 | 0.45 | 0.80 | 170 | 15 | 9.77 |

Params:

- `tgt_maxpos_notional=16000`
- `place_thresh=0.00018`
- `cancel_buffer=0.75`
- `front_rung_spacing_mult=1.0`
- `max_back_levels=3`
- `per_backlevel_rung_spacing_mult=2`
- `premium_tdc_s=4`
- `per_order_widen_frac=1.2`
- `order_decrease_coef=0.7`
- `pred_momentum_tdc=1`
- `pred_momentum_coef=10`
- `vol_norm_coef=1.0`
- `vol_norm_tdc=30`
- `ladder_one_sided=False`

Saved artifacts:

- `/home/david/scratch/relwide_cme/consolidated_prelive/xyz100/reports/sv5_v984697_best_positive_170_turnover.md`
- `/home/david/scratch/relwide_cme/consolidated_prelive/xyz100/configs/sv5_v984697_best_positive_170_turnover_pk.json`
- `/home/david/scratch/relwide_cme/consolidated_prelive/xyz100/configs/sv5_v984697_best_positive_170_turnover_pk_full.json`
- `/home/david/scratch/relwide_cme/consolidated_prelive/xyz100/configs/sv5_v984697_best_positive_170_turnover_sim_results.json`
- `/home/david/scratch/relwide_cme/consolidated_prelive/xyz100/configs/sv5_v984697_best_positive_170_turnover_promotion_summary.json`

Read: this is promising because it combines positive net, high positivity, and
near-200 turnover. It is still weaker than CL and should be treated as a small
live validation candidate unless subsequent OOS strengthens it.

## Symbol-Specific Learnings

### SP500

SP500 was the first robust result. It was positive across the initial threshold
scan and retained strong corrected OOS.

Useful patterns:

- `per_order_widen_frac=0.35`
- `premium_tdc_s=20/30`
- `pred_momentum_coef=2/3`
- `front_rung_spacing_mult=0.5`
- moderate `cancel_buffer`

Preferred candidates:

- `sp500__sv3_v4941_near_200_turnover_pk_full`: corrected OOS net `248.15`,
  closed `285.44`, `1570` trades, `9/9` positive days.
- `sp500__sv3_v29_best_overall_pk_full`: corrected OOS net `247.10`, cleaner
  stability, lower turnover.
- `sp500__sv1_v13_gt_200_turnover_pk_full`: corrected OOS net `196.98`.

### SILVER

SILVER looked bad in the initial COPPER-clone scan but became one of the best
symbols after focused refinement. The key lesson is not "maximize turnover";
it is "find the stress-day-resilient pocket."

Preferred candidates:

- `silver__sv4_v1993_alt_fast_momentum_pk_full`: corrected OOS net `339.46`,
  closed `341.53`, `438` trades, `8/9` positive days.
- `silver__sv4_v1450_best_0515_resilient_pk_full`: corrected OOS net `264.21`,
  `7/9` positive, smaller worst day.
- `silver__sv4_v465_higher_turnover_pk_full`: corrected OOS net `233.47`, but
  higher turnover and rougher worst day.

### BRENTOIL

Brent is practical, not pristine. The selected candidate was chosen for
meaningful turnover plus positive OOS, not because it had the cleanest daily
path in the universe.

Preferred candidates:

- `brent__sv2_v237_best_practical_pk_full`: corrected OOS net `116.54`,
  closed `119.01`, `428` trades, `6/9` positive.
- `brent__sv2_v257_alt_134_turnover_pk_full`: corrected OOS net `100.27`,
  `408` trades, `7/9` positive.

### GOLD

GOLD is viable in lower/moderate-turnover pockets. It is not yet a high-volume
edge. The `sv12` curv/no-curv set is still important for validation, but the
best corrected OOS pick was `sv8_v246`.

Preferred/validation candidates:

- `gold__sv8_v246_best_net_pk_full`: corrected OOS net `60.90`, closed `74.38`.
- `gold__sv12_v1914_best_curv_pk_full`: corrected OOS net `55.35`.
- `gold__sv12_v1407_curv3_pk_full`: corrected OOS net `44.52`, `7/9` positive.
- `gold__sv12_v122_best_no_curv_pk_full`: corrected OOS net `37.15`.

Practical priors:

- `ladder_one_sided=true`
- `min_ord_lifetime=0.5`
- `vol_norm_coef=1.5`
- `max_back_levels=3`
- `place_thresh=0.00015` for quality pockets
- `cancel_buffer=0.2/0.25`
- `premium_tdc_s=8/16`
- `pred_momentum_coef=2.0`

### CL

CL is the clearest example of why corrected OOS and tradecall audit matter.
Early saved CL candidates failed corrected OOS, but later CL work found a real
positive pocket. Current best is `sv_9/v1449302`.

Do not generalize from old "CL is bad" notes. The better statement is:

- Old CL candidates were bad after correcting tradecall.
- `sv4_v85649` showed a corrected-clock positive pocket.
- `sv9_v1449302` is now the best current CL live-test candidate by quality,
  positivity, and turnover.

### XYZ100

XYZ100 is no longer "dead," but it is still a cautious validation symbol. The
best current row has modest positive net, high positivity, and useful turnover.
It should be live-tested smaller than CL unless explicitly running an
exploration.

Current best:

- `sv5_v984697_best_positive_170_turnover`: `avg_pnl=4.18`,
  `pct_positive=0.80`, `170` trades/day, `15` dates.

Important nearby read:

- There was an XYZ100 row with higher `avg_pnl` and about `188` trades/day, but
  lower positivity (`0.67`) and lower score. Given our values, the `v984697`
  high-positivity row was saved.

## Commands Worth Remembering

Broad search:

```bash
coverage_autosearch.py --scan-thresh /home/david/scratch/relwide_cme/<SYM>/spec.py
coverage_autosearch.py --run-spec /home/david/scratch/relwide_cme/<SYM>/spec.py
```

Restart CL:

```bash
cd /home/david/tradefi/retraded_3

tmux new-session -d -s cl_sv9_restart_YYYYMMDD \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir /home/david/scratch/relwide_cme/david_sims/cl/sv_9 \
  --conf /home/david/scratch/relwide_cme/david_sims/cl/refinements/vars_refine_quality.json \
  --pk /home/david/scratch/relwide_cme/david_sims/cl/pk_full_cme_tradecall.json \
  --start 20260508 --end 20260602 \
  --random-sample 3000 --chunk-size 50 --remote --resume"
```

Restart XYZ100:

```bash
cd /home/david/tradefi/retraded_3

tmux new-session -d -s xyz100_sv5_restart_YYYYMMDD \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5 \
  --conf /home/david/scratch/relwide_cme/david_sims/xyz100/refinements/vars_refine_quality.json \
  --pk /home/david/scratch/relwide_cme/david_sims/xyz100/pk_full_cme_tradecall.json \
  --start 20260504 --end 20260523 \
  --random-sample 2000 --chunk-size 50 --remote --resume"
```

Monitor:

```bash
tmux ls
pgrep -af SimVariations
cat /home/david/scratch/relwide_cme/david_sims/cl/sv_9/progress.json
cat /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5/progress.json
tail -n 20 /home/david/scratch/relwide_cme/david_sims/cl/sv_9/results.txt
tail -n 20 /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5/results.txt
```

Inspect saved candidates:

```bash
find /home/david/scratch/relwide_cme/consolidated_prelive -maxdepth 3 -type f \
  \( -name '*promotion_summary.json' -o -name '*.md' \) | sort

jq . /home/david/scratch/relwide_cme/consolidated_prelive/cl/configs/*promotion_summary.json
jq . /home/david/scratch/relwide_cme/consolidated_prelive/xyz100/configs/*promotion_summary.json
```

Read current reports:

```bash
sed -n '1,180p' /home/david/scratch/relwide_cme/consolidated_prelive/cl/reports/sv9_v1449302_best_quality_286_turnover.md
sed -n '1,180p' /home/david/scratch/relwide_cme/consolidated_prelive/xyz100/reports/sv5_v984697_best_positive_170_turnover.md
sed -n '1,220p' /home/david/scratch/relwide_cme/consolidated_prelive/all_candidates_cme_tradecall_oos_20260602/reports/README.md
```

## Result Reading Checklist

Use this order before promotion:

1. Confirm expected `n_dates_ok`.
2. Confirm tradecall audit: remote and tradecall both match CME `TopBookCme`.
3. Rank on net/avg PnL after commission.
4. Check `pct_positive`.
5. Check trade count and fillrate.
6. Inspect daily path and worst day.
7. Compare nearby rows if available; broad regions beat one-row wins.
8. Save source vars, source PK, PK full, sim results, daily rows, and report.
9. If building a multi-symbol package, run combined-book OOS after individual
   selection. Summed individual OOS is useful but not final.

## Open Follow-Ups

- Run a fresh corrected-clock OOS specifically for `CL sv9/v1449302`.
- Run a fresh corrected-clock OOS specifically for `XYZ100 sv5/v984697`.
- If CL remains strong, update the prior no-CL package rather than relying on
  the 2026-06-02 "exclude CL" decision.
- Keep XYZ100 smaller or validation-only until it has stronger OOS evidence.
- Consider a combined-book OOS package once the CL/XYZ live-test rows are folded
  into the candidate set.
