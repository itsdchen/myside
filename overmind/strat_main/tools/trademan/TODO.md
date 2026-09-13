# Trademan — TODO

Three parts: **Trademan** (how things performed), **Sim vs Live** (are our sims
accurate), and **PTA** (post-trade analysis — find unusually bad days and
explain why).

---

## Part 1: Trademan — performance reporting & analysis

### Done
- [x] `fetch` — SCP acct/trade/orders/configs/logs from remote machines
- [x] `report` — PnL summary by strat, daily pivot table, per-symbol breakdown
- [x] `fetch-report` — fetch + report in one command
- [x] `email-report` — fetch + report + send via email
- [x] Alert rules: consecutive losing days, min positive days in window
- [x] Config change tracking: diff dated pk_*.json configs day-over-day
- [x] `log-events` — standalone INFO log parser with structured event types:
      usermsg (SET_THRESH_MULT, SET_THROTTLE_LEVEL), position inheritance,
      risk level transitions (TL0-TL5), cancel reject pauses, overnight pos,
      setSizeMult
- [x] Feed latency analysis — per-symbol ms_behind percentiles (p50/p75/p90/p99/max)
      from TryFire log lines
- [x] Order event anomalies — cancel rejects, OE ack not found, erase non-existent
      order, all broken down per symbol
- [x] `order-analysis` — RTT percentiles (NewOrd→NewOrdAck, Cancel→CancelAck) per
      symbol, cancel/order reject counts. Standalone command and embedded in report.

### TODO
- [x] **Fix log-events glob** (2026-07-15): pattern was `pktrade.*.log.INFO...`
      which missed multi-instance files named `pktrade_0.HOST...` /
      `pktrade_1.HOST...`. Now `pktrade*.log.INFO.{ds}-*`. Old runs of
      log-events silently returned "No events found" when multi-instance
      files were the only ones present.
- [x] **Fixed 2026-07-15**: `pm_daily_timeline.py --markouts` was filtering
      `add_remove == "ADD"` only, zeroing out markouts for cross strats
      (RelCross, IOC-based) whose fills are all REM. Now auto-detects
      dominant side per (sym, day): shows REM markouts for cross strats,
      ADD for MM strats, MIX when both sides present. Header includes
      `side` column.
- [x] Log events in report — per-strat-per-date table showing UMs, inherits,
      cancel rejects/pauses, OE anomalies, feed latency p50/p99/max. Respects
      --strat/--group filters. Garbage latency values (>10s) filtered out.
- [ ] Cancel reject analysis — aggregate cancel pauses over time: which syms
      get the most, time-of-day distribution, correlation with PnL.
- [x] Time-of-day performance — `tod` command buckets trades into 30-min
      intervals, shows fills, net_pnl, pnl_bps, and markout avgs per bucket.
      Standalone command and compact summary embedded in `report`.
- [ ] Risk level duration tracking — from log events, compute how long each
      sym spent in TL1/TL2/TL5 per day. Already have the transitions parsed.
- [ ] Usermsg impact analysis — compare performance before/after each usermsg
      (thresh mult change). Log has exact timestamps, trades file has timestamps.
- [x] Fill context analysis — `fillstats` command runs bin/fillstats (superset
      of markout) computing per-trade features (LMR, spread, volatility,
      notional) from local+remote books at fill time. Drill-down report
      segments trades by feature quartiles to surface adverse selection
      patterns. Standalone command and compact summary embedded in `report`.

### Needs user input
- [ ] Cancel fill rate from orders — % of shares on cancel attempts that got filled.
      Orders format is understood. Needs: match Cancel→Exec events by order_id to
      compute how often a cancel attempt results in a fill instead.
- [x] Markout analysis — `markout` command runs bin/markout on trades files,
      shows per-symbol and ADD/REM breakdown at configurable offsets. Compact
      summary embedded in `report` when _markout.csv files exist.

### Future
- [ ] Dashboard / plots — web UI or matplotlib for interactive exploration
- [ ] Automated daily email with log event summary + PnL report
- [ ] Position tracking over time — plot position size through the day per sym
- [x] **Coded 2026-07-17** (needs rebuild + deploy): Added depth-for-notional
      to `symstats.cc`. Every 2-minute snapshot walks each side of the book
      consuming $30k / $60k / $250k / $2M notional (USD, HL is USDC-native)
      and records bps-from-mid to reach the threshold. Reports median per
      side per threshold + coverage count. New CSV columns: `depth_bps_bid_30k`,
      `depth_bps_ask_30k`, `depth_bps_bid_60k`, `depth_bps_ask_60k`,
      `depth_bps_bid_250k`, `depth_bps_ask_250k`, `depth_bps_bid_2M`,
      `depth_bps_ask_2M`, plus `depth_n_covered_*` counts and
      `depth_n_snapshots`. `pm_hl_stats.py::run_symstats` opportunistically
      surfaces these columns when present. Once binary is rebuilt,
      `pm_hl_stats` and `hl_divergence_monitor` can add them as first-class dims.
