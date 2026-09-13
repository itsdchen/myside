# Trade Manager

Single CLI for two things:
1. **Trademan** — fetch live data, report on how things performed, analyze log events
2. **Sim vs Live** — re-run sims against dated configs and compare to live results

All commands: `python trade_manager.py <command> [flags]`

There is also a separate **Post Trade Analysis** tool — `pta.py` — for outlier
flagging and per-day forensic drill-downs. See Part 3 below.

---

## Part 1: Trademan — performance reporting & analysis

### fetch — download data from remote machines

```bash
python trade_manager.py fetch                          # last 3 days, all machines
python trade_manager.py fetch --days 7                 # last 7 days
python trade_manager.py fetch --days 5 --aliases gf0,gf1
```

Files saved to `~/scratch/tradeperf/{alias}/{group}/{strat}/`. Already-downloaded files are skipped.

### report — performance reports from local data

```bash
python trade_manager.py report                         # all local data
python trade_manager.py report --days 5                # last 5 days
python trade_manager.py report --strat allday_v1       # filter by strat
python trade_manager.py report --symbol xyz:GOLD       # filter by symbol
python trade_manager.py report --days 7 --aliases gf0 --symbol xyz:GOLD
```

Report sections: summary by strat, daily PnL pivot, summary by symbol, alerts, config changes.

### fetch-report — fetch then report

```bash
python trade_manager.py fetch-report --days 5
python trade_manager.py fetch-report --days 7 --aliases gf0 --symbol xyz:GOLD
```

### email-report — fetch, report, and email

```bash
python trade_manager.py email-report --days 5
```

Requires email config in `CONFIG` dict.

### log-events — parse INFO logs for actionable events

Parses glog-format INFO logs for usermsgs, position inheritance, risk level transitions, cancel reject pauses, and more.

```bash
# All events for a date
python trade_manager.py log-events --date 20260212 --aliases gf1

# Filter to specific strat
python trade_manager.py log-events --date 20260212 --aliases gf1 --strat usday

# Date range
python trade_manager.py log-events --date-range 20260210:20260213 --aliases gf1

# Only usermsg and inherit events
python trade_manager.py log-events --date 20260212 --aliases gf1 --types usermsg,inherit
```

**Event types:** `usermsg`, `usermsg_ignored`, `usermsg_effect`, `inherit`, `inherit_calc`, `risk_level`, `cancel_pause`, `feed_latency`, `reject` (cancel_reject + oe_ack_not_found + oe_erase_nonexistent), `overnight_pos`, `size_mult`

| Flag | Description |
|---|---|
| `--date YYYYMMDD` | Date (default: today) |
| `--date-range START:END` | Date range (YYYYMMDD:YYYYMMDD) |
| `--aliases gf0,gf1` | SSH aliases to scan |
| `--strat SUBSTR` | Filter strats by substring |
| `--group SUBSTR` | Filter groups by substring |
| `--types usermsg,inherit` | Comma-separated event types to show (see list above) |

### order-analysis — RTT and reject analysis from orders files

Parses `orders_*.csv` files for round-trip times (NewOrd→NewOrdAck, Cancel→CancelAck) and reject counts. Also included automatically in `report` output.

```bash
# Last 3 days, all machines
python trade_manager.py order-analysis

# Single date, specific alias/strat
python trade_manager.py order-analysis --date 20260212 --aliases gf1 --strat usday

# Date range
python trade_manager.py order-analysis --date-range 20260210:20260213 --aliases gf1
```

Shows per-symbol RTT percentiles (p50/p75/p90/p99/max), cancel reject and new order reject counts.

| Flag | Description |
|---|---|
| `--days N` | Number of recent days (default: 3) |
| `--date YYYYMMDD` | Single date |
| `--date-range START:END` | Date range (YYYYMMDD:YYYYMMDD) |
| `--aliases gf0,gf1` | SSH aliases to scan |
| `--strat SUBSTR` | Filter strats by substring |
| `--group SUBSTR` | Filter groups by substring |

