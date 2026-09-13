# RelWide CME gf0 Full-Symbol Search — Setup & Playbook

> **Historical record.** Use `overmind/playbooks/cme_retrains_playbook.md`
> for new runs. In particular, do not copy the fixed May/June date ranges or
> assume the historical routing table is still current.

Date: 2026-06-16

This records the fresh RelWideMM2 parameter search across the **full gf0
CME/macro symbol set**, extending the prior 6-symbol package
(`relwide_cme_continuity_20260608.md`, `relwide_cme_search_learnings_20260609.md`)
to the new symbols. It doubles as a playbook for adding new CME/HIP3 symbols to
a relwide search, because several non-obvious blockers showed up.

Canonical workdir: `/home/david/scratch/relwide_cme/david_sims/gf0_fresh_20260616/`
Branch: `more-cme-resims`.

## Goal

Sanity-check sweep (different date range, **20260502–20260531**) for every gf0
symbol, with priority on the symbols that had no prior search:
PLATINUM, PALLADIUM, COPPER, EUR, JPY, GBP. The 6 existing package symbols
(XYZ100, SP500, CL, BRENTOIL, GOLD, SILVER) are also re-swept.

Run order (sequential — see "Host pool is the bottleneck"):
**PLATINUM → PALLADIUM → COPPER → EUR → JPY → GBP → SP500 → CL → GOLD → SILVER
→ XYZ100 → BRENTOIL.**

Depth: `--random-sample 3000` per symbol (grid ~26k–35k), `--chunk-size 50`,
`--remote --resume`.

## Remote-book routing (the key wiring decision)

A symbol's `remote_mid` and tradecall tempo must point at its **remote leading
book**; the relative leg stays BTC/Hyperliquid. What that book is depends on what
data actually exists:

| symbol | remote_symbol | remote_market | notes |
|---|---|---|---|
| XYZ100 | NQ | TopBookCme | |
| SP500 | ES | TopBookCme | |
| CL | CL | TopBookCme | |
| BRENTOIL | BZ | TopBookCme | |
| GOLD | GC | TopBookCme | |
| SILVER | SI | TopBookCme | |
| COPPER | HG | TopBookCme | |
| PLATINUM | PL | TopBookCme | CME data exists despite symbolizer "Pyth-only" comment |
| PALLADIUM | PA | TopBookCme | same |
| EUR | EUR | **TopBookEquity** | no CME FX futures recorded (no 6E relayed) |
| JPY | JPY | **TopBookEquity** | no CME FX futures (no 6J); scale = USDJPY ~160, not inverted |
| GBP | GBP | **TopBookEquity** | no CME FX futures (no 6B); data thin (from 2026-05-19) |

- There is **no recorded CME FX futures data** — `6E/6J` exist in the symbolizer
  db_map but were never relayed (DataBento relay set = `ES,NQ,GC,SI,CL,HG,NG`),
  and `6B` isn't even mapped. So EUR/JPY/GBP route to the FX equity/proxy book.
- `relwide_remote_wiring` in `symbolizer.py` was updated this round to add
  EUR/JPY/GBP (→ TopBookEquity). PL/PA were already present (→ TopBookCme).
- Guardrail unchanged: tradecall clocks on the remote leading book, NEVER BTC.
  Donor configs (saved_strats, the tsla equity clone) ship the BTC-clock bug;
  the builder overwrites it.

## Blockers hit (read this before the next add-a-symbol round)

### 1. Committed merge conflict in `HyperliquidSec.h` → every sim aborts
The pktrade secmaster embeds the Hyperliquid HIP3 `universe`/`mids` as a
compile-time raw-string JSON in `src/pktrade/util/secmaster_lib/HyperliquidSec.h`.
A merge (`9295d8e4`) left **unresolved `<<<<<<< / ======= / >>>>>>>` markers
committed into that JSON**. The embedded JSON then fails to parse →
`hyperliquid_hip3_exchinfo_doc_` is not an object → assert at `secmaster.cc:349`
(`["universe"]`) → abort. This kills ALL sims (CL and PLATINUM alike), not just
new symbols. Fix: resolve the conflicts (we took the `9295d8e4` side, the
newer/complete snapshot) and rebuild. Validate with: extract each `R"(...)"`
block and `json.loads` it; confirm all gf0 symbols appear in both universe+mids.

