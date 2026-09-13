# US Equities Cancel RTT Degradation — June 2026

## Summary

US equities passive cancel RTT degraded starting with the `2026-06-22` US day session.
The strongest current read is that this was a **cancel-specific Hyperliquid / `xyz`
unit-perp path change**, not local gateway rate-limit sleep and not a general order
ack/feed-latency issue.

The key evidence is the clean-cancel test:

- universe: `gf1`, `gf2`, `gf3`
- symbols: `xyz:` US equities
- session: US day, `09:30-16:00` ET
- order type: ADD/passive orders only
- clean cancel definition:
  - order had already received `NewOrdAck`
  - no `Exec` arrived between `Cancel` and `CancelAck`
  - RTT paired as `CancelAck - most recent prior Cancel`

This removes the main contamination from cancels that raced order placement or fills.

## Main Result

Clean cancel RTT shifted up across the whole distribution after `2026-06-18`.

| date | samples | p10 | p25 | p50 | p75 | p90 | p95 | p99 | p99.9 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `20260618` | 602,893 | 418 | 463 | 529 | 620 | 749 | 869 | 1169 | 1513 |
| `20260622` | 586,705 | 721 | 757 | 806 | 867 | 936 | 989 | 1138 | 1467 |
| `20260623` | 572,228 | 722 | 757 | 805 | 863 | 924 | 968 | 1084 | 1352 |
| `20260624` | 562,677 | 740 | 778 | 830 | 894 | 969 | 1027 | 1181 | 1556 |
| `20260625` | 508,404 | 742 | 785 | 849 | 934 | 1050 | 1149 | 1436 | 1910 |
| `20260626` | 448,251 | 733 | 771 | 826 | 893 | 970 | 1029 | 1187 | 1545 |
| `20260629` | 376,765 | 722 | 759 | 814 | 880 | 952 | 1005 | 1144 | 1445 |

Read:

- This was not mainly a tail event. The p10/p25/p50 all moved up by roughly `+280ms`.
- The p99 did not blow out on `20260622`; the floor/body of the cancel RTT distribution moved.
- The effect persisted through `20260629`.

## By Gateway

The p50 move was broad across hosts.

| date | gf1 p50 | gf2 p50 | gf3 p50 |
|---|---:|---:|---:|
| `20260618` | 496 | 534 | 544 |
| `20260622` | 792 | 808 | 813 |
| `20260623` | 796 | 807 | 807 |
| `20260624` | 829 | 837 | 821 |
| `20260625` | 816 | 860 | 853 |
| `20260626` | 819 | 835 | 817 |
| `20260629` | 796 | 821 | 812 |

Read:

- This does not look like one bad host or one bad strategy process.
- The old unchanged usday strategies moved with the newer `0616` variants.

## Time Of Day

The degradation was not just the open. It appears in every half-hour bucket.

Baseline `20260618` vs bad-window aggregate `20260622-20260629`:

| ET bucket | base p50 | base p90 | bad p50 | bad p90 | p50 delta |
|---|---:|---:|---:|---:|---:|
| `09:30` | 591 | 822 | 879 | 1055 | +288 |
| `10:00` | 596 | 844 | 873 | 1042 | +277 |
| `10:30` | 561 | 802 | 849 | 992 | +288 |
| `11:00` | 563 | 787 | 827 | 959 | +264 |
| `11:30` | 573 | 787 | 811 | 931 | +238 |
| `12:00` | 527 | 741 | 804 | 925 | +277 |
| `12:30` | 498 | 667 | 796 | 920 | +298 |
| `13:00` | 489 | 647 | 797 | 924 | +308 |
| `13:30` | 473 | 626 | 795 | 921 | +322 |
| `14:00` | 480 | 627 | 794 | 919 | +314 |
| `14:30` | 468 | 614 | 785 | 896 | +317 |
| `15:00` | 469 | 617 | 791 | 905 | +322 |
| `15:30` | 493 | 666 | 787 | 893 | +294 |

Read:

- The entire day shifted upward.
- This argues against a simple opening-auction/open-vol-only explanation.

## Control: New Order Acks

ADD `NewOrd -> NewOrdAck` did not degrade the same way.

| date | samples | p10 | p25 | p50 | p75 | p90 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `20260618` | 700,804 | 416 | 463 | 530 | 621 | 753 | 883 | 1215 |
| `20260622` | 650,039 | 392 | 428 | 481 | 546 | 620 | 678 | 850 |
| `20260623` | 629,427 | 394 | 431 | 482 | 544 | 610 | 659 | 793 |
| `20260624` | 627,990 | 412 | 451 | 506 | 575 | 655 | 720 | 906 |
| `20260625` | 584,335 | 415 | 461 | 529 | 622 | 749 | 859 | 1179 |
| `20260626` | 493,870 | 404 | 444 | 501 | 571 | 653 | 717 | 886 |
| `20260629` | 418,977 | 393 | 432 | 490 | 561 | 637 | 694 | 842 |

Read:

- This is not a general gateway/network/order-ack slowdown.
- The degradation is cancel-specific.

## Batch / Ack-Cluster Check

Hypothesis tested: larger cancel batches, or batches waiting for the slowest cancel in the
response, caused the observed RTT shift.

The strategy order logs do not expose gateway batch ids directly, so the proxy was to group
nearby `CancelAck`s in the same orders file into response clusters:

- exact same millisecond: not useful; all clean `CancelAck`s were unique at ms precision
- consecutive `CancelAck`s within `10ms`, `25ms`, or `50ms`

Larger inferred clusters do have higher RTT, but this does **not** explain the June shift.
Under the `25ms` cluster proxy:

| date | samples | avg cluster | p50 cluster | p90 cluster | single% | >=5% |
|---|---:|---:|---:|---:|---:|---:|
| `20260618` | 602,893 | 1.89 | 1 | 4 | 54.1 | 5.3 |
| `20260622` | 586,705 | 1.74 | 1 | 3 | 59.3 | 3.7 |
| `20260623` | 572,228 | 1.80 | 1 | 3 | 56.8 | 4.2 |
| `20260624` | 562,677 | 1.79 | 1 | 3 | 57.0 | 4.0 |
| `20260625` | 508,404 | 1.75 | 1 | 3 | 57.7 | 3.6 |
| `20260626` | 448,251 | 1.65 | 1 | 3 | 61.5 | 2.6 |
| `20260629` | 376,765 | 1.63 | 1 | 3 | 63.3 | 2.7 |

Read:

- Inferred clusters were, if anything, **smaller** after `20260618`.
- More acks were singletons after the degradation began.

The RTT shift remains when conditioning on inferred cluster size:

| cluster bucket (`25ms` proxy) | `20260618` p50 | bad-window p50 | delta |
|---|---:|---:|---:|
| `1` | 499 | 799 | +300 |
| `2` | 543 | 836 | +293 |
| `3-4` | 585 | 867 | +282 |
| `5-8` | 649 | 909 | +260 |
| `9-16` | 749 | 970 | +221 |

The same conclusion holds using a looser `50ms` proxy:

| cluster bucket (`50ms` proxy) | `20260618` p50 | bad-window p50 | delta |
|---|---:|---:|---:|
| `1` | 489 | 793 | +304 |
| `2` | 530 | 827 | +297 |
| `3-4` | 568 | 854 | +286 |
| `5-8` | 625 | 893 | +268 |
| `9-16` | 717 | 946 | +229 |
| `17+` | 872 | 1068 | +196 |

Read:

- Batch/response clustering is a secondary RTT gradient, but not the cause of the
  regime change.
- The degradation is visible even for inferred singleton cancels.

## Double-Ack Check

Hypothesis tested: duplicate `CancelAck`s to the strategy/order log inflated cancel RTT or
created false samples.

Universe:

- `gf1`, `gf2`, `gf3`
- `xyz:` symbols
- ADD/passive orders
- US day cancels
- same date set as the main clean-cancel study

Result:

| date | cancel order ids | cancel events | cancel ack events | order ids with multiple acks | extra ack events |
|---|---:|---:|---:|---:|---:|
| `20260618` | 697,195 | 697,585 | 697,195 | 0 | 0 |
| `20260622` | 643,877 | 643,954 | 643,877 | 0 | 0 |
| `20260623` | 622,350 | 622,401 | 622,350 | 0 | 0 |
| `20260624` | 623,056 | 623,127 | 623,056 | 0 | 0 |
| `20260625` | 580,358 | 580,402 | 580,358 | 0 | 0 |
| `20260626` | 490,885 | 490,930 | 490,885 | 0 | 0 |
| `20260629` | 415,837 | 415,900 | 415,837 | 0 | 0 |

There were a few repeated `Cancel` requests for the same order id, but no repeated
strategy-visible `CancelAck`s for a single order id. The clean-cancel RTT table is identical
whether measured using all clean acks or only the first clean ack per order id.

Read:

- Double-acking is not present in the strategy-visible order files for this universe.
- It cannot explain the clean cancel RTT shift.
- Gateway logs do contain many `"Got cancel ack from HL ... but already acked query"` lines,
  but those are suppressed duplicate HL responses after the gateway already acked the query;
  they did not become duplicate `CancelAck` rows in the order files.

## Gateway Rate-Limit Check

For gf1, gateway logs do not support the local `wsgateway` 1100-weighted-limit sleep
theory.

Checked relevant `pygateway` logs with the correct session mapping:

- session `20260618` -> `pygateway.20260617.log`
- session `20260622` -> `pygateway.20260621.log`
- session `20260623` -> `pygateway.20260622.log`
- session `20260624` -> `pygateway.20260623.log`
- session `20260625` -> `pygateway.20260624.log`
- session `20260626` -> `pygateway.20260625.log`
- session `20260629` -> `pygateway.20260628.log`

For all checked gf1 sessions:

- `Waiting to send cancels`: `0`
- `Waiting to send orders`: `0`
- `Priority IOC cap hit`: `0`
- `Priority IOC budget exhausted`: `0`
- `429 Too Many Requests`: `0`
- nonce-space skips: `0`

Sampled gf1 weighted usage also stayed far below the `1100` warning threshold:

| session | max sampled order weighted | max sampled cancel weighted | max sampled priority |
|---|---:|---:|---:|
| `20260618` | 513 | 458 | 32 |
| `20260622` | 485 | 463 | 43 |
| `20260623` | 465 | 411 | 31 |
| `20260624` | 362 | 347 | 23 |
| `20260625` | 402 | 358 | 30 |
| `20260626` | 309 | 347 | 21 |
| `20260629` | 433 | 399 | 29 |

Read:

- The earlier `gw-cross` mechanism remains risky in code, but gf1 does not show the
  direct local-throttle signature.
- If `gw-cross` contributed, it was not via gf1 hitting the local `0.3s` cancel sleep path.

The same gateway-pressure check on `gf2` and `gf3` also found no local throttling:

| alias | sessions checked | max sampled order weighted | max sampled cancel weighted | pressure markers |
|---|---:|---:|---:|---|
| `gf2` | 7 | 744 | 734 | none |
| `gf3` | 7 | 642 | 629 | none |

Pressure markers checked:

- `Waiting to send cancels`
- `Waiting to send orders`
- `Priority IOC cap hit`
- `Priority IOC budget exhausted`
- `429 Too Many Requests`
- nonce-space skips

All were zero on `gf2`/`gf3` for the checked sessions.

## Double-Lap / Orphan PR Check

PR #937 (`gw-orphans`) merged on `2026-06-17 16:32 -0400`, after `gw-cross` on the
same day. The important wsgateway changes were:

1. `6eeb21c9` — treat HL's `"Order was never placed, already canceled, or filled"`
   cancel error as terminal only if the order was safely terminal:
   - had a resting ack older than the double-lap buffer, or
   - placement was older than the expired-order threshold
2. `21691179` — add an orphan backstop every 5 minutes:
   - query open orders on both `xyz` and default perps
   - if an order was marked dead >60s ago but HL still shows it resting, force-cancel
     it and alert
3. `816d0b1e` — increase double-lap buffer from `1s` to `3s`, and after an early
   terminal cxlack, enqueue one forced insurance cancel

Potential effects:

- The `1s -> 3s` buffer can delay cxlack for cancel rejects that arrive 1-3s after
  the order ack, because those are no longer treated as terminal immediately.
- The insurance cancel adds one extra cancel action per early-terminal cxlack.
- The orphan backstop can add force-cancels, but only if HL still shows orders resting
  >60s after we marked them dead.

Observed gateway log counts do **not** make this look like the primary cause of the
median cancel RTT regime shift.

All-alias tagged counts:

| session | cancel errors | early cxlack | recent-ack double-lap warnings | late cancel ack after query ack |
|---|---:|---:|---:|---:|
| `20260618` | 4,475 | 373 | 473 | 23,939 |
| `20260622` | 5,279 | 514 | 598 | 19,289 |
| `20260623` | 4,837 | 481 | 582 | 16,761 |
| `20260624` | 4,688 | 607 | 591 | 6,912 |
| `20260625` | 6,670 | 766 | 1,211 | 12,825 |
| `20260626` | 4,782 | 619 | 718 | 8,455 |
| `20260629` | 4,536 | 516 | 631 | 2,224 |

Backstop / retry exhaustion signals:

- `Orphaned orders`: zero on checked `gf1/gf2/gf3` sessions
- `force-cancelling`: zero
- `Sending cxlack anyway`: zero
- `Cancelback` from too many cancel attempts: near zero

### The `1s -> 3s` Widening Specifically

The widening only affects one narrow branch:

```text
cancel response from HL is an error
  and error contains "never placed"
  and order has already received a resting ack
  and now - NewOrdAck time is between 1s and 3s
```

Old behavior with the `1s` threshold:

- If the order had a resting ack older than `1s`, treat the cancel error as terminal.
- Send `CancelAck` to the strategy immediately.
- Mark the order dead.
- Do not retry the cancel.

New behavior with the `3s` threshold:

- If the order had a resting ack between `1s` and `3s` ago, treat it as possible
  double-lap.
- Log:

```text
Got a cancel reject for recently acked pkcoid ...; possible double-lap, not treating as terminal
```

- Requeue the cancel instead of sending `CancelAck` immediately.
- Only after the ack is older than `3s` will the same terminal error produce an early
  `CancelAck`.

Matched gateway warning lines back to local `NewOrdAck` timestamps:

| scope | warnings | matched | `<=1s` | `1-3s` | `>3s` |
|---|---:|---:|---:|---:|---:|
| all collected | 5,446 | 5,205 | 2,786 | 2,378 | 0 |
| study sessions only | 5,446 | 4,976 | 2,648 | 2,294 | 0 |

By session date:

| date | matched warnings | `<=1s` | `1-3s` |
|---|---:|---:|---:|
| `20260618` | 473 | 351 | 122 |
| `20260622` | 612 | 342 | 270 |
| `20260623` | 597 | 352 | 245 |
| `20260624` | 653 | 323 | 330 |
| `20260625` | 1,292 | 651 | 641 |
| `20260626` | 718 | 333 | 385 |
| `20260629` | 631 | 330 | 301 |

Read:

- The widening absolutely changed behavior for the `1-3s` cases.
- Roughly `46%` of current recently-acked cancel-reject warnings are in that widened band.
- Those cases would likely have received immediate early cxlack under the old `1s` rule.
- But the absolute count is still only hundreds per day, while clean-cancel RTT samples are
  hundreds of thousands per day.
- Therefore the widening can explain some extra delayed cxlacks / warning logs, but it
  cannot explain the median clean-cancel RTT moving by `~300ms`.

Read:

- Double-lap-related logs did rise in some sessions, especially `20260625`, but these
  counts are tiny relative to the clean-cancel sample set (`~0.4M-0.6M` per day).
- The clean-cancel RTT shift is visible for hundreds of thousands of successful-looking
  post-ack/no-fill cancels, not just the hundreds of terminal-error/early-cxlack cases.
- No orphan backstop force-cancel activity was observed, so the periodic orphan mechanism
  was not creating meaningful extra cancel load.
- Insurance cancels are at most one per early-terminal cxlack; counts are too small to
  move median RTT or create gateway pressure.

Current interpretation:

- The double-lap PR is a plausible **symptom amplifier** and may increase some cancel-error
  / late-response logging.
- It is unlikely to be the primary reason clean cancel p50 moved from roughly `530ms` to
  `800ms+`.
- If HL-side cancel latency increased, double-lap races naturally become more common, so
  the PR's logs can rise because of the same underlying HL cancel-path degradation.

## Crosser RTT

For gf1 `eq_gf1_cross`, IOC RTT was measured as `NewOrd -> earliest terminal event`
(`Exec`, `Elimination`, or `NewOrderReject`).

| date | REM orders | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|
| `20260618` | 12,251 | 813 | 884 | 997 | 1493 |
| `20260622` | 12,674 | 797 | 863 | 943 | 1207 |
| `20260623` | 9,521 | 799 | 858 | 925 | 1155 |
| `20260624` | 10,553 | 828 | 902 | 1001 | 1328 |
| `20260625` | 8,775 | 827 | 905 | 1005 | 1461 |
| `20260626` | 7,276 | 832 | 902 | 994 | 1306 |
| `20260629` | 6,652 | 801 | 861 | 922 | 1113 |