### trends — RTT (ALO/IOC) and fill-rate evolution over time

How order round-trip times and fill rates change **over a window**, one row per
date, split by order type. Order RTT is bucketed using the `ADD`/`REM` marker on
each NewOrd line — `ADD` = passive/maker (resting, ALO), `REM` = aggressive/taker
(crossing, IOC) — so the two latency profiles never get mixed:

- **ADD (`a_*`)** RTT = `NewOrd → NewOrdAck` (time to acknowledge a resting order).
- **REM (`r_*`)** RTT = `NewOrd → earliest terminal event` (`Exec` / `Elimination`
  / `NewOrderReject`). IOC orders never get an ack — they live until they fill,
  get eliminated, or get rejected — so this is the equivalent "time to hear back".
  Earliest terminal is used because a partial fill's `Elimination` can log a few
  ms before its `Exec`.

Fill rate is recomputed from acct files (`fill% = shs_traded/shs_sent`);
`reach%`/`ioc%` come from the acct file (2dp).

Prints a **strat-level rollup** then **one table per symbol** (symbols vary
wildly, so this is the point). Scope with the filters below; with no filters it
prints every strat/symbol over the window.

```bash
# Default: last 14 days, all strats, strat rollup + per-symbol tables
python trade_manager.py trends

# One strat, three weeks, just the rollups (no per-symbol)
python trade_manager.py trends --strat rel_nq_allday --days 21 --strat-only

# Specific symbols on one machine over a date range
python trade_manager.py trends --aliases gf3 --strat rel_nq_allday \
    --date-range 20260601:20260620 --symbol xyz:GME,xyz:MRVL
```

Columns: `rtts` (round-trip sample count = ADD acks + REM terminals), `a_p50`/`a_p99`
(ADD RTT ms), `r_p50`/`r_p99` (REM RTT ms), `cxl50` (Cancel→CancelAck p50),
`fill%`, `reach%`, `ioc%`.

| Flag | Description |
|---|---|
| `--days N` | Number of recent days (default: 14) |
| `--date YYYYMMDD` | Single date |
| `--date-range START:END` | Date range (YYYYMMDD:YYYYMMDD) |
| `--aliases gf0,gf1` | SSH aliases to scan |
| `--strat SUBSTR` | Filter strats by substring |
| `--group SUBSTR` | Filter groups by substring |
| `--symbol xyz:NVDA,...` | Comma-separated exact symbols |
| `--strat-only` | Strat rollups only, skip per-symbol tables |
| `--email` | Email the report in addition to printing |
| `--to addr` | Override email recipient (default: group list) |

---

## Part 2: Sim vs Live — simulation evaluation

### sim-eval — run sims and compare against live

Fetches dated configs from remote machines, downloads market data, runs sims, and compares against live results.

```bash
# Single day, interactive strat/sym selection
python trade_manager.py sim-eval --date 20260212

# Filter to specific strats, auto-select all matching pairs
python trade_manager.py sim-eval --date 20260212 --aliases gf1 --strat usday --auto-all

# Date range
python trade_manager.py sim-eval --date-range 20260210:20260213 --strat usday --auto-all

# Overnight strat (sim date = live date + 1)
python trade_manager.py sim-eval --date 20260212 --date-offset 1

# Skip steps if data already local
python trade_manager.py sim-eval --date 20260212 --skip-fetch --skip-md --auto-all
```

**Pipeline steps:**
1. Fetch from remote: dated configs (`pk_*.json.YYYYMMDD`), INFO logs, orders, acct/trade CSVs
2. Interactive (strat, sym) selection (or `--auto-all`)
3. Download market data via `md_exists.py` (auto-confirms S3 downloads)
4. Run sims with `pktrade` using the dated configs
5. Compare sim vs live: PnL, fill rates, trade counts per symbol
6. Flag days with usermsg/inherit events detected in logs

