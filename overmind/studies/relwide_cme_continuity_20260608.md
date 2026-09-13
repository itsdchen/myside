# RelWide CME Continuity Notes

> **Historical record.** Use `overmind/playbooks/cme_retrains_playbook.md`
> for new runs. The restart commands below apply only to their original run
> identities and fixed dates.

Date written: 2026-06-08

This note exists so a future assistant can resume the CME RelWide workstream
without rediscovering the same breadcrumbs. Canonical scratch root:
`/home/david/scratch/relwide_cme`.

## Current State

The active work is CME/HL `RelWideMM2` refinement for `xyz:CL` and
`xyz:XYZ100`, using corrected CME tradecall configs and remote swarmhost
SimVariations.

Active tmux sessions started on 2026-06-08:

```bash
tmux attach -t cl_sv9_restart_20260608
tmux attach -t xyz100_sv5_restart_20260608
```

Monitor:

```bash
cat /home/david/scratch/relwide_cme/david_sims/cl/sv_9/progress.json
cat /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5/progress.json
tail -n 20 /home/david/scratch/relwide_cme/david_sims/cl/sv_9/results.txt
tail -n 20 /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5/results.txt
```

Resume commands if the machine or tmux dies:

```bash
cd /home/david/tradefi/retraded_3

tmux new-session -d -s cl_sv9_restart_YYYYMMDD \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir /home/david/scratch/relwide_cme/david_sims/cl/sv_9 \
  --conf /home/david/scratch/relwide_cme/david_sims/cl/refinements/vars_refine_quality.json \
  --pk /home/david/scratch/relwide_cme/david_sims/cl/pk_full_cme_tradecall.json \
  --start 20260508 --end 20260602 \
  --random-sample 3000 --chunk-size 50 --remote --resume"

tmux new-session -d -s xyz100_sv5_restart_YYYYMMDD \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir /home/david/scratch/relwide_cme/david_sims/xyz100/sv_5 \
  --conf /home/david/scratch/relwide_cme/david_sims/xyz100/refinements/vars_refine_quality.json \
  --pk /home/david/scratch/relwide_cme/david_sims/xyz100/pk_full_cme_tradecall.json \
  --start 20260504 --end 20260523 \
  --random-sample 2000 --chunk-size 50 --remote --resume"
```

`SimVariations.py` random sampling is deterministic (`random.seed(42)`), so
using the same `--random-sample` value with `--resume` targets the same variant
IDs and loads existing `scratch/<variant>/sim_results.json`.

XYZ100 may prompt about missing 2026-05-01 Hyperliquid mktdata. The active run
dates are 2026-05-04 through 2026-05-22, and the prior run proceeded past the
same warning. It is acceptable to answer `y` unless the requested date range
has changed to include 2026-05-01.

## Value System

We are not trying to find the highest in-sample row. We care about deployable,
auditable candidates:

- Correct tradecall wiring before any metric is trusted.
- OOS and corrected-clock results outrank initial sweep results.
- `avg_pnl` / `net_pnl` after commission is the main economic metric.
- `closed_pnl` is useful but does not include commission; do not promote on
  closed PnL alone.
- Turnover matters, but only if the PnL survives commission and daily path
  checks.
- Prefer broad positive regions over single lucky rows.
- Prefer candidates with many positive days and small worst-day losses.
- Stress-day behavior matters, especially for SILVER and CL.
- High turnover with low `pct_positive`, bad worst days, or high adverse
  selection is not a win.
- Saved packages must include provenance: source config, corrected config,
  validation/audit, OOS summary, and daily rows.

## Non-Negotiable Tradecall Guardrail

The largest known failure mode was an invalid tradecall clock. An earlier
combined go-live/OOS config had correct CME remote signals but `FinalTempo`
tradecall tempos still subscribed to `BTC / Hyperliquid`. That made decisions
on BTC final callbacks instead of the relevant CME remote symbol callbacks.

For every CME/HL RelWide config, validate all of these before OOS or promotion:

1. `ordex.remote_sig` points to the intended `remote_mid_*` signal.
2. `remote_mid_*` uses the intended CME symbol and `TopBookCme`.
3. `ordex.trade_caller` points to a `FinalTempo` whose `symbol` and `markets`
   match the same CME remote symbol/book.
4. `relative_sig` / `rel_mid_*` is separate; BTC may appear there, but BTC must
   not be the tradecall tempo unless explicitly intended.
5. Store the parsed tradecall audit next to the candidate/package.