Read:

- The crosser was already around `800ms` p50 before the bad window.
- Crosser RTT did not jump on `20260622`.
- Passive clean cancel RTT moved upward into the same ballpark as IOC terminal RTT.

## PnL Impact and Per-Machine Asymmetry

The cancel RTT regression translated to a clean PnL flip on two of three machines, while
`gf1` actually improved. The asymmetry is well-explained by symbol-basket
cancel-sensitivity, not by per-host infra differences (the cancel regression itself is
uniform across hosts).

Scope: US-equity strats only (`combined_equities_bfx{1,2,3}` + `eq_gf{1,2}_cross`).
PRIOR = `20260615-20260618` (4 trading days, Juneteenth on `20260619`).
THIS = `20260622-20260626` (5 trading days). Per-day averages used since day-counts differ.

| alias | PRIOR $/day | THIS $/day | delta $/day | fill rate PRIOR | fill rate THIS |
|---|---:|---:|---:|---:|---:|
| `gf1` | +113 | +784 | **+671** | 0.39% | 0.84% |
| `gf2` | +1,222 | -1,640 | **-2,862** | 0.78% | 1.16% |
| `gf3` | +1,617 | -3,037 | **-4,654** | 0.59% | 1.96% |

Read:

- `gf3` fill rate roughly **tripled** while PnL flipped negative — consistent with passive
  orders staying cancellable longer and getting picked off.
- Reported cxl_p50 delta is essentially uniform across the three hosts (~+322 ms each), so
  it cannot account for the asymmetric PnL outcome on its own.

### Symbol Basket Differentiation

`usday_cov_0521` runs on `gf1` and `gf2` only; the basket size and identity differ
sharply by host. Per-sym net PnL on that strat:

| alias | sym | PRIOR | THIS | delta |
|---|---|---:|---:|---:|
| `gf1` | `xyz:GOOGL` | 94 | -374 | -468 |
| `gf1` | `xyz:AMZN` | 490 | 149 | -341 |
| `gf1` | `xyz:PLTR` | 4 | -105 | -108 |
| `gf1` | `xyz:NVDA` | -215 | 426 | +641 |
| `gf2` | `xyz:EWY` | 49 | -2,125 | **-2,174** |
| `gf2` | `xyz:MSTR` | 377 | -866 | -1,243 |
| `gf2` | `xyz:LLY` | 364 | -772 | -1,136 |
| `gf2` | `xyz:USAR` | -843 | -1,655 | -812 |
| `gf2` | `xyz:INTC` | 342 | -325 | -667 |
| `gf2` | `xyz:MU` | 90 | -506 | -596 |
| `gf2` | `xyz:AAPL` | 505 | -58 | -564 |
| `gf2` | `xyz:MSFT` | 163 | -135 | -298 |

Spot-check on `20260626` acct files: none of the big-loss US single names
(`USAR`/`MSTR`/`EWY`/`MSFT`/`ORCL`/`LLY`/`INTC`/`NFLX`) appear in `gf1`'s acct files.
`gf3` does not run `usday_cov_0521` but bled across `usday`, `usday_cov_0516`,
`usday_cov_0616`, and `postusa` simultaneously.

Read:

- `gf1`'s slice of `usday_cov_0521` is 4 mega-caps; small absolute losses and `NVDA`
  offsets the rest.
- `gf2`'s slice is 22 syms including high-beta single names and ETF baskets; every
  meaningful position flipped negative.
- The per-machine PnL outcomes are explained by basket cancel-sensitivity, not by any
  per-host infra factor.

## Adjacent Factors Checked

Three further controls eliminated as primary drivers, consistent with the main study
finding.

### Trader Load Growth

Active `(strat, sym)` trader instance count per (alias, day), US equity strats only:

| alias | PRIOR avg | THIS avg | growth |
|---|---:|---:|---:|
| `gf1` | 28 | 32 | +13% |
| `gf2` | 71 | 87 | +22% |
| `gf3` | 62 | 70 | +14% |

Read:

- Load grew on every host but cxl_p50 delta is identical (+322 / +327 / +322 ms).
- Baseline cxl_p50 levels are nearly identical in both weeks (~485ms PRIOR, ~810ms THIS)
  despite `gf2` carrying 2.7× more traders than `gf1`.
- Adding traders is not the bottleneck on cancels.

### Per-Symbol Vol Expansion

Intraday range (max-min mid, bps) averaged across each week, from `trades_*.csv` `mid`
column:

| sym | PRIOR bps | THIS bps | ratio | host basket |
|---|---:|---:|---:|---|
| `xyz:MU` | 240 | 539 | 2.24x | `gf2` loser |
| `xyz:AAPL` | 108 | 211 | 1.95x | `gf2` loser |
| `xyz:MSFT` | 163 | 269 | 1.65x | `gf2` loser |
| `xyz:NFLX` | 190 | 305 | 1.60x | `gf2` loser |
| `xyz:LLY` | 171 | 252 | 1.47x | `gf2` loser |
| `xyz:USAR` | 526 | 710 | 1.35x | `gf2` loser |
| `xyz:GOOGL` | 194 | 261 | 1.35x | `gf1` winner |
| `xyz:NVDA` | 154 | 189 | 1.22x | `gf1` winner |
| `xyz:EWY` | 324 | 318 | **0.98x** | `gf2` largest loss |
| `xyz:INTC` | 517 | 512 | **0.99x** | `gf2` loser |
| `xyz:TSM` | 264 | 256 | 0.97x | `gf2` loser |

Read:

- Vol expanded on most losing names, but the two biggest single-name losses on `gf2`
  (`EWY` -$2,125, `INTC` -$667) had completely flat vol.
- `gf1`'s basket saw comparable 1.13-1.35x vol expansion yet profited.
- Vol expansion is an amplifier of the cancel-RTT issue, not a primary cause.