| Flag | Description |
|---|---|
| `--date YYYYMMDD` | Live session date |
| `--date-range START:END` | Date range (YYYYMMDD:YYYYMMDD) |
| `--date-offset N` | Sim date = live date + N (default 0) |
| `--aliases gf0,gf1` | SSH aliases to fetch from |
| `--strat SUBSTR` | Filter strats by substring |
| `--group SUBSTR` | Filter groups by substring |
| `--skip-fetch` | Skip remote file fetch |
| `--skip-md` | Skip market data download |
| `--auto-all` | Skip interactive selection, use all pairs |
| `--sim-dir PATH` | Sim output dir (default `~/scratch/simeval`) |

### sim-drill — trade-level drill-down

Compares individual trades between sim and live. Operates on files already produced by `sim-eval`.

```bash
# All syms for a strat
python trade_manager.py sim-drill --date 20260212 --strat usday

# Single symbol
python trade_manager.py sim-drill --date 20260212 --strat usday --sym xyz:TSLA

# Time window
python trade_manager.py sim-drill --date 20260212 --strat usday --sym xyz:TSLA --time 09:45-10:00
```

Shows per-symbol: fill count, shares traded (buy/sell), net PnL, commission, VWAP, end position, ADD/REMOVE breakdown. Lists individual trades when under 30.

| Flag | Description |
|---|---|
| `--date YYYYMMDD` | Date (required) |
| `--strat SUBSTR` | Filter strats by substring |
| `--group SUBSTR` | Filter groups by substring |
| `--sym xyz:TSLA` | Single symbol to drill into |
| `--time HH:MM-HH:MM` | Time window filter |

---

---

## Part 3: PTA — Post Trade Analysis

Standalone script (`pta.py`) for finding unusually bad days per symbol and
explaining why they happened. Uses local `acct_*.csv` and `trades_*.csv` files
already fetched by `trade_manager.py fetch`.

All commands: `python pta.py <command> [flags]`

### flag — list outlier loss days

Per-`(alias, group, strat, sym)` trailing-window z-score on `net_pnl`. Flags
days where the symbol lost an unusually large amount versus its own recent
history.

```bash
python pta.py flag                                  # all local data
python pta.py flag --days 7                         # only flags from last week
python pta.py flag --z-threshold -3.0 --abs-floor -100
python pta.py flag --aliases gf2 --strat usday --sym xyz:META
```

| Flag | Description |
|---|---|
| `--days N` | Only show flags within last N calendar days |
| `--window N` | Trailing window length in trading days (default 30) |
| `--min-days N` | Min baseline days before z is computed (default 10) |
| `--z-threshold X` | Flag when z_net_pnl < threshold (default -2.0) |
| `--abs-floor X` | Also require net_pnl < this (default -25.0) |
| `--aliases gf0,gf1` | SSH aliases to scan |
| `--strat S` / `--sym X` | Filter |

### drill — forensic view for one (date, sym)

For a single flagged day, prints: acct summary, trade-level summary, 30-min
TOD buckets, top losing trades, markouts (if available), log events, and a
"Likely factors" diagnosis.

```bash
python pta.py drill --date 20260519 --sym xyz:TSM
python pta.py drill --date 20260519 --sym xyz:TSM --strat usday_alphacov_0503
python pta.py drill --date 20260519 --sym xyz:TSM --skip-markouts
```

Factors that can fire:
- `DIRECTIONAL` — avg position × price move ≈ most of the loss
- `WHIPSAW` — round-trip leak: sell_vwap < buy_vwap across many flips
- `CONCENTRATED` — top 3 trades did most of the damage
- `TIME-SPIKE` — one 30m bucket explains >40% of net pnl
- `ONE-SIDED FLOW` — in the worst bucket, >80% one-sided shares
- `VOLUME-SPIKE` — shs_traded >3σ above sym's own baseline
- `ADVERSE SELECTION` — avg ADD markout ≤ -0.5 bps (needs `bin/fillstats`)
- `BLED OUT` / `PARTIAL RECOVERY` — min_pnl_seen vs end-of-day
- `OPEN POSITION` — ended carrying directional risk

