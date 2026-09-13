# Postmortem: {symbol} on {strat}

- **Case ID:** `{symbol}_{strat}_{YYYYMMDD}`
- **Author date:** YYYY-MM-DD
- **Strat path:** `{alias}/{group}/{strat}`
- **Symbol:** `{symbol}`
- **Verdict:** {data | signal_decay | execution | regime | config_change | mixed}

## Summary

One-paragraph TL;DR: what was good, what went bad, what caused it, what to do.

## Windows

- **Baseline (good):** YYYYMMDD–YYYYMMDD — N days, avg PnL/day, total PnL, fills/day
- **Bad:** YYYYMMDD–YYYYMMDD — N days, avg PnL/day, total PnL, fills/day
- **Inflection:** YYYYMMDD (event/observation that anchored the split)

## Baseline vs Bad — account metrics

| metric | baseline | bad | delta |
|---|---|---|---|
| net_pnl/day | | | |
| closed_pnl/day | | | |
| commission/day | | | |
| funding/day | | | |
| fill_rate | | | |
| reachable_fill_rate | | | |
| times_traded/day | | | |
| times_flipped/day | | | |
| min_pnl_seen | | | |
| open_pos (eod) | | | |

## Execution check

Markouts / fillstats / TOD comparison across windows. Note any shift in TOD profile, fill quality at +1/+10/+30/+60/+120s, reject rate.

## Signal check (sim vs live)

Run sim-eval over both windows. If sim mirrors the degradation → signal/regime. If sim stays good → execution/feed.

## Config & events

- Config diffs in the window (`trade_manager.py report --config`).
- Log events: `usermsg`, `inherit`, `risk_level`, `cancel_pause`, `feed_latency`, `reject`, `size_mult`.

## Data red flags

Check against `~/CLAUDE.md` red-flag list:
- Sudden drop to very few trades/day
- One-direction trading
- fill_rate→1.0 with tiny notional

## Verdict & evidence

State the verdict and the 1–3 pieces of evidence that pin it. Rule out the other categories explicitly.

## Actions

- Immediate: (turn off / size down / leave on)
- Followups: (data fix, signal rework, exec retune, etc.)

## Commands used

```
# Paste the exact commands run so this case is rerunnable
```

## Artifacts

- `~/scratch/postmortems/{case_id}/` — pulled data, intermediate CSVs, plots