Reference postmortem:
`/home/david/scratch/relwide_cme/consolidated_prelive/live_package_20260602_cme_tradecall/reports/postmortem_tradecall_clock.md`

Correcting only the tradecall tempos changed the combined OOS read from
negative to strongly positive:

| run | net_pnl | closed_pnl | trades |
|---|---:|---:|---:|
| invalid BTC-clocked | -257.05 | -187.57 | 5023 |
| corrected CME-clocked | 464.17 | 507.22 | 2435 |

Treat any OOS result as invalid if this audit was not done first.

## Main Directories

- Top-level progress: `/home/david/scratch/relwide_cme/PROGRESS.md`
- Per-symbol sweep roots: `/home/david/scratch/relwide_cme/{GOLD,SILVER,CL,BRENTOIL,XYZ100,SP500}`
- User refinement roots: `/home/david/scratch/relwide_cme/david_sims/{cl,xyz100,sp500,brent,silver}`
- Candidate consolidation: `/home/david/scratch/relwide_cme/consolidated_prelive`
- Corrected all-candidate OOS:
  `/home/david/scratch/relwide_cme/consolidated_prelive/all_candidates_cme_tradecall_oos_20260602`
- Recommended no-CL package:
  `/home/david/scratch/relwide_cme/consolidated_prelive/live_package_20260602_best_oos_cme_tradecall_no_cl`

## Corrected Candidate OOS Snapshot

Corrected OOS window: 2026-05-20 through 2026-06-01.

Source report:
`/home/david/scratch/relwide_cme/consolidated_prelive/all_candidates_cme_tradecall_oos_20260602/reports/README.md`

Best by symbol from the corrected all-candidate OOS:

| sym | best slug | net | closed | trades | pos days | worst |
|---|---|---:|---:|---:|---:|---|
| `xyz:SILVER` | `silver__sv4_v1993_alt_fast_momentum_pk_full` | 339.46 | 341.53 | 438 | 8/9 | 20260529 -18.67 |
| `xyz:SP500` | `sp500__sv3_v4941_near_200_turnover_pk_full` | 248.15 | 285.44 | 1570 | 9/9 | 20260521 +1.60 |
| `xyz:BRENTOIL` | `brent__sv2_v237_best_practical_pk_full` | 116.54 | 119.01 | 428 | 6/9 | 20260522 -33.56 |
| `xyz:GOLD` | `gold__sv8_v246_best_net_pk_full` | 60.90 | 74.38 | 209 | 5/9 | 20260529 -19.27 |
| `xyz:CL` | `cl__sv1_v1175_best_288_turnover_pk_full` | -83.28 | -79.09 | 734 | 4/9 | 20260521 -79.39 |

The recommended 2026-06-02 package excluded CL because all saved CL candidates
were negative in corrected OOS at that point:
`/home/david/scratch/relwide_cme/consolidated_prelive/live_package_20260602_best_oos_cme_tradecall_no_cl`.

Important update: a later CL candidate exists:
`/home/david/scratch/relwide_cme/consolidated_prelive/cl/reports/sv4_v85649_best_cme_tradecall_oos.md`.
It showed corrected OOS net `+265.33`, closed `+270.25`, `845` trades, `7/9`
positive days, worst day `20260521 -37.24`. This supersedes the older blanket
"CL is bad" read, but it still needs the same caution and current refinement
validation.

## Symbol Reads

### SP500

SP500 was the original robust winner. It was positive across threshold scans,
had a broad positive region, and corrected OOS stayed strong.

Preferred candidates:
- `sv_3/v29`: best quality/stability, 10/10 positive original days,
  corrected OOS net 247.10.
- `sv_3/v4941`: high-quality near-200 turnover, corrected OOS net 248.15,
  9/9 positive days, 1570 OOS trades.
- `sv_1/v13`: strict >200-trade profile, corrected OOS net 196.98.

Parameter lessons: `per_order_widen_frac=0.35`, `premium_tdc_s=20/30`,
`pred_momentum_coef=2/3`, `front_rung_spacing_mult=0.5`, and moderate
`cancel_buffer` worked well.

### SILVER

SILVER became strong only after focused stress-day work. The value here is
moderate turnover and resilience, not maximum turnover.

Preferred candidates:
- `sv4_v1993_alt_fast_momentum`: best corrected OOS net, 339.46, 8/9 positive.
- `sv4_v1450_best_0515_resilient`: balanced stress-day-resilient candidate.
- `sv4_v465_higher_turnover`: higher turnover but less clean.

Avoid rows above roughly 200 average trades unless they prove they do not give
back too much on the stress day.

### BRENTOIL