Each factor prints metric, likely cause, and what to check next.

### scan — flag, then drill the top N flagged rows

End-to-end: flag scan + per-row drill + factor-frequency rollup at the end.

```bash
python pta.py scan --days 7 --top 5
python pta.py scan --days 30 --z-threshold -3.0 --top 10 --skip-markouts
```

| Flag | Description |
|---|---|
| (all `flag` flags) | Same thresholds as `flag` |
| `--top N` | Drill into top N most-anomalous flagged rows (default 10) |
| `--skip-markouts` | Don't compute/load fillstats markouts (faster) |
| `--mo-offset N` | Markout offset in seconds (default 30) |

### patterns — cross-day patterns across flagged losses

Aggregates factors, recurring worst-buckets, and side-bias across **all**
flagged days for a sym or strat. Best for spotting recurring failure modes
(e.g., "this sym loses at the open every time").

```bash
# All most-affected (strat, sym) pairs:
python pta.py patterns --days 60

# Scoped to one strat:
python pta.py patterns --strat usday --days 60

# Single-sym deep dive:
python pta.py patterns --sym xyz:META --days 60
```

| Flag | Description |
|---|---|
| (all `flag` flags) | Filter / threshold the input set |
| `--min-pattern-days N` | Multi-pair view: min flagged days per pair (default 2) |
| `--top-pairs N` | Max pairs in the multi-pair view (default 20) |
| `--include-markouts` | Run fillstats per day (slow) for ADVERSE SELECTION |

Multi-pair view output (one block per `(alias, group, strat, sym)`):
- count of flagged days, total/avg loss
- top 3 factor labels with frequency
- most common worst-bucket TOD

Single-sym view adds: full factor frequency, side-going-in distribution, and
a per-day list with factor stacks.

### Markouts (ADVERSE SELECTION factor)

`drill`, `scan`, and `patterns --include-markouts` will lazily invoke
`bin/fillstats` to compute per-fill markouts. If the binary isn't built in
this checkout, the factor is skipped with a one-line message and the rest of
the diagnosis continues. To enable:

```bash
# Symlink from a sibling checkout where it's already built:
ln -s /home/pktrade/tradefi/retraded/bin/fillstats \
      /home/pktrade/tradefi/retraded_3/bin/fillstats
```

Cached `trades_YYYYMMDD_fillstats.csv` files are reused automatically.

---

## File structure

```
~/scratch/tradeperf/                    # live data (fetched from remotes)
  {alias}/{group}/{strat}/
    acct_YYYYMMDD.csv                   # daily account summary
    trades_YYYYMMDD.csv                 # trade log
    trades_YYYYMMDD_fillstats.csv       # markouts (lazy, written by bin/fillstats)
    orders_YYYYMMDD.csv                 # order log
    pk_*.json.YYYYMMDD                  # dated config snapshot
    pktrade.*.log.INFO.YYYYMMDD-*       # INFO log

~/scratch/simeval/                      # sim outputs
  {alias}/{group}/{strat}/
    acct_simeval_LIVEDATE_SIMDATE.csv   # sim account summary
    trd_simeval_LIVEDATE_SIMDATE.csv    # sim trade log
    out_SIMDATE.txt                     # sim stdout
    run_info.txt                        # command + config used
  comparison_latest.csv                 # full comparison data
```

## Config

Defaults in `CONFIG` dict at top of `trade_manager.py`:

| Setting | Default | Override |
|---|---|---|
| `ssh_aliases` | `gf0, gf1, gf2` | `--aliases gf0,gf1` |
| `remote_base` | `/home/ubuntu/estrader` | edit CONFIG |
| `local_base` | `~/scratch/tradeperf` | `--local-base /path` |

## Dependencies

- Python 3 (use `/home/pktrade/.venvs/v1/bin/python`)
- pandas, boto3
- SSH access to gf0/gf1/gf2
- `pktrade` binary at `retraded/bin/pktrade`
- `md_exists.py` at `strat_main/tools/md_exists.py`