### Feed Staleness

`ms_behind` from `rel_wide_mm_2.cc:737` TryFire lines, pooled per (alias, week):

| alias | week | n | p50 | p75 | p90 | mean |
|---|---|---:|---:|---:|---:|---:|
| `gf1` | PRIOR | 44k | 70 | 72 | 2092 | 648 |
| `gf1` | THIS | 99k | 71 | 654 | 3526 | 947 |
| `gf2` | PRIOR | 77k | 71 | 657 | 4380 | 1081 |
| `gf2` | THIS | 184k | 71 | 1605 | 4848 | 1318 |
| `gf3` | PRIOR | 61k | 72 | 238 | 4262 | 1020 |
| `gf3` | THIS | 126k | 72 | 1007 | 4489 | 1165 |

Read:

- p50 `ms_behind` unchanged on every host; steady-state feed timing is fine.
- p75/p90 widened on all three, but mean delta was **largest** on `gf1` (+299 ms) and
  **smallest** on `gf3` (+145 ms) — the opposite of the PnL ranking.
- Feed staleness is not the differentiator between machines.

## Distribution Shape Cross-Check

Pooled `Cancel -> CancelAck` RTT for US equity strats, all symbols, grouped by code
version (`a63cad67` = OLD, deployed `20260609 17:28`; `15f1cfff` = NEW, deployed
`20260617 16:35`). OLD-code days = `20260615-20260617`. NEW-code days = `20260618`
onward.

| alias | wk | n | p1 | p5 | p10 | p25 | p50 | p75 | p90 | p95 | p99 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `gf1` | OLD | 209k | 345 | 370 | 386 | 420 | 474 | 550 | 667 | 795 | 1121 |
| `gf1` | NEW | 763k | 389 | 453 | 520 | 715 | 779 | 850 | 928 | 991 | 1166 |
| `gf2` | OLD | 466k | 349 | 375 | 393 | 430 | 485 | 560 | 664 | 769 | 1058 |
| `gf2` | NEW | 1.94M | 403 | 484 | 573 | 735 | 804 | 880 | 967 | 1040 | 1260 |
| `gf3` | OLD | 469k | 353 | 379 | 395 | 430 | 481 | 550 | 644 | 734 | 994 |
| `gf3` | NEW | 1.48M | 413 | 492 | 581 | 736 | 801 | 871 | 952 | 1021 | 1235 |

Per-percentile NEW - OLD delta (ms):

| alias | Δp1 | Δp5 | Δp10 | Δp25 | Δp50 | Δp75 | Δp90 | Δp95 | Δp99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `gf1` | +44 | +83 | +134 | +295 | +305 | +300 | +261 | +196 | +45 |
| `gf2` | +54 | +109 | +180 | +305 | +319 | +320 | +303 | +271 | +202 |
| `gf3` | +60 | +113 | +186 | +306 | +320 | +321 | +308 | +287 | +241 |

Read:

- Sharp step up between p10 and p25 on all three hosts; lower percentiles are
  comparatively less affected. ~15-20% of cancels remain near the OLD distribution.
- The remaining ~80% jump roughly uniformly by ~300 ms (p25 through p75 plateau).
- The plateau height is **identical** across three independent hosts with different
  symbol baskets and load levels — fingerprint of a fleet-wide constant-cost addition
  rather than a multiplicative or load-driven slowdown.
- p99 deltas shrink because the 60 s cap in `order_analysis.py` clips the worst tail in
  both periods.

## Current Interpretation

Most likely:

1. Hyperliquid's `xyz` cancel response path changed or degraded over the weekend before
   `2026-06-22`.
2. Clean passive cancels began taking roughly the same round-trip time as IOC terminal
   responses.
3. Because passive orders remained cancellable for roughly `+280ms`, fill-after-cancel
   rates and cancel-pending fill quantity rose sharply.

Less likely as primary cause:

- local gateway rate-limit sleep
- `gw-cross` priority cap exhaustion
- larger cancel batches / cancel-ack clusters
- double-acking in strategy-visible order logs
- double-lap/orphan PR mechanics
- feed data latency
- general order-placement latency
- a single new strategy or single host

Still plausible as contributors:

- increased live strategy count and churn made the slower cancel path more costly
- cross/live changes increased adverse selection or fill exposure
- `gw-orphans` / insurance-cancel logic amplified cancel-error log volume after cancels
  started racing terminal states more often

## Follow-Ups

1. Repeat the gateway-log pressure check for `gf2` and `gf3`.
   - If they also show zero local pressure, the HL-side explanation gets much stronger.
2. Check whether non-`xyz` products on the same gateways had the same cancel shift.
   - Local gf1 data in this sample was effectively all `xyz`, so it was not a control.
3. Pull/compare HL or exchange-side changes around the weekend before `2026-06-22`.
   - Unit asset count in gf1 gateway startup logs changed from `92` to `93/94` around this
     period, which is not proof but is directionally consistent with a unit-market change.
4. Add a daily clean-cancel RTT monitor:
   - p10/p25/p50/p75/p90/p95/p99 by alias and by 30-min bucket
   - separate ADD NewOrdAck control panel
   - alert when clean cancel p25/p50 shifts without corresponding NewOrdAck shift

## gv0 Live Probe, 2026-06-30

Freestanding script:

```text
/home/pktrade/tradefi/retraded_5/overmind/strat_main/tools/trademan/hl_cancel_path_probe.py
staged on gv0 as /tmp/hl_cancel_path_probe.py
```

Guardrails:

- ran on `gv0` with `~/.creds/.Hyperliquid.creds.json`
- creds had `subaccount_address`; live mode refused main-account creds by default
- existing `wsgateway.py --id 9` was killed before testing
- probe used gateway-style nonce lane `8/10`
- one live order at a time, `xyz:AAPL`, buy ALO, size `0.05`, price about `10%`
  below mid, notional about `$12.7`, max notional cap `$20`