- [x] **Coded 2026-07-17** (needs rebuild + deploy): Fixed `fill_rate`
      column precision in acct file. `src/pktrade/pktrader/pktrader.cc:199`
      changed the three fill_rate format specifiers from `{:.2f}` to `{:.6f}`.
      Post-rebuild, the `shs_traded/shs_sent` workaround in
      `pm_daily_timeline.py` becomes unnecessary (though still safe to keep
      for backward compat with older CSVs).

---

## Part 2: Sim vs Live — simulation evaluation

### Done
- [x] `sim-eval` — full pipeline: fetch → select → download md → sim → compare
- [x] CLI with --date, --date-range, --date-offset, --skip-fetch, --skip-md
- [x] Interactive (strat, sym) pair selector with range input, or --auto-all
- [x] Comparison report: per-symbol PnL, fill rates, trade counts, day-by-day
      and aggregate views
- [x] `sim-drill` — trade-level drill-down with time window filtering,
      per-symbol stats, trade-by-trade listing when small enough
- [x] md_exists auto-runs for all configs (auto-confirms S3 downloads)
- [x] run_info.txt in each sim dir (command + config + dates)
- [x] Log event flagging — days with usermsg or inherit are marked FLAGGED

### TODO — important
- [x] **Usermsg replay in sim** — schedule generated from parsed log events
      (`generate_usermsg_schedule`), C++ binary reads TSV via
      `--global-usermsg-path` and replays UMs at correct timestamps
      (`TradeCarrier::loadUsermsgSchedule`). Wired into sim-eval pipeline.
- [x] **Position inheritance replay** — schedule generated from parsed log
      events (`generate_inherit_schedule`), C++ binary reads CSV via
      `--global-inherit-path` and replays inherits at correct timestamps
      (`TradeCarrier::loadInheritSchedule`). Wired into sim-eval pipeline.
- [ ] **Time-bucketed sim vs live** — split the day into intervals using trade
      timestamps, show sim vs live stats per bucket. Most useful after markout
      comparison is available. Deferred until markouts are done.
- [ ] Fill rate investigation — acct columns (fill_rate, reachable_fill_rate,
      ioc_fill_rate) show 0.00 for both sim and live. Check if shs_traded /
      shs_sent is genuinely near-zero or if the calculation is different.

### Needs user input
- [ ] Cancel fill rate in comparison — needs orders_*.csv format.
- [ ] Markout comparison — compare markouts sim vs live. Trademan markout
      analysis is done; need to run markout on sim trade files and compare.

### Future
- [ ] Order-level matching — compare individual orders between sim and live
- [ ] Auto-flag root cause of divergence — use log events + trade comparison
      to auto-classify (e.g. "diverged after usermsg at 09:29")

---

---

## Part 3: PTA — Post Trade Analysis

Standalone `pta.py`. Flags unusually-bad days per `(strat, sym)` and produces
trade-level diagnostics. See `README.md` Part 3 for usage.

### Done
- [x] `flag` — per-(strat, sym) trailing-window z-score on net_pnl, with
      configurable window, min-days, z-threshold, and absolute floor.
- [x] `drill` — forensic view for one (date, sym): acct row, trade summary,
      30m TOD buckets, top losing trades, log events, markouts summary.
- [x] `scan` — flag + drill the top N flagged rows, with a factor-frequency
      rollup at the end.
- [x] `patterns` — cross-day aggregation: factor frequency, recurring
      worst-bucket TODs, side-going-in distribution. Two views: multi-pair
      roll-up (no filter or `--strat`) and single-sym deep dive (`--sym`).
- [x] Factor diagnosis with hypothesis + check hints: DIRECTIONAL, WHIPSAW,
      CONCENTRATED, TIME-SPIKE, ONE-SIDED FLOW, VOLUME-SPIKE,
      ADVERSE SELECTION (markouts), BLED OUT, PARTIAL RECOVERY, OPEN POSITION.
