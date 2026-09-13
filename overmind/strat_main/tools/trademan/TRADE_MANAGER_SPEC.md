# Trade Manager Tool — Spec & Discovery Log

## Goal
Build a Python script to:
1. SCP trade files down from remote machines (using existing SSH aliases)
2. Parse and tag trades by symbol / strategy
3. Analyze performance over a series of days
4. Email summaries of how each strat/symbol pair is doing
5. Support management decisions (e.g., stop a strat, adjust sizing)

## Open Questions

### Trade file format — ANSWERED
- **Format**: CSV, no header row
- **Naming**: `trd_{N}_{YYYYMMDD}.csv` (e.g. `trd_0_20260128.csv`)
- **Key columns** (positional):
  - 0: time string (`20260128 00:03:44.190000001 EST`)
  - 1: time ms since epoch
  - 2: order number
  - 3: symbol name (e.g. `xyz:COPPER`)
  - 4: market (e.g. `Hyperliquid`)
  - 5: side (`Buy`/`Sell`)
  - 6: price
  - 7: size
  - 8: end position
  - 9: closed PnL
  - 10: commission
  - (middle columns less important)
  - -2: `ADD`/`REMOVE`
  - -1: front order flag (e.g. `PR:Front`)
- **Path pattern (live)**: `{ssh_alias}:{base_path}/{group}/{strat}/trades_{YYYYMMDD}.csv`
  - Example: `gf0:/home/ubuntu/estrader/combined_exotic_gf0/allday_v1/trades_20260212.csv`
  - `gf0` = SSH alias
  - `combined_exotic_gf0` = group
  - `allday_v1` = strat
  - Symbol is inside the trade file (column 3), not in the path

### Remote machines — ANSWERED
- **3 machines currently**, more later
- **Base path is the same on all**: `/home/ubuntu/estrader`
- **SSH aliases**: e.g. `gf0` (will need a config list of aliases)
- **File naming**: `trades_{YYYYMMDD}.csv` (one per day per strat)

### Symbol / strategy tagging — ANSWERED
- **Multiple groups/strats per machine** — script should auto-discover by listing directories
- Symbol is in the data files (column `sym` in account files, column 3 in trade files)
- Tagging hierarchy: **machine (alias) -> group -> strat -> symbol**

### Account files (primary data source for v1) — ANSWERED
- **Same directory as trade files**
- **Naming**: `acct_{YYYYMMDD}.csv`
- **Has a header row**: `date,sym,net_pnl,closed_pnl,commission,funding,min_pnl_seen,shs_traded,shs_sent,fill_rate,reachable_fill_rate,ioc_fill_rate,times_traded,times_flipped,open_pos`
- **Key columns**: date, sym, net_pnl, closed_pnl, commission, funding, min_pnl_seen, shs_traded, times_traded, times_flipped, open_pos
- One row per symbol per day

### Analysis — PARTIAL
- v1: focus on account file data (daily PnL per strat/symbol)
- Metrics TBD as we iterate, but account files give us: net_pnl, closed_pnl, commission, funding, fill rates, trade counts, open position

### Email — DEFERRED
- Skip for v1, just print reports
- Will add email later

### Management decisions — ANSWERED
- v1: print reports for manual decision-making
- Later: add automatic anomaly flagging (losing streaks, drawdowns, etc.)

### Local storage — ANSWERED
- Default: `~/scratch/tradeperf`
- Should be configurable (CLI arg or config file)

---

## Discoveries
- Account files have headers, trade files do not
- Account files are the primary data source for v1 (daily PnL per symbol)
- Trade files will be fetched but not parsed in v1
- SSH aliases: gf0, gf1, gf2
- Base path on all machines: `/home/ubuntu/estrader`
- Discovery via `ssh {alias} ls /home/ubuntu/estrader/` to find groups, then strats within each

## Design Decisions
- Single Python script for v1, can be split later
- Configurable local storage path (default `~/scratch/tradeperf`)
- Configurable list of SSH aliases
- Email deferred to later iteration
