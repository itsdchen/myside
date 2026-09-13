# Postmortem Workflow

Steps to diagnose a (strat, symbol) that traded well, then turned bad.
All commands assume `cd ~/tradefi/retraded_4/overmind/strat_main/tools/trademan`.

## 0. Pick the case

- Symbol + strat (one of `{alias}/{group}/{strat}` from `~/scratch/tradeperf/`).
- For multi-symbol strats (`combined_equities_bfx*`), filter with `--symbol`.

## 1. Pull full history into a per-symbol PnL series

Run the helper:

```bash
python postmortems/pm_history.py \
    --strat-path gf3/combined_equities_bfx3/usday \
    --symbol xyz:MRVL \
    --out ~/scratch/postmortems/{case_id}/history.csv
```

It walks every `acct_*.csv` under the strat dir and emits a per-day row for the
symbol. Plot or eyeball the cumulative PnL curve to identify the inflection.

## 2. Define windows

- **Baseline:** stretch of consistently positive days.
- **Bad:** stretch of consistently negative or flat-with-cost days.
- Record the inflection date (where slope changes).

## 3. Account metrics: baseline vs bad

```bash
python postmortems/pm_history.py \
    --strat-path ... --symbol ... \
    --baseline YYYYMMDD:YYYYMMDD \
    --bad YYYYMMDD:YYYYMMDD \
    --summary
```

Prints the metric table for the postmortem.

## 4. Execution check

```bash
python trade_manager.py markout   --date-range BASELINE_RANGE --strat STRAT --group GROUP
python trade_manager.py markout   --date-range BAD_RANGE       --strat STRAT --group GROUP
python trade_manager.py tod       --date-range BASELINE_RANGE --strat STRAT --group GROUP
python trade_manager.py tod       --date-range BAD_RANGE       --strat STRAT --group GROUP
python trade_manager.py fillstats --date-range BAD_RANGE       --strat STRAT --group GROUP
python trade_manager.py order-analysis --date-range BAD_RANGE  --strat STRAT --group GROUP
```

Compare markouts and TOD profile. Look for: degraded +60/+120s markouts,
shifted TOD bucket, jump in rejects, change in RTT.

## 5. Sim vs live

```bash
python trade_manager.py sim-eval --date-range BASELINE_RANGE --strat STRAT --sym SYMBOL
python trade_manager.py sim-eval --date-range BAD_RANGE      --strat STRAT --sym SYMBOL
```

- Sim degrades in lockstep → signal / regime / data.
- Sim still profitable → execution or feed.

For trade-level drill, use `sim-drill --date X --sym SYMBOL`.

## 6. Config & events

```bash
python trade_manager.py report --config --days N --strat STRAT --symbol SYMBOL
python trade_manager.py log-events --date-range FULL_RANGE --strat STRAT
```

Note any change in `size_mult`, `risk_level`, sizing, params, or rejected/cancel-paused episodes.

## 7. Data red flags (from `~/CLAUDE.md`)

- `times_traded` collapses well below baseline → likely stale/wrong signal.
- All buys or all sells → persistent basis; check symbolizer / contract month.
- `fill_rate → 1.0` with tiny `shs_traded` → stuck at max-pos, exit-only.

## 8. Verdict

Pick one category. Cite 1–3 numbers. Explicitly rule out the others.

## 9. Write the case file

Copy `_template.md` to `pm_{case_id}.md`. Fill in. Add a row to `README.md`'s table.