- `openOrders` was checked before/after; final state had zero open orders

Read-only baseline from `gv0`:

```text
SDK: hyperliquid-python-sdk 0.23.0
gateway hl_signing: /home/ubuntu/estrader/gateway
xyz:AAPL asset: 110009
openOrders dex=xyz, n=50: p50 17.9ms, p90 59.3ms, p99 127.4ms, count 0
```

Clean cancel matrix, after resting ack, `10` samples per row:

```text
place_path  cancel_path  key    place_p50 place_p90 cancel_p50 cancel_p90 cancel_p99 update_p50 update_p90
manual-ws   manual-ws    cloid      416.2     456.6      711.5      813.6      850.5      758.9      847.9
manual-ws   manual-ws    oid        353.5     409.3      672.0      740.7      751.5      736.5      818.9
manual-ws   manual-http  cloid      382.4     472.7      735.7      837.7      955.9      783.5     1025.7
manual-ws   manual-http  oid        419.3     510.0      747.9      792.8      983.7      773.5     1215.7
manual-ws   sdk-http     cloid      401.2     459.5      706.6      749.1      773.0      771.2      951.1
manual-ws   sdk-http     oid        402.6     535.2      713.5      868.0     1257.1      781.5      949.8
```

Place-path isolation, `10` samples per row:

```text
place_path   cancel_path  key    place_p50 place_p90 cancel_p50 cancel_p90 cancel_p99 update_p50 update_p90
manual-http  manual-http  cloid      404.5     444.4      745.4      795.4      829.8      781.1      911.8
manual-http  manual-http  oid        388.8     446.0      703.5      727.0      784.6      736.0     1071.4
sdk-http     sdk-http     cloid      383.3     420.6      697.6      771.5      804.2      722.1      979.1
sdk-http     sdk-http     oid        377.6     399.6      709.1      802.7      816.1      756.0     1034.9
```

Double-lap timing probe, `10` samples per delay, cloid cancel only:

```text
delay_ms place_status cancel_status place_p50 place_p90 cancel_p50 cancel_p90 cancel_p99 update_p50 update_p90
0        resting=10   success=10       370.0     451.2      691.2      738.3      787.6      765.3      780.3
100      resting=10   success=10       349.9     383.0      679.2      762.7      812.2      700.4      885.1
250      resting=10   success=10       371.3     424.3      680.3      746.4      790.9      749.6      935.5
500      resting=10   success=10       375.6     427.9      685.9      787.6      797.0      714.6      926.7
1000     resting=10   success=10       358.1     384.6      692.1      785.3      903.4      736.3      853.1
2000     resting=10   success=10       352.5     425.0      701.6      752.1      869.0      748.4      908.2
3000     resting=10   success=10       356.5     394.5      690.4      706.2      886.8      747.9      984.0
```

Interpretation from live probe:

- Current live `xyz:AAPL` cancel RTTs from `gv0` are in the same degraded band
  observed historically after `2026-06-22`: roughly `690-750ms` p50 by post response,
  with user-stream cancel updates usually a bit later.
- `cancelByCloid` vs oid cancel did not show a large current-state gap. Oid was modestly
  faster on some rows, but not enough to explain the historical step change by itself.
- SDK HTTP vs gateway-signed WS/HTTP did not show a material current-state difference.
  The SDK version/path is therefore less likely to be the primary cause of the
  post-`2026-06-22` shift.
- The double-lap branch was not reproduced by this controlled one-order-at-a-time probe:
  all `0-3000ms` early cloid cancels returned success, all placements rested, and cleanup
  was clean. This does not invalidate the historical double-lap logs, but it argues that
  the 1s-to-3s widening is not sufficient to create the median clean-cancel degradation
  under current simple conditions.

### Cancel-by-Modify Probe, 2026-06-30

We also tested whether a resting ALO can be removed by modifying it "into the other
side". Initial order was always a buy ALO below mid (`xyz:AAPL`, size `0.05`).

Two interpretations were tested:

1. True side flip: modify the buy ALO into a sell ALO above mid.
   - One smoke sample returned `error: Attempted to modify to invalid new order`.
   - `cleanup_open_before=1`, so the original order remained resting until explicit cleanup.
   - This is **not** a viable cancel path.
2. Same-side cross ALO: modify the buy ALO to a buy ALO price above the ask.
   - HL returned `badAloPxRejected` / `Post only order would have immediately matched`.
   - `cleanup_open_before=0` in all `50/50` matrix samples, so the original order was
     already gone before cleanup. This does behave like a cancel-by-modify path.

Modify-cross matrix, `10` samples per row:

```text
place_path   modify_path  action            place_p50 place_p90 modify_p50 modify_p90 modify_p99 update_p50 update_p90 cleanup_before cleanup_after
manual-ws    manual-ws    modify-cross-alo      455.3     680.5      414.3      465.9      550.6       95.0      519.6 0/10           0/10
manual-ws    manual-http  modify-cross-alo      438.0     564.6      373.8      485.9      492.2      126.4      624.6 0/10           0/10
manual-ws    sdk-http     modify-cross-alo      627.7     805.8      435.0      504.8      507.5      467.3      586.0 0/10           0/10
manual-http  manual-http  modify-cross-alo      366.4     449.7      377.1      447.6      465.6      184.8      445.5 0/10           0/10
sdk-http     sdk-http     modify-cross-alo      371.5     395.3      379.3      450.0      536.7      140.9      795.0 0/10           0/10
```

Interpretation:

- The same-side crossed-ALO modify path is materially faster than current normal cancel
  (`~370-435ms` p50 vs `~690-750ms` p50 in the clean cancel matrix).
- It returns an error status, not cancel success, so strategy/gateway code would need to
  explicitly treat `badAloPxRejected` from a modify as terminal-for-old-order before this
  could be used as a cancel mechanism.