- [x] Per-trade pnl from cumulative `closed_pnl` via `.diff()`; reconciliation
      against acct's `closed_pnl` with explicit gap reporting when they differ
      (carryover, missing trades).
- [x] Lazy fillstats invocation for the ADVERSE SELECTION factor; degrades
      gracefully when `bin/fillstats` isn't present.

### TODO
- [ ] CSV / JSON export from `flag` and `patterns` for downstream tooling.
- [ ] Cross-sym pattern detection — "5 syms on the same strat got TIME-SPIKE
      on the same date" indicates a strat-level cause vs sym-level.
- [ ] Wire `order-analysis` results into drill (RTT per worst-bucket window)
      to corroborate ONE-SIDED FLOW pickoff diagnoses.
- [ ] `--terse` output mode for `scan` (skip per-day trade tables, just emit
      factor stacks).
- [ ] Sanity-check trade-file `commission` column — acct totals diverge wildly
      from trade-file sums on some strats; unit/convention mismatch.

---

## Reference

### Log event types
| Event type | Source | Pattern |
|---|---|---|
| `usermsg` | `trade_carrier.cc:365` | `Received UM targeting strat_id ...` |
| `usermsg_ignored` | `trade_carrier.cc:359` | `Ignoring UM not targeted at strat_id ...` |
| `usermsg_effect` | `rel_wide_mm_2.cc:2297` | `(SYM) Thresh mult set to N` |
| `inherit` | `trade_risk_man.cc:1227` | `(SYM) Inherited position (QTY). New position: QTY` |
| `inherit_calc` | `global_positioner.cc:304` | `(SYM) GPCalc: median global position ...` |
| `risk_level` | `trade_risk_man.cc:981/990/999/1020` | `(SYM) TLN: description (pos QTY)` |
| `cancel_pause` | `trade_risk_man.cc:1461` | `(SYM) Pausing trading for N seconds ...` |
| `cancel_reject` | `trade_risk_man.cc:588` | `(SYM) TradeRiskMan: CancelOrderReject ...` |
| `oe_ack_not_found` | `trade_risk_man.cc:478` | `(SYM) onOE NewOrderAck: order N not found ...` |
| `oe_erase_nonexistent` | `trade_risk_man.cc:540` | `(SYM) Attempting to erase non-existent order ID: N` |
| `feed_latency` | `rel_wide_mm_2.cc:768` | `(SYM) TryFire, ... (N ms_behind)` |
| `overnight_pos` | `trade_risk_man.cc:1188` | `Tried to load_overnight_pos with QTY for SYM ...` |
| `size_mult` | `trade_risk_man.cc:1141` | `(SYM) setSizeMult (N) maxpos X -> Y` |

### Known UM IDs
| um_id | Name | Args |
|---|---|---|
| 302 | SET_THRESH_MULT | `{"mult": N}` — multiplier on base place thresholds |
| 403 | SET_THROTTLE_LEVEL | `{"tl": N}` — override risk throttle level (0-5) |

### File layout
```
trademan/
├── trade_manager.py       # CLI entry point, fetch/report/email, shared helpers
├── order_analysis.py      # orders CSV parser (RTT, rejects)
├── markout_analysis.py    # markout binary runner, parser, reports
├── fillstats_analysis.py  # fillstats binary runner, parser, drill-down reports
├── sim_eval.py            # sim-eval pipeline orchestration
├── sim_eval_select.py     # interactive (strat, sym) picker
├── sim_eval_compare.py    # sim vs live comparison metrics & reporting
├── sim_eval_drill.py      # trade-level drill-down
├── sim_eval_logs.py       # INFO log parser (used by both parts)
├── pta.py                 # post-trade analysis (flag/drill/scan/patterns)
├── fleet_review.py        # inheritance-attributed PnL, fleet-wide classify+verdict
├── hl_divergence_monitor.py  # per-day z-score monitor for HL micro-structure shifts (rolling severity mode is the main one)
├── postmortems/
│   ├── pm_daily_timeline.py  # per-day timeline for a kill/postmortem decision
│   ├── pm_hl_stats.py     # HL micro-structure baseline vs bad (use --weekdays-only)
│   ├── pm_topbook_stats.py # topbook micro-structure via Databento
│   ├── pm_history.py      # full per-symbol PnL history
│   ├── pm_diagnose.py     # (older diagnostic)
│   └── pm_topbook_stats.py
├── README.md              # command reference
└── TODO.md                # this file
```
