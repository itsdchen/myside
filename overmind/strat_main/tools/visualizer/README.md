# VizData — Market Data Visualizer

Interactive visualizer for HIP3 perp market data alongside remote reference prices
(e.g. `xyz:META` on Hyperliquid vs `META` on NASDAQ).

## Architecture

Two components:

1. **`vizdata`** (C++) — Replays historical gzpbf data, computes features, outputs CSV.
2. **`app.py`** (Python/Dash) — Web UI that runs `vizdata` on demand and renders interactive charts.

```
┌──────────────┐       CSV        ┌──────────────┐
│   vizdata    │ ──────────────►  │   app.py     │
│  (C++ tool)  │  ~/.cache/vizdata│  (Dash app)  │
│              │                  │              │
│  gzpbf ──►  │                  │  ──► browser  │
│  replay +   │                  │  interactive  │
│  EMS compute │                  │  Plotly charts│
└──────────────┘                  └──────────────┘
```

## Quick Start

### Prerequisites

Build `vizdata`:

```bash
./compileit.sh
```

Install Python dependencies:

```bash
pip install -r requirements.txt   # dash, plotly, pandas
```

### Run the Dash App

```bash
python app.py [--port 8050] [--debug]
```

Open `http://localhost:8050` in a browser. Select a symbol and date, click **Load**.

The app calls `vizdata` as a subprocess to generate the CSV (cached in
`~/.cache/vizdata/`), then renders the chart. Reloading the same symbol+date
is instant.

### Run vizdata Standalone

```bash
./vizdata \
  --book Hyperliquid --symbol xyz:META \
  --remote-book TopBookEquity --remote-symbol META \
  --date 20260220 \
  --out /tmp/vizdata_meta.csv \
  --interval-ms 100
```

#### vizdata CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `-b, --book` | *(required)* | Local market (e.g. `Hyperliquid`) |
| `--symbol` | *(required)* | Local symbol (e.g. `xyz:META`) |
| `--remote-book` | *(required)* | Remote market (e.g. `TopBookEquity`, `TopBookCme`) |
| `--remote-symbol` | *(required)* | Remote symbol (e.g. `META`, `NQ`, `HG`) |
| `-d, --date` | *(required)* | Date `YYYYMMDD` |
| `-o, --out` | *(required)* | Output CSV path |
| `--interval-ms` | `100` | Min milliseconds between CSV rows (0 = every book update) |
| `--datadir` | `~/tardis_datasets/gzpbf` | Historical data directory |
| `--start-time` | `18:00:00 America/New_York` | Session start time |
| `--end-time` | `17:59:55 America/New_York` | Session end time |

## CSV Columns

| Column | Description |
|--------|-------------|
| `time_ms` | Unix timestamp in milliseconds |
| `local_bid` | Best bid price (Hyperliquid) |
| `local_ask` | Best ask price (Hyperliquid) |
| `local_mid` | Mid price `(bid+ask)/2` |
| `remote_mid` | Remote reference mid price (e.g. NASDAQ) |
| `local_spread` | `ask - bid` |
| `premium_bps` | `(local_mid - remote_mid) / remote_mid * 10000` |
| `vol_ems_remote_4s` | Volatility EMS (4s decay) — decaying sum of \|remote return\| |
| `vol_ems_remote_30s` | Volatility EMS (30s decay) |
| `momentum_1s` | Momentum EMS (1s decay) — decaying sum of signed remote return |
| `momentum_4s` | Momentum EMS (4s decay) |
| `trade_rate_ems` | Trade intensity — decaying count of local trades (30s decay) |
| `trade_notional_signed_ems` | Buy/sell pressure — signed trade notional EMS |
| `liq_depth_buy_100k` | Price depth (fraction of mid) to sell $100k into bids |
| `liq_depth_buy_250k` | Price depth to sell $250k into bids |
| `liq_depth_sell_100k` | Price depth to buy $100k from asks |
| `liq_depth_sell_250k` | Price depth to buy $250k from asks |
| `deep_bid_notional` | Total bid notional within 50bps of mid |
| `deep_ask_notional` | Total ask notional within 50bps of mid |
| `local_bb_size` | Quantity at best bid |
| `local_ba_size` | Quantity at best ask |

## Visualizer Layers

All layers are togglable via checkboxes in the UI:

| Layer | Description |
|-------|-------------|
| **Price** | Bid/Ask lines (or Mid) + remote mid. Toggle between modes with radio button. |
| **Depth bands** | Shaded bands around bid/ask showing 100k and 250k liquidity depth. |
| **Premium** | `premium_bps` as a line with zero reference. |
| **Volatility** | `vol_ems_remote_4s` and `vol_ems_remote_30s`. |
| **Momentum** | `momentum_1s` and `momentum_4s` with zero reference. |
| **Trade flow** | `trade_rate_ems` and `trade_notional_signed_ems`. |
| **Book thickness** | `deep_bid/ask_notional` (solid) and `bb/ba_size` (dotted). |

Charts support zoom, pan, and hover inspection. Large datasets (>100k rows)
are automatically downsampled. All traces use WebGL (`Scattergl`) for
performance.

## Symbol Mapping

Symbols are loaded from `../known_syms.txt`. Remote reference mapping defaults
to `(SYMBOL, TopBookEquity)` for equities. Overrides for commodities:

| Local | Remote Symbol | Remote Book |
|-------|---------------|-------------|
| CL | CL | TopBookCme |
| COPPER | HG | TopBookCme |
| NATGAS | NG | TopBookCme |
| XYZ100 | NQ | TopBookCme |

## Files

```
visualizer/
  app.py              Dash web app
  requirements.txt    Python dependencies
  README.md           This file

src/pktrade/toolbins/
  vizdata.cc          C++ historical data dumper
  CMakeLists.txt      Build config (vizdata target added)
```