- This was tested with one order at a time and far-away tiny ALOs. It should not be
  assumed safe for production until tested under real batching/rate-limit conditions and
  with careful fill-risk analysis.

Focused side/transport breakdown, same-side crossed-ALO modify, `20` samples per row:

```text
initial_side terminal_path  place_p50 place_p90 place_p99 modify_p50 modify_p90 modify_p99 cleanup_before cleanup_after
buy          manual-ws          456.5     634.7    1138.7      446.2      510.3      797.3 0/20           0/20
buy          manual-http        473.9     540.7     661.5      418.7      507.2      652.8 0/20           0/20
sell         manual-ws          442.1     710.3     955.9      472.4      621.8      782.9 0/20           0/20
sell         manual-http        499.5     634.7    1151.1      416.5      492.3      566.8 0/20           0/20
```

Notes from focused breakdown:

- Both sides behaved as cancel-by-modify: `cleanup_open_before=0` and
  `cleanup_open_after=0` in all `80/80` focused samples.
- Manual HTTP terminal was modestly faster than manual WS terminal in this focused run:
  about `27ms` p50 faster on initial buys and `56ms` p50 faster on initial sells.
- The HTTP-vs-WS gap is much smaller than the normal-cancel degradation and should not be
  over-read from this sample. It may reflect WS post/receive-loop overhead or normal tail
  noise rather than an exchange-side semantic difference.
- `orderUpdates` timing in this modify test is not a clean terminal-ack measure because the
  probe sometimes observes delayed `open` updates for the original oid; post-response RTT is
  the cleaner comparison here.

Follow-up transport/order test, 2026-06-30:

Measurement definition:

- `place_post_rtt_ms` and `terminal_p*_ms` are client-side action-response RTTs from
  `time.perf_counter_ns()` just before action submission to receipt of the action response.
- For manual HTTP this is `/exchange` response time. For manual WS this is the matching
  websocket `channel=post` response frame. Both manual paths sign before the timer starts,
  so this comparison controls gateway-style signing cost.
- `orderUpdates` were not used for the terminal latency comparison. Warmup iterations were
  excluded from all percentiles below.

The prior HTTP-faster result did not survive a reversed-order retest. In the earlier focused
run, the case order was `manual-ws` terminal first, then `manual-http`. Reversing the order
and using `20` measured samples per row produced:

```text
side socket      terminal     n  place_p50 place_p90 terminal_p50 terminal_p90 terminal_p99 sendgap_p50 prev_place_p50
buy  subscribed  manual-http 20      402.6     496.0        460.7        559.0        661.0       432.7         1005.2
buy  subscribed  manual-ws   20      400.5     502.7        423.8        523.6        698.2       430.6         1054.5
sell subscribed  manual-http 20      432.3     529.2        452.5        493.2        529.5       462.3         1054.4
sell subscribed  manual-ws   20      402.4     528.4        419.0        516.8        533.2       432.3         1020.2
```

Action-only websocket test:

```text
side socket       terminal     n  place_p50 place_p90 terminal_p50 terminal_p90 terminal_p99 sendgap_p50 prev_place_p50
buy  action-only  manual-http 13      416.3     597.9        450.6        581.7        744.8       446.6         1072.9
buy  action-only  manual-ws   13      446.3     516.2        466.9        588.3        592.2       476.2         1041.3
sell action-only  manual-http 10      459.4     524.3        465.6        502.7        831.0       489.7         1041.9
sell action-only  manual-ws   10      411.4     516.7        449.1        529.9        636.0       441.5         1042.9
```

Cadence, excluding warmup and failed rows:

```text
dataset         actions elapsed_s actions_per_s action_interval_p50 action_interval_p90 place_to_terminal_p50 terminal_to_next_place_p50
buy_subscribed       80      73.0          1.08               650.0              1273.0                 432.7                     1054.0
buy_actiononly       52      46.6          1.09               634.0              1312.0                 474.5                     1073.0
sell_subscribed      80      66.4          1.19               681.0              1140.0                 460.1                     1024.0
sell_actiononly      40      29.7          1.31               637.0              1125.0                 485.9                     1043.0
```

Observations:

- The HTTP terminal path is not consistently faster than WS once ordering is reversed.
  In the subscribed retest, WS was faster at p50 on both sides by about `34ms`.
- Removing the `orderUpdates` subscription did not make WS materially faster. It also made
  the longer buy action-only run operationally worse: the websocket closed with
  `1000 (OK) Inactive` beginning at trial `31`, after the useful measured sample count was
  already `13` per row. An action-only WS path probably needs a keepalive/subscription or
  reconnect policy before it is reliable enough to benchmark for long runs.
- These probes were not rapid-fire: measured action send rate was only about `1.1-1.3`
  actions/sec, and the next place action was typically about `1.0s` after the prior terminal
  action. This should not be interpreted as a local rate-limit pressure test.
- Some immediate cleanup probes saw the original order still visible right after the
  modify-cross response when `update_timeout_s=0`; final open-order checks were zero. The
  earlier wait-bearing modify-cross tests are the cleaner evidence that the old order is
  removed, while this retest is cleaner for post-response RTT.

External benchmark comparison:

- A Discord benchmark reported approximately `155ms` non-crossing maker place, `157ms`
  amend, `470ms` cancel, and `487ms` IOC taker.
- On gv0 today, our warmup-excluded maker place p50 is still around `400-460ms`, so our
  placement path is materially slower than that benchmark if the measurement definitions
  match.
- Our same-side crossed-ALO modify terminal response is around `419-467ms` p50, roughly in
  the neighborhood of that reported cancel/IOC number but much slower than the reported
  amend number.
- Our normal cancel path from the earlier clean matrix remains worse, around `690-750ms`
  p50. That is still the stronger anomaly than HTTP-vs-WS transport choice.

