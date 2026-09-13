# Strategy/Symbol Postmortems

Diagnoses of cases where a (strat, symbol) pair traded consistently well, then turned consistently bad.

## See also

- **`../symbol_regime_log.md`** — append-only registry of symbols whose
  market micro-structure has fundamentally changed. Check before
  redeploying a symbol; add an entry when authoring a `regime`-verdict
  postmortem.
- **`../fleet_review_playbook.md`** — the fleet-wide inheritance-attributed
  review that surfaces postmortem candidates.

## Layout

- **Notes + results (this dir)**: significant findings, verdicts, process notes.
- **Scratch / intermediate**: `~/scratch/postmortems/{case_id}/`
- **Scripts**: `~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/`

## Case ID

`{symbol}_{strat}_{YYYYMMDD}` where the date is the postmortem authoring date.
Symbols are lowercased; strat is the local subdir name.

## Files

- `_template.md` — case template
- `_workflow.md` — the playbook (steps + which `trade_manager.py` commands to run)
- `pm_*.md` — individual cases

## Verdict categories

- `data` — wrong/stale feed, symbolizer mismatch, contract month bug, etc.
- `signal_decay` — alpha is gone; sim shows the same degradation as live.
- `execution` — fill quality / markouts / latency degraded; sim still fine.
- `regime` — market structure shifted (vol, liquidity, correlation regime).
- `config_change` — sizing / params / risk_level / size_mult changed.
- `mixed` — multiple causes; specify ranking.

## Cases

| Case ID | Strat | Symbol | Window | Verdict | Notes |
|---|---|---|---|---|---|
| [mrvl_usday_20260618](pm_mrvl_usday_20260618.md) | gf3/combined_equities_bfx3/usday | MRVL | base 0511–0528 / bad 0603–0611 | mixed | HL volume 1,000×, spread 8× tighter; topbook +80% rally + 1.65× vol. Underlying news/regime event reshaped HL contract. Sim-eval pending. |
| [ewy_usday_cov_0313_20260618](pm_ewy_usday_cov_0313_20260618.md) | gf2/combined_equities_bfx2/usday_cov_0313 | EWY | base 0528–0610 / bad 0611–0616 | mixed | HL inside book 3.4× thinner; mid-changes 4×; topbook flat. No real-world event — looks like new HL participant. Confirmed on usday_cov_0330. |
| [techmm_20260625break_20260715](pm_techmm_20260625break_20260715.md) | gf1/…/usday + gf2/…/usday_cov_0521 | NVDA, INTC, MSFT | base 20260601–20260624 / bad 20260625–20260714 | regime | Three tech mega-caps peaked on the *exact same session* (2026-06-25) across two independent RelWideMM2 configs. HL diverged from topbook per-symbol: NVDA/INTC HL quieter than topbook, MSFT HL busier. HL-side regime event; not the cancel bug (fix landed later, didn't restore edge). All three disabled. |
