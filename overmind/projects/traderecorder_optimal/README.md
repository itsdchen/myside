# TradeRecorder + Oracle DP: Inverted Strategy Optimization

## Motivation

Classical strategy optimization follows: **params → strategy → trades → PnL → iterate**.
This project inverts that loop:

1. Record ALL book trades with delayed market features (simulating decision-time info)
2. Compute oracle-optimal trade sequences via backward-induction DP (perfect foresight)
3. Regress features → oracle decisions to reverse-engineer what strategy parameters
   would have produced the optimal trades

### Why invert?

The project originated from HOOD (Robinhood on Hyperliquid) parameter optimization
hitting diminishing returns. After 156 SimVariations sweeps across vol_widen,
momentum_pred, liq_depth, and ladder parameters, we found:

- Volatility-reactive mechanisms that helped TSLA all **hurt** HOOD
- Only structural ladder changes improved performance (sim_score: 70 → 80)
- Classical iteration was noisy and slow

The inverted approach solves several problems:

- **Sidesteps sim noise**: Directly examines profitable trade structure instead of
  searching a high-dimensional parameter space with noisy PnL feedback
- **Discovers missing mechanisms**: Oracle patterns that don't map to any existing
  parameter reveal new mechanisms needed
- **Provides a ceiling**: Oracle PnL establishes theoretical max alpha, showing what
  fraction the current strategy captures
- **Per-symbol understanding**: Explains *why* TSLA vs HOOD respond differently to
  the same mechanisms


## Architecture

### C++ Component: TradeRecorder ordex

**Files**: `src/pktrade/ordex/trade_recorder.{h,cc}`

A passive ordex (inherits Ordex, TempoListener, SignalListener, BeaconListener)
modeled on DiagnosticsMM but stripped of all quoting/order logic. Key differences:

| DiagnosticsMM | TradeRecorder |
|---|---|
| Places orders, records OUR fills | No orders, records ALL book trades |
| Features snapshotted at fill time | Features snapshotted every 100ms into ring buffer |
| Markout horizons: 1s/5s/30s/60s/300s | Markout horizons: 30s/300s |
| One CSV row per our fill | One CSV row per aggregated trade event |

#### Design decisions

**Feature ring buffer (100ms snapshots, 5s max)**:
Features are snapshotted into a `std::deque<FeatureSnapshot>` every 100ms via a
TimeTempo. When a trade occurs, we look up the snapshot closest to
`trade_time - delay_s`. This simulates what you would have known at decision time,
not at fill time.

**Delayed feature lookup**:
- **1.0s delay for maker features**: Approximates roundtrip latency for passive
  order placement/cancellation on Hyperliquid. Represents "what you knew when
  deciding to place/keep your resting order."
- **1.5s delay for taker features**: Aggressive crossing has higher latency than
  passive order management.

**Trade aggregation by transact_t**:
Multiple fills at the same `transact_t` (exchange transaction timestamp) are
aggregated into a single event with VWAP price, total quantity, and net signed
quantity. This collapses multi-level sweeps into single logical events.

**Markout horizons (30s, 300s)**:
Used as fill-quality proxies in the first-pass markout regression before running
the full DP. The 30s horizon captures short-term adverse selection; 300s captures
medium-term directional moves.

#### Feature set (26 fields per snapshot)

```
Category          Fields
─────────────────────────────────────────────────────
Prices            remote_mid, local_mid, spread, bid_size, ask_size
Vol EMS (7 TDC)   vol_100ms, vol_1s, vol_4s, vol_10s, vol_30s, vol_60s, vol_300s
Momentum EMS (5)  mom_100ms, mom_1s, mom_4s, mom_10s, mom_30s
Trade micro (10)  trade_rate, trade_size, trade_notional_signed,
                  trade_notional_abs, book_depth_bid, book_depth_ask,
                  buy_trade_depth, sell_trade_depth, large_trade_rate, trade_depth
```

All EMS values use the same exponential moving sum math as DiagnosticsMM
(see `diagnostics_mm.cc:881-1053`). Vol and momentum EMS are updated in
`onSignalValue()` on remote signal updates. Trade microstructure EMS are
updated in `onTrade()`.

#### CSV output format (60 columns)