### 2. Two binaries — `bin/pktrade` (local) vs `jammy.bin/pktrade` (shipped)
After fixing the header you must rebuild BOTH. Local smoke sims use
`bin/pktrade` (via `util.pathing.bindir()`); the **remote sweep ships
`jammy.bin/pktrade`** (built by `./jammybuild.sh`). Rebuilding only `bin/` makes
local smokes pass while every remote job still crashes on the stale secmaster.
SimVariations prints a WARNING when the binary is older than the header — heed it.

### 3. Market data is NOT local by default — pull from S3 with `md_exists`
`syms_go_live_dates` in symbolizer reflects source availability, not local
ingestion. New symbols' books are absent from `~/tardis_datasets/gzpbf/`. Pull
with `overmind/strat_main/tools/md_exists.py` (boto3 profile `l1`, bucket
`s3://l1-pktrade-capture/gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf`). Drive it
non-interactively with `yes y | md_exists.py --pk <config> --start --end` — `--pk`
mode resolves ALL books a config needs: the remote book (e.g. `TopBookCme/PL`),
the **Hyperliquid traded leg** (`Hyperliquid/xyz:PLATINUM_l2Book` + `_trades`),
and BTC. Pulling only the remote book is not enough — the traded leg must be
local too or the symbol produces 0 trades. 20260501 is missing in S3 for all
symbols (a dropped day); harmless since sim dates start 0504.

### 4. FX clones don't quote out of the box (price-scale / threshold)
EUR/JPY/GBP cloned from a TSLA equity config (price ~$410) shipped
`place_thresh=0.002` (~20bp) with `place_thresh_mode!=1`. FX is low-vol, so the
signal almost never cleared → ~1 trade/day. Fix in the builder: for
TopBookEquity symbols set `place_thresh_mode=1` and `place_thresh=0.0001` (~1bp).
A 20260520 scan confirmed healthy quoting then (EUR 35 / JPY 15 / GBP 41 trades).
Price scales were verified to MATCH between remote book and perp for all FX
(EUR ~1.16, GBP ~1.34, JPY ~160) — no inversion. metals/COPPER clone from GOLD
quote fine as-is (PL 60, PA 94, COPPER 74 trades/day).

## ⚠️ Concurrent sweeps collide via a shared remote cache key (DATA CORRUPTION)

Running multiple SimVariations sweeps CONCURRENTLY corrupts results unless each
gets a unique experiment_id. The remote swarmhost caches/serves results keyed on
`(experiment_id, variant_id)` with NO symbol in the key. The experiment_id is
`{date}_{hostname}_{slug}_{binhash}` (see `overmind/swarmhost/experiment.py`
`Experiment.create`), where `slug = _slug("simvar-{workdir_basename}")` and
`binhash` is the pktrade sha. If two symbols use the same workdir basename (we
used `<SYM>/sv` for all → basename "sv") AND the same binary, they share one
experiment_id; and because random sampling is deterministic (seed 42, same
`--random-sample`), they request identical variant IDs → the cache cross-serves
one symbol's sims to another.

Observed 2026-06-20: JPY+GBP+SP500 (concurrent) and CL+GOLD+SILVER (concurrent)
were corrupted — e.g. `CL/sv/scratch/<v>/acct` contained `xyz:GOLD` rows. The 4
symbols run sequentially (PLATINUM/PALLADIUM/COPPER/EUR) were clean.

FIX: give each concurrent run a unique workdir basename so the slug (hence
experiment_id) differs — e.g. workdir `<SYM>/sv_<SYM>`. Then concurrency is safe.
VERIFY after every run: `<SYM>/<wd>/scratch/<variant>/acct_*.csv` line 2 col 2
must equal `xyz:<SYM>`. A quick audit loop over one variant catches it instantly.