Brent is practical but not as clean as SP500/SILVER. Selection used meaningful
turnover first, then `pct_positive`, then net/closed PnL. Low-trade top-PnL
rows were intentionally excluded.

Preferred corrected-OOS candidate:
- `sv2_v237_best_practical`: net 116.54, 428 trades, 6/9 positive.

Alternates:
- `sv2_v257_alt_134_turnover`: net 100.27, 408 trades, 7/9 positive.

### GOLD

GOLD is viable only in lower/moderate turnover pockets. It is not a high-volume
edge yet. Best corrected OOS was `sv8_v246`, but the `sv12` curv/no-curv set is
still important for validation.

Validation set to keep around:
- `sv12_v1914_best_curv`
- `sv12_v122_best_no_curv`
- `sv12_v1407_curv3`
- `sv8_v56_high_turnover_best`
- `sv8_v246_best_net`
- `sv10_v28_fast_premium_best_net`

Practical GOLD priors: `ladder_one_sided=true`, `min_ord_lifetime=0.5`,
`vol_norm_coef=1.5`, `max_back_levels=3`, `place_thresh=0.00015` for quality,
`cancel_buffer=0.2/0.25`, `premium_tdc_s=8/16`, `pred_momentum_coef=2.0`.
Curv impulse is candidate-specific, not proven as a broad global win.

### CL

CL had many in-sample positive pockets that failed corrected OOS. Do not trust
old CL promotion candidates unless they have corrected CME tradecall OOS.

Old saved CL candidates were negative in the 2026-06-02 corrected OOS report.
Later `sv4_v85649` changed the picture:

- corrected OOS net `+265.33`
- closed `+270.25`
- `845` trades
- `7/9` positive days
- worst day `-37.24`
- params include `place_thresh=0.00025`, `cancel_buffer=0.4`,
  `premium_tdc_s=5`, `per_order_widen_frac=0.75`,
  `pred_momentum_tdc=10`, `pred_momentum_coef=1`, `vol_norm_coef=2`,
  `vol_norm_tdc=30`, `front_rung_spacing_mult=1.25`,
  `per_backlevel_rung_spacing_mult=4`, `ladder_one_sided=true`.

Current `cl/sv_9` refinement is meant to validate/refine this newer CL pocket.
Judge it by corrected OOS-like dates, daily path, and whether the positive
region is broad. Do not promote a single high-turnover CL row without daily
and tradecall audit checks.

### XYZ100

Early scans were mildly negative to flat, but this was treated as a tuning
candidate rather than a dead symbol. Current `xyz100/sv_5` is a quality
refinement using corrected CME tradecall config. Evaluate it like CL: broad
positive region, not just one best row.

## How To Read Results

`results.txt` columns normally include:
`variant`, swept params, `avg_pnl`, `avg_closed_pnl`, `sharpe`,
`pct_positive`, `num_trds`, `fillrate`, `n_dates_ok`, `sim_score`.

Practical ranking process:

1. Filter out `n_dates_ok` below the expected date count.
2. Filter or flag candidates with low `pct_positive`.
3. Check `avg_pnl` first, not `avg_closed_pnl`.
4. Check trade count against the symbol's purpose:
   - SP500 can support higher turnover.
   - SILVER/GOLD may be better at moderate turnover.
   - CL high turnover needs extra skepticism.
5. Inspect daily rows / `sim_results.json` for worst-day and stress-day path.
6. Validate tradecall wiring before calling anything OOS.
7. Prefer candidates whose nearby parameter neighborhood also works.

## Restart And Cleanup Discipline

- Use `tmux` for any long remote sweep.
- Use `--resume`; do not delete workdirs unless explicitly starting a new run.
- Before restarting, check `pgrep -af SimVariations` and `tmux ls` to avoid
  double-dispatch.
- If a foreground run must be stopped, prefer `SIGINT` so SimVariations kills
  active remote `pktrade` procs. Avoid hard kills unless remote cleanup is
  handled separately.
- A resumed run may create a new `experiment_id` but reuse cached variant
  results. That is fine.
- Remote progress counts in a resumed run count the remaining pending variants,
  while `n_variants_scored` includes cached variants already loaded.

## Open Questions

- Does CL `sv_9` confirm `sv4_v85649` as a robust region or was it a narrow OOS
  pocket?
- Does XYZ100 `sv_5` finally find a positive corrected-clock pocket?
- Should the no-CL live package be revised if CL `sv_9` validates?
- Should the final package be run as a combined-book OOS after selecting the
  per-symbol bests? Yes; summed individual OOS is useful but not final.