```
time_ms, agg_px, agg_qty, net_signed_qty, n_trades, remote_mid_at_trade,
mk_remote_mid, mk_local_mid, mk_spread, ..., mk_trade_depth,     (27 maker feature cols)
tk_remote_mid, tk_local_mid, tk_spread, ..., tk_trade_depth,      (27 taker feature cols)
markout_30s, markout_300s
```

Maker features have prefix `mk_`, taker features have prefix `tk_`.

### Python Component: Config Generator

**File**: `pybin/TradeRecorder.py`

Modeled on `pybin/SymDiag.py`. Generates pktrade configs with:
- `type: "TradeRecorder"` ordex (no order params)
- `max_orders: 0` in risk config (safety)
- Two tempos: FinalTempo (trade caller, no-op) + TimeTempo (100ms snapshots)
- Configurable `maker_delay_s` and `taker_delay_s`
- Same REMOTE_REF table as SymDiag for equity remote signal routing

### Python Component: Oracle DP Pipeline

**File**: `pybin/oracle_dp.py`

Four analysis components:

#### 1. `maker_oracle_dp(df, max_pos, horizon_col)`
Backward-induction DP for passive market making.
- **State**: `(event_index, position)` where position ∈ [-max_pos, max_pos]
- **Actions**: Fill (matching passive side of the trade) or skip
- **Fill value**: `(markout_mid - agg_px) * fill_side` using the markout horizon
- **Interpretation**: "An unconstrained maker quoting at the inside at all times —
  which fills should they have been positioned for?"

The maker oracle is most directly relevant to RelWideMM2 since it's a passive
strategy. Oracle decisions that say "don't rest here" map to placement thresholds,
widening, and skewing parameters.

#### 2. `taker_oracle_dp(df, max_pos, spread_col, horizon_col)`
Backward-induction DP for aggressive crossing.
- **State**: `(time_index, position)` sampled at trade events
- **Actions**: Buy (cross ask), sell (cross bid), or hold
- **Cost**: Half-spread per trade
- **Interpretation**: "When should I have been aggressive?"

The taker oracle is more of a signal/timing model — identifies moments where
aggressive entry would have been profitable.

#### 3. `markout_regression(df, prefix, horizon_col)`
Ridge regression: delayed features → signed markout return.
- Quick first-pass analysis (no DP needed)
- Reports R², top 10 feature coefficients
- High R² means features are predictive of future price movement

#### 4. `oracle_regression(df, oracle_decisions, prefix, label)`
Logistic regression: delayed features → oracle fill/skip decision.
- Reports accuracy, top 10 feature coefficients
- Maps directly to strategy mechanisms:
  - High coefficient on `mk_vol_30s` → vol_widen mechanism
  - High coefficient on `mk_mom_4s` → momentum_pred mechanism
  - High coefficient on `mk_book_depth_bid/ask` → liq_depth mechanism

#### DP complexity
The DP is tiny: ~(2*max_pos+1) × N_events. For max_pos=5 and 10K events/day,
that's 11 × 10K = 110K states. Runs in under a second.


## How to Run

### Prerequisites
- Built pktrade binary (`cmake --build build`)
- Python venv at `/home/pktrade/.venvs/v1/bin/python`
- Market data available in `~/tardis_datasets/gzpbf/`

### Step 1: Record trades

```bash
# Single symbol, single date
/home/pktrade/.venvs/v1/bin/python pybin/TradeRecorder.py run HOOD \
    --start 20260224 --end 20260226 \
    --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test

# With custom delays
/home/pktrade/.venvs/v1/bin/python pybin/TradeRecorder.py run HOOD \
    --start 20260224 --end 20260226 \
    --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test \
    --maker-delay 1.0 --taker-delay 1.5
```

This runs pktrade sims that produce `trade_record_YYYYMMDD.csv` files in the workdir.

**Important**: Run from the repo root (`~/tradefi/retraded_1/`) so Python path
resolution works correctly.

### Step 2: Oracle DP analysis

```bash
# Default: max_pos=5, horizon=markout_30s
/home/pktrade/.venvs/v1/bin/python pybin/oracle_dp.py \
    --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test

# Custom max_pos and horizon
/home/pktrade/.venvs/v1/bin/python pybin/oracle_dp.py \
    --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test \
    --max-pos 10 --horizon markout_300s
```