### Passive Amend Probe, 2026-06-30

We tested whether normal passive `batchModify` was a hidden fast path comparable to the
reported `~157ms` amend number. These were still one-order-at-a-time `xyz:AAPL` ALOs under
the `$20` notional cap.

Far parked order to nearer passive quote, `10` measured samples per row:

```text
side direction   terminal     n  place_p50 place_p90 amend_p50 amend_p90 amend_p99
buy  far->near   manual-http 10      438.2     529.2     392.3     670.4    1017.6
buy  far->near   manual-ws   10      415.0     462.2     390.0     422.9     446.1
sell far->near   manual-http 10      381.9     502.8     423.2     472.5     547.3
sell far->near   manual-ws   10      385.2     525.3     455.2     538.6     668.7
```

Near passive quote to far parked order, `5` measured samples per row:

```text
side direction   terminal     n  place_p50 place_p90 amend_p50 amend_p90 amend_p99
buy  near->far   manual-http  5      413.8     484.2     483.8     578.4     578.4
buy  near->far   manual-ws    5      452.3     500.6     401.8     753.8     753.8
sell near->far   manual-http  5      458.7     514.3     401.8     622.1     622.1
sell near->far   manual-ws    5      410.8     483.9     399.1     534.7     534.7
```

Interpretation:

- Passive amend on `xyz:AAPL` from `gv0` was not a `~157ms` path in this setup.
- A parked-order design may still simplify strategy state, but it does not currently look
  like a direct latency escape hatch for US equities.

### Fast Cancel Flag Probe, 2026-06-30

Hyperliquid docs now show optional fast cancel encoding:

```text
{"type": "cancel", "cancels": [{"a": asset, "o": oid}], "f": true}
```

The probe encoded `f` at the top level of `cancel` / `cancelByCloid` actions only when
`fast_cancel=True`; baseline actions omitted `f` entirely. `f: false` was never sent.

Fast-cancel matrix, `xyz:AAPL`, buy ALO, size `0.05`, `10` measured samples per row:

```text
terminal     key   fast  n  place_p50 cancel_p50 cancel_p90 cancel_p99 update_p50 update_p90 cleanup_before cleanup_after
manual-http  cloid false 10     410.4      757.6      854.5     1141.1      820.6      998.2 10/10          10/10
manual-http  cloid true  10     478.3      420.3      465.5      520.9      483.2      606.8 10/10          10/10
manual-http  oid   false 10     459.2      754.1      829.6      998.2      847.6     1022.5 10/10          10/10
manual-http  oid   true  10     439.3      402.3      452.5      670.7      464.2      695.0 10/10          10/10
manual-ws    cloid false 10     387.7      750.5      836.8      856.6      817.2      886.2 10/10          10/10
manual-ws    cloid true  10     424.2      405.7      470.7      732.3      508.7      582.2 10/10          10/10
manual-ws    oid   false 10     423.5      710.0      792.3      856.1      809.7      903.6 10/10          10/10
manual-ws    oid   true  10     436.5      427.2      571.1      612.7      457.4      612.7 10/10          10/10
```

Fast minus normal p50 deltas:

```text
terminal     key    normal_p50 fast_p50 delta_ms
manual-ws    cloid       750.5    405.7   -344.8
manual-ws    oid         710.0    427.2   -282.8
manual-http  cloid       757.6    420.3   -337.3
manual-http  oid         754.1    402.3   -351.8
```

Interpretation:

- Contrary to the doc note that fast currently has no other effect, `f: true` materially
  reduced current gv0 `xyz:AAPL` cancel RTTs in this run.
- Fast cancel moved normal cancel from the degraded `~710-758ms` p50 band into the
  `~402-427ms` p50 band, similar to the same-side crossed-ALO modify response band but
  with normal cancel semantics and `success` statuses.
- This is the strongest live mitigation found so far. It should be implemented as
  top-level `f: true` on cancel/cancelByCloid actions behind an explicit config flag, with
  careful omission of `f` when disabled.

Gateway implementation update:

- `pygateway/wsgateway.py` now enables fast cancels by default for all HL
  `cancelByCloid` batches sent by the gateway.
- The action is encoded as top-level `f: true` before signing.
- Rollback switch: start wsgateway with `--disable-fast-cancels` to omit the field.
- Important: the disabled path omits `f` entirely; it does not send `f: false`.

### Networking Notes, 2026-06-30

`gv0` DNS resolution for `api.hyperliquid.xyz` returned CloudFront-style IPv4s:

```text
3.173.197.79
3.173.197.48
3.173.197.51
3.173.197.8
```

Simple `curl` POST timing to `/info` with `{"type":"allMids","dex":"xyz"}`, `10` samples:

```text
metric          p50_ms p90_ms
DNS              ~1.0   ~1.6
TCP connect      ~2.8   ~3.5
TLS appconnect  ~42.3  ~44.7
total           ~49.1  ~77.2
```

Interpretation:

- Basic HTTPS path timing from gv0 to Hyperliquid is far below the `700ms` normal-cancel
  action-response RTT, so local network path alone is unlikely to explain the degraded
  normal cancel path.
- Network/location can still matter versus external `~155ms` placement benchmarks, but the
  fast-cancel result points more strongly at exchange/action prioritization than raw
  transport RTT.

## Reproduction Notes

Raw data source:

```text
/home/pktrade/scratch/tradeperf/{gf1,gf2,gf3}/*/*/orders_YYYYMMDD.csv
```

Main dates checked:

```text
20260618
20260622
20260623
20260624
20260625
20260626
20260629
```

Important implementation detail:

- Pair `CancelAck` to the most recent prior `Cancel` for the same order id.
- Filter to `ADD` orders only.
- Filter to `xyz:` symbols only.
- For the clean-cancel table, require `Cancel` time after `NewOrdAck` and no `Exec`
  between `Cancel` and `CancelAck`.