## Host pool is the bottleneck → run sequentially (unless experiment_ids are unique)

`overmind/swarmhost/hosts.yaml` = 3 Hetzner boxes, 16 cores each (**48 total**).
Total work is fixed (~60k jobs/symbol ÷ 48 cores), so running symbols
concurrently does NOT finish faster — it oversubscribes the hosts and adds
timeout risk. Run one SimVariations sweep at a time using the full pool; advance
the queue as each completes. Don't rely on chained bash waiters (the harness
kills long bg tasks); monitor and launch the next manually.

## Tooling (reproducible, in the workdir)

- `build_configs.py` — generates each symbol's `pk_base.json` from a corrected
  seed, sets wiring (remote_mid + tradecall on the remote leading book, relative
  BTC), global-replaces donor traded-symbol leftovers, applies the FX base fix,
  and writes `wiring_audit.json`. Seeds: the 6 package symbols from their
  corrected `*_cme_tradecall` candidates; PL/PA/COPPER cloned from the GOLD
  candidate (GC→PL/PA/HG); EUR/JPY/GBP cloned from
  `saved_strats/tsla/pk_tsla_rel_btc_allday.json`.
- `gen_vars.py` — writes per-symbol `vars.json` sweep dims: shared ordex knobs
  (tgt_maxpos_notional, cancel_buffer, premium_tdc_s, per_order_widen_frac,
  pred_momentum_tdc/coef, front_rung_spacing_mult, per_backlevel_rung_spacing_mult)
  plus a per-symbol `place_thresh` band (SP500 0.3–1bp, metals/COPPER ~1–3bp,
  CL/SILVER/BRENT wider, FX 0.5–2bp).

## Launch pattern (per symbol, in tmux)

```bash
cd /home/david/tradefi/retraded_1
B=/home/david/scratch/relwide_cme/david_sims/gf0_fresh_20260616
tmux new-session -d -s <sym>_gf0_20260616 \
  "/home/david/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
   --workdir $B/<SYM>/sv --conf $B/<SYM>/vars.json --pk $B/<SYM>/pk_base.json \
   --start 20260502 --end 20260531 --random-sample 3000 --chunk-size 50 --remote --resume \
   2>&1 | tee $B/<SYM>/sv_launch.log"
```
Always `--dry-run` first to confirm dates/variant count/host pool. `--resume`
makes kills recoverable.

## Status at write time

- Secmaster conflict fixed; `bin/pktrade` rebuilt (smokes pass). **`jammy.bin/pktrade`
  still stale (2026-06-14) — must `./jammybuild.sh` before remote sweeps.**
- All 12 configs built, wiring-audited, data ingested, and confirmed quoting.
- No sweep has completed yet; PLATINUM launch was aborted pending the jammy build.

## Open follow-ups

- Rebuild `jammy.bin/pktrade`, then run the sequential sweep queue.
- Evaluate per the value system; fresh post-IS corrected-clock OOS on picks.
- GBP is data-thin (validation-only) until it accrues more history.
- Consider committing the `HyperliquidSec.h` conflict fix (it was committed broken
  on `main` upstream of this branch — flag to the team).

---

# Update 2026-06-24 — OOS validation, FX sizing failure, live package

## June OOS validation (the picks were sanity-checked out-of-sample)
After the May sweep produced 12 picks, each symbol's TOP-5 (by May avg_pnl) ∪ the
recorded pick were re-simmed over **June 20260602-20260623** (true post-May OOS) on
the swarmhost. Driver: `david_sims/gf0_fresh_20260616/run_june_oos.py` (uses
`sim_dates(remote=True)` per config; results in `june_oos/summary.json`).

OOS mechanics learned:
- `sim_dates(remote=True)` pushes binary+conf but does NOT stage mktdata — you must
  stage June data to the hosts first (mirror SimVariations: discover_for_conf →
  s3_fetch_missing → stage_for_conf per host). The driver does this per symbol.
- Pull June data first via md_exists --pk (S3 had full June 1-23 for remotes+traded
  legs). EXCEPTION: BTC (the relative leg) capture stopped after 0611 in S3.