Or via the TradeRecorder.py analyze subcommand:
```bash
/home/pktrade/.venvs/v1/bin/python pybin/TradeRecorder.py analyze \
    --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test \
    --max-pos 5
```

### Output

- `trade_record_YYYYMMDD.csv` — one per sim date, ~5K-6K rows/day for HOOD
- `oracle_dp_results.json` — full analysis results


## Initial Results (HOOD, 2 days: 20260224-20260225)

```
10,751 aggregated trade events

MAKER ORACLE (max_pos=5):
  Optimal PnL:    $308
  Fill rate:      4,485 / 10,751 events (42%)
  Buy/sell split: 2,243 / 2,242

TAKER ORACLE (max_pos=5):
  Optimal PnL:    $322
  Trade rate:     5,447 / 10,751 events (51%)
  Buy/sell split: 2,723 / 2,724

MARKOUT REGRESSION (features → 30s markout return):
  Maker features R²: 0.431
  Taker features R²: 0.430
  Top features: trade_notional_abs, trade_size, local_mid, remote_mid

ORACLE REGRESSION (features → oracle fill/skip):
  Maker accuracy: 59.0%
  Taker accuracy: 71.1%
  Top maker features: remote_mid-local_mid divergence, mom_4s, mom_10s, vol_30s
  Top taker features: remote_mid-local_mid divergence (3.26x), mom_10s, mom_4s, vol_30s
```

### Interpretation

- **43% markout R²** is high — delayed features explain nearly half the variance
  in 30s forward returns. The feature set is informative.
- **Taker oracle accuracy (71%)** is significantly higher than maker (59%).
  Taker decisions are more predictable from features, possibly because the
  taker oracle has full control over when to trade (vs maker being constrained
  to actual book trade events).
- **remote_mid − local_mid divergence** is the strongest signal for both oracles.
  This is the premium/basis signal — the core of the existing strategy.
- **Momentum features (mom_4s, mom_10s)** are prominent in oracle decisions,
  suggesting momentum_pred should be valuable — but our SimVariations sweeps
  showed it hurts HOOD. This tension is worth investigating: perhaps the sign
  or TDC needs to differ from what was swept.
- **vol_30s** appears in both oracle regressions with negative coefficients,
  consistent with "widen when vol is high" — but vol_widen sweeps hurt HOOD.
  Again, the oracle may be revealing subtler use than the existing mechanism.


## Future Directions

### Immediate next steps
1. Run on more dates/symbols to check stability of results
2. Compare HOOD oracle features vs TSLA oracle features to explain their
   divergent responses to vol_widen/momentum_pred
3. Investigate the mom_4s/mom_10s vs momentum_pred discrepancy

### Potential extensions
- **Full DP with multi-step position management** instead of per-event markout proxy
- **Nonlinear models** (gradient boosted trees) for oracle regression
- **Time-of-day interaction features** (oracle behavior at open vs close vs overnight)
- **Multi-symbol oracle** comparing feature importance across asset classes
- **Closed-loop optimization**: use oracle regression coefficients to seed
  SimVariations sweeps, then validate


## File Inventory

| File | Purpose |
|------|---------|
| `src/pktrade/ordex/trade_recorder.h` | TradeRecorder class declaration + structs |
| `src/pktrade/ordex/trade_recorder.cc` | TradeRecorder implementation |
| `src/pktrade/ordex/ordex_factory.cc` | Factory registration (modified) |
| `src/pktrade/ordex/CMakeLists.txt` | Build registration (modified) |
| `pybin/TradeRecorder.py` | Config generator + sim runner |
| `pybin/oracle_dp.py` | Oracle DP + regression pipeline |
| `overmind/projects/traderecorder_optimal/README.md` | This document |

### Related reference files
| File | Relationship |
|------|-------------|
| `src/pktrade/ordex/diagnostics_mm.{h,cc}` | Template for TradeRecorder C++ code |
| `pybin/SymDiag.py` | Template for TradeRecorder.py config generation |
| `pybin/SimVariations.py` | Classical parameter sweep tool (what this project inverts) |