## Part 5: HL Divergence Monitor

Standalone script (`hl_divergence_monitor.py`) that catches HL micro-structure
regime shifts *as they happen*, so we don't discover them post-hoc via PnL.

For every currently-traded HL symbol, computes per-day z-scores of these
dimensions against a trailing 30-weekday baseline:

- `notional_per_day` (vol_shs × avg_mid)
- `num_trades`
- `spread_bps`
- `med_inside_liq`
- `n_midchanges`
- `med_trdsz`

Weekends skipped (matches `--weekdays-only` convention). Per-day results
cached under `~/scratch/hl_divergence_cache/` so subsequent runs are
sub-second.

**Two modes:**

- **Per-day flag** — flag syms where any dim's |z| > 2 on a given date.
  Severity = # of dims fired (LOW / MEDIUM / HIGH).
- **Rolling severity** — check the last N weekdays and report syms
  flagged on ≥ K of them. This is the main mode, because tech mega-caps
  have naturally high day-to-day variance and single-day flags are noisy.
  The 06-25 tech-cluster event *does not* fire clean single-day flags,
  but *does* fire cleanly under rolling mode.

```bash
# Single date
python hl_divergence_monitor.py --date 20260625

# Rolling mode — flags syms that fired on 3+ of the last 12 weekdays
python hl_divergence_monitor.py --date-range 20260625:20260710 --rolling 12 --consec-threshold 3

# Filter to specific syms
python hl_divergence_monitor.py --date 20260625 --syms xyz:NVDA,xyz:MSFT
```

The rolling backfill of 06-25 → 07-10 flagged META (z=+12.9 on notional),
MSFT (n_midchanges z=-3.3), AAPL, AMZN, TSLA, AVGO, NVDA — the exact set
we identified manually in `pm_techmm_20260625break_20260715.md`. LLY,
which we identified as unaffected, is absent. This validates the tool.

**Follow-ups**:
- Add HL/topbook divergence dimensions (needs Databento per-day cache).
- Wire into cron alongside `pta scan --days 1`.
- First-time run for a sym is ~20-30s (baseline fill); after cache warm,
  daily incremental is essentially instant.

## Part 4: Fleet Review — inheritance attribution

Standalone script (`fleet_review.py`) for fleet-wide PnL attribution. Splits
each strat's `net_pnl` into two pieces: `inherit_mtm` (mark-to-market on the
SOD inherited position over the strat's own session) and `strat_own`
(residual — the earned edge). Then classifies each (alias, group, strat, sym)
pair as FAKE_WINNER / REAL_BLEEDER / OVERACHIEVER etc., and overlays a
lifetime-history verdict (ALWAYS_BAD / OLD_DECAY / RECENT_DECAY / STEADY_EARNER).

Motivation: raw PnL is misleading for strats that inherit sibling positions,
because a chunk of the day's PnL is market drift on inherited size, not the
strat's own edge. Cross-family strats end flat so their PnL is always genuine;
RelWideMM2 usday / boats / postusa / preus all inherit and can look
better or worse than their edge really is.

```bash
# End-to-end
python fleet_review.py --since 20260701 run

# Individual phases
python fleet_review.py --since 20260701 attribute   # per-row inheritance split
python fleet_review.py --since 20260701 classify    # per-pair category
python fleet_review.py --since 20260701 history     # lifetime verdict for actionable pairs

# Filter to a family / machine
python fleet_review.py --since 20260701 --strat usday run
python fleet_review.py --since 20260701 --aliases gf3  run
```

Output CSVs land in `--outdir` (default `~/scratch/tradeperf/`):

- `_attribution_YYYYMMDD.csv` — one row per (strat, sym, date)
- `_actionable_YYYYMMDD.csv`  — one row per (strat, sym) aggregate + category
- `_history_review_YYYYMMDD.csv` — actionable pairs with lifetime verdict

See the study `studies/fleet_review_20260715.md` for the first-run findings
and method write-up.

## See also

- `TODO.md` — outstanding work items for both parts