- BTC absence is OK: sims trade on the CME/FX remote+tradecall regardless; the
  relative-BTC leg just goes quiet. Verified (CL 20260616, BTC absent: 34 trades,
  +19.7). So OOS runs over the full window; don't clip to the BTC range.
- acct CSV columns: net_pnl=idx2, times_traded=idx12 (not 11).

OOS results (pick May->June pnl/pos): HELD = JPY (71.8->77.9/0.87, OOS>IS),
XYZ100 (6.5->27.7/0.93), SP500 (41.8->59.8/0.60), COPPER, CL, PALLADIUM, EUR, GBP.
FAILED = SILVER (21.9->-2.0) and GOLD's turnover pick 3070 (4.2->-0.1). PLATINUM
degraded hard (30->5/0.53). The TOP-5 safety net paid off: GOLD alt 9052 (+17.6 OOS)
and PALLADIUM orig 16986 (27.1/0.80) generalized better than the chosen picks.
Lesson: validate OOS before promoting; May-best != robust. SILVER/PLATINUM were
May-overfit.

## FX SIZING FAILURE (important — root-caused 2026-06-24)
The EUR/JPY/GBP picks were built on a BROKEN sizing setup:
- FX bases were cloned from an equity donor (tsla) with order_size=20, max_pos=200
  (a sane 10:1 ladder ratio) and NO notional caps.
- The shared sweep varied tgt_maxpos_notional, which crashed FX ("both
  tgt_order_notional and tgt_maxpos_notional must be specified together"). The
  workaround was to sweep `order_size` for FX instead — BUT order_size was swept up
  to [..200] then [200..2000] WITHOUT checking/scaling max_pos (fixed at 200).
- Result: every FX pick landed at order_size == max_pos == 200 (ratio 1) — a
  degenerate config that holds exactly ONE clip (no laddered book). The order_size
  "edge pin" at 200 was misread as "wants bigger clips" and the band was widened
  further, when it was really clamping at the position cap.
- Compounding bug: order_size is in UNITS, so order_size=200 meant a ~$232 clip for
  EUR (px~1.16) but a ~$32,000 clip for JPY (px~160) — wildly inconsistent risk
  across the book.

FIX (decided): FX re-sweep on NOTIONAL sizing (consistent with the rest of the book,
auto-normalizes across price scales). Design:
- tgt_order_notional = 1000 (fixed, like CME); tgt_maxpos_notional swept
  [6000,10000,14000] (ratio 6-14, real ladder).
- place_thresh [0.00005,0.0001,0.00015,0.0002,0.0003] + the usual shared dims.
- Window: COMBINED May+June 20260502-20260623 (robust across regimes).
- GATE: smoke-test ONE FX config under notional sizing first (notional THROTTLED FX
  in the first botched attempt — confirm healthy quoting before dispatching 3x3000).
GUARDRAIL going forward: order_size must stay well below max_pos (keep ladder ratio);
when sweeping clip size, sweep/scale the position cap too; never sweep one against a
fixed other without checking. And surface methodology divergences (sizing/wiring/
window) as user decisions — don't pick unilaterally.

## SILVER re-search (in progress)
SILVER's May pick failed June OOS (-2.0), so re-searching SILVER over the combined
May+June window (david_sims/.../SILVER/sv_jun, broad place_thresh band) to find a
config robust across both. Pending.

## Live package 20260624
`consolidated_prelive/gf0_live_package_20260624/configs/pk_fullsearch_0624.json` —
8 symbols going live: BRENTOIL, CL (28818), COPPER (18536), GOLD (41015),
PALLADIUM (16986), PLATINUM (18585), SP500 (18824), XYZ100 (18651). All notional-
sized, wiring-clean (tradecall on remote leading book, not BTC), enabled.
EXCLUDED: EUR/JPY/GBP (FX sizing failure -> pending notional re-sweep);
SILVER (failed OOS -> re-searching). PICKS.md in the sim workdir is the per-symbol
source of truth incl. May & June OOS numbers and all pick swaps/rationale.
