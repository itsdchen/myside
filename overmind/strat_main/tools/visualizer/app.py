#!/usr/bin/env python3
"""
Interactive market data visualizer for HIP3 perps.

Runs the C++ vizdata tool on demand to generate CSV data, then renders
interactive Plotly charts via Dash.  Supports zoom/pan, togglable layers,
bid/ask vs mid mode, and book-depth shading bands.

Usage:
    python app.py [--port 8050] [--debug]
"""

import argparse
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import dash
from dash import dcc, html, Input, Output, State
import numpy as np
import plotly.graph_objects as go
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_DIR = SCRIPT_DIR.parent               # overmind/strat_main/tools/
REPO_ROOT = SCRIPT_DIR.parents[3]           # retraded_6/
BIN_DIR = REPO_ROOT / "bin"
VIZDATA_BIN = BIN_DIR / "vizdata"
CACHE_DIR = Path.home() / ".cache" / "vizdata"
KNOWN_SYMS_PATH = SCRIPT_DIR.parent / "known_syms.txt"
DATA_DIR = Path.home() / "tardis_datasets" / "gzpbf"

# Add tools dir to path so we can import md_exists.
sys.path.insert(0, str(TOOLS_DIR))
import md_exists

# ── Symbol → remote mapping (from SymDiag.py) ────────────────────────────────

REMOTE_REF = {
    "CL":      ("CL",  "TopBookCme"),
    "COPPER":  ("HG",  "TopBookCme"),
    "NATGAS":  ("NG",  "TopBookCme"),
    "XYZ100":  ("NQ",  "TopBookCme"),
}


def get_remote(name):
    """Return (remote_symbol, remote_book) for a HIP3 commodity name."""
    return REMOTE_REF.get(name, (name, "TopBookEquity"))


def load_symbols():
    """Load symbol list from known_syms.txt, falling back to a hardcoded list."""
    syms = []
    if KNOWN_SYMS_PATH.exists():
        for line in KNOWN_SYMS_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("xyz:"):
                syms.append(line[4:])  # strip "xyz:" prefix
    if not syms:
        syms = [
            "META", "TSLA", "NVDA", "GOLD", "AAPL", "MSFT", "GOOGL", "AMZN",
            "AMD", "HOOD", "INTC", "PLTR", "COIN", "NFLX", "CL", "COPPER",
            "NATGAS", "XYZ100", "SILVER",
        ]
    return syms


SYMBOLS = load_symbols()

# ── Dash App ──────────────────────────────────────────────────────────────────

app = dash.Dash(__name__, title="VizData")

LAYER_OPTIONS = [
    {"label": "Price (bid/ask or mid + remote)", "value": "price"},
    {"label": "Trades", "value": "trades"},
    {"label": "Depth bands (250k)", "value": "depth"},
    {"label": "Premium (bps)", "value": "premium"},
    {"label": "Volatility EMS", "value": "vol"},
    {"label": "Momentum EMS", "value": "momentum"},
    {"label": "Trade flow", "value": "trade_flow"},
    {"label": "Book thickness", "value": "book_thick"},
]
VALID_LAYERS = {opt["value"] for opt in LAYER_OPTIONS}
VALID_MODES = {"bidask", "mid"}

app.layout = html.Div([
    # URL routing
    dcc.Location(id="url", refresh=False),
    html.Div(id="url-dummy", style={"display": "none"}),

    # Top bar
    html.Div([
        html.Label("Symbol:", style={"marginRight": "6px", "fontWeight": "bold"}),
        dcc.Dropdown(
            id="symbol-dropdown",
            options=[{"label": s, "value": s} for s in SYMBOLS],
            value="META",
            style={"width": "160px", "display": "inline-block", "verticalAlign": "middle"},
        ),
        html.Label("Date:", style={"marginLeft": "20px", "marginRight": "6px", "fontWeight": "bold"}),
        dcc.Input(
            id="date-input",
            type="text",
            placeholder="YYYYMMDD",
            value="20260220",
            style={"width": "110px", "verticalAlign": "middle"},
        ),
        html.Button("Load", id="load-btn", n_clicks=0,
                     style={"marginLeft": "16px", "verticalAlign": "middle"}),
        html.Button("Clear Cache", id="clear-cache-btn", n_clicks=0,
                     style={"marginLeft": "8px", "verticalAlign": "middle"}),
        html.Button("?", id="help-toggle-btn", n_clicks=0,
                     style={"marginLeft": "8px", "verticalAlign": "middle",
                            "fontWeight": "bold", "fontSize": "14px",
                            "width": "28px", "height": "28px", "padding": "0",
                            "borderRadius": "50%", "cursor": "pointer"}),
        html.Span(id="status-text", style={"marginLeft": "16px", "color": "#666"}),
    ], style={"padding": "12px", "display": "flex", "alignItems": "center",
              "borderBottom": "1px solid #ddd"}),

    # Controls row
    html.Div([
        html.Div([
            html.Label("Mode:", style={"fontWeight": "bold", "marginRight": "8px"}),
            dcc.RadioItems(
                id="price-mode",
                options=[
                    {"label": "Bid/Ask", "value": "bidask"},
                    {"label": "Mid", "value": "mid"},
                ],
                value="bidask",
                inline=True,
                style={"display": "inline-block"},
            ),
        ], style={"marginRight": "30px"}),
        html.Div([
            html.Label("Layers:", style={"fontWeight": "bold", "marginRight": "8px"}),
            dcc.Checklist(
                id="layer-checklist",
                options=LAYER_OPTIONS,
                value=["price"],
                inline=True,
                style={"display": "inline-block"},
            ),
        ]),
    ], style={"padding": "8px 12px", "display": "flex", "alignItems": "center",
              "borderBottom": "1px solid #eee", "flexWrap": "wrap"}),

    # Help panel (toggled by "?" button)
    html.Div(id="help-panel", children=[
        html.H4("Layer Reference", style={"marginTop": "0"}),
        html.Table([
            html.Thead(html.Tr([
                html.Th("Layer", style={"textAlign": "left", "paddingRight": "16px"}),
                html.Th("Description", style={"textAlign": "left"}),
            ])),
            html.Tbody([
                html.Tr([
                    html.Td("Price", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td("Local Bid/Ask/Mid from HIP3 perp (Hyperliquid). "
                            "Remote Mid (dotted) from equity or CME reference feed."),
                ]),
                html.Tr([
                    html.Td("Trades", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td([
                        "Green \u25b2 = aggressor buy, Red \u25bc = aggressor sell. ",
                        "Marker size \u221d notional (99th-percentile normalized, 4\u201320 px).",
                    ]),
                ]),
                html.Tr([
                    html.Td("Depth bands", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td("Shaded band showing the price to fill $100k / $250k notional from best bid/ask."),
                ]),
                html.Tr([
                    html.Td("Premium", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td(html.Code("(local_mid \u2212 remote_mid) / remote_mid \u00d7 10\u2009000 bps")),
                ]),
                html.Tr([
                    html.Td("Volatility EMS", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td([
                        html.Code("|return| + exp(\u2212dt/\u03c4) \u00d7 prev"),
                        " \u2014 \u03c4 = 4 s (fast), \u03c4 = 30 s (slow). Computed from remote returns.",
                    ]),
                ]),
                html.Tr([
                    html.Td("Momentum EMS", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td([
                        html.Code("return + exp(\u2212dt/\u03c4) \u00d7 prev"),
                        " \u2014 \u03c4 = 1 s (fast), \u03c4 = 4 s (slow). Signed (positive = up).",
                    ]),
                ]),
                html.Tr([
                    html.Td("Trade Flow", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td([
                        "Rate: ", html.Code("1 + exp(\u2212dt/10s) \u00d7 prev"), " (count-weighted). ",
                        "Flow: ", html.Code("sign \u00d7 notional + exp(\u2212dt/10s) \u00d7 prev"),
                        " (buy-positive).",
                    ]),
                ]),
                html.Tr([
                    html.Td("Book Thickness", style={"verticalAlign": "top", "paddingRight": "16px", "fontWeight": "bold"}),
                    html.Td("Deep Bid/Ask Notional = liquidity within 50 bps of mid. "
                            "BB/BA Size = top-of-book quantity."),
                ]),
            ]),
        ], style={"borderCollapse": "collapse", "fontSize": "13px"}),
    ], style={"display": "none", "padding": "12px 16px",
              "background": "#f8f8f0", "borderBottom": "1px solid #ddd"}),

    # Loading spinner
    dcc.Loading(
        id="loading",
        type="default",
        children=[
            dcc.Graph(
                id="main-chart",
                style={"height": "85vh"},
                config={
                    "scrollZoom": True,
                    "displayModeBar": True,
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                },
            ),
        ],
    ),

    # Hidden store for CSV data path
    dcc.Store(id="csv-store"),
], style={"fontFamily": "monospace"})


# ── Callbacks ─────────────────────────────────────────────────────────────────

@app.callback(
    Output("csv-store", "data"),
    Output("status-text", "children"),
    Input("load-btn", "n_clicks"),
    Input("clear-cache-btn", "n_clicks"),
    State("symbol-dropdown", "value"),
    State("date-input", "value"),
    prevent_initial_call=True,
)
def load_data(load_clicks, clear_clicks, symbol, date_str):
    """Run vizdata (if needed) and store the CSV path.  Also handles cache clearing."""
    if not symbol or not date_str:
        return None, "Select symbol and date."

    date_str = date_str.strip()
    if len(date_str) != 8 or not date_str.isdigit():
        return None, "Invalid date format. Use YYYYMMDD."

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = CACHE_DIR / f"{symbol}_{date_str}.csv"
    trades_path = CACHE_DIR / f"{symbol}_{date_str}_trades.csv"

    triggered = (dash.callback_context.triggered or [{}])[0].get("prop_id", "").split(".")[0]

    if triggered == "clear-cache-btn":
        cleared = []
        for p in (csv_path, trades_path):
            if p.exists():
                p.unlink()
                cleared.append(p.name)
        if cleared:
            return None, f"Cleared cache: {', '.join(cleared)}"
        return None, "No cached file to clear."

    # Load button — generate if needed.
    if csv_path.exists() and csv_path.stat().st_size > 0:
        return {"csv": str(csv_path), "trades_csv": str(trades_path)}, f"Loaded from cache: {csv_path.name}"

    if not VIZDATA_BIN.exists():
        return None, f"vizdata binary not found at {VIZDATA_BIN}"

    remote_sym, remote_book = get_remote(symbol)

    # Ensure market data exists locally; download from S3 if missing.
    local_sym = f"xyz:{symbol}"
    syms_books_to_dates = {
        (local_sym, "Hyperliquid"): [date_str],
        (remote_sym, remote_book): [date_str],
    }
    missing = md_exists.missing_data_files(syms_books_to_dates, str(DATA_DIR))
    if missing["files_to_copy_props"]:
        names = list(missing["files_to_copy_props"].keys())
        try:
            md_exists.check_for_data(syms_books_to_dates, str(DATA_DIR),
                                     dl_automatically=True)
        except Exception as e:
            return None, f"Data download failed: {e}"

    cmd = [
        str(VIZDATA_BIN),
        "--book", "Hyperliquid",
        "--symbol", local_sym,
        "--remote-book", remote_book,
        "--remote-symbol", remote_sym,
        "--date", date_str,
        "--out", str(csv_path),
        "--trades-out", str(trades_path),
        "--interval-ms", "100",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            err = result.stderr[:300] if result.stderr else "unknown error"
            return None, f"vizdata failed: {err}"
    except subprocess.TimeoutExpired:
        return None, "vizdata timed out (>5 min)"
    except Exception as e:
        return None, f"Error running vizdata: {e}"

    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None, "vizdata produced no output."

    return {"csv": str(csv_path), "trades_csv": str(trades_path)}, f"Generated: {csv_path.name}"


@app.callback(
    Output("main-chart", "figure"),
    Input("csv-store", "data"),
    Input("price-mode", "value"),
    Input("layer-checklist", "value"),
    State("main-chart", "figure"),
)
def update_chart(store_data, price_mode, layers, cur_fig):
    """Render the chart from CSV data."""
    # Handle both dict (new) and plain string (legacy) store formats.
    if isinstance(store_data, dict):
        csv_path = store_data.get("csv")
        trades_csv_path = store_data.get("trades_csv")
    else:
        csv_path = store_data
        trades_csv_path = None

    if not csv_path or not os.path.exists(csv_path):
        return go.Figure().update_layout(
            title="No data loaded. Select a symbol and date, then click Load.",
            template="plotly_white",
        )

    df = pd.read_csv(csv_path)
    if df.empty:
        return go.Figure().update_layout(title="CSV is empty.")

    # Convert time_ms to datetime in ET for x-axis.
    df["time"] = (
        pd.to_datetime(df["time_ms"], unit="ms", utc=True)
        .dt.tz_convert("America/New_York")
        .dt.tz_localize(None)
    )

    # Downsample if too many points (>100k rows).
    if len(df) > 100_000:
        step = len(df) // 100_000 + 1
        df = df.iloc[::step].reset_index(drop=True)

    # ── Determine active panels ──────────────────────────────────────────
    # Each: (id, title, height_weight).  Price is always first.
    panels = [("price", "Price", 3)]
    if "premium" in layers:
        panels.append(("premium", "Premium (bps)", 1))
    if "vol" in layers:
        panels.append(("vol", "Volatility EMS", 1))
    if "momentum" in layers:
        panels.append(("momentum", "Momentum EMS", 1))
    if "trade_flow" in layers:
        panels.append(("trade_flow", "Trade Flow", 1))
    if "book_thick" in layers:
        panels.append(("book_thick", "Book Thickness", 1))

    n = len(panels)
    spacing = 0.04
    weights = [p[2] for p in panels]
    total_w = sum(weights)
    avail = 1.0 - spacing * max(0, n - 1)

    # Compute vertical domains (top-to-bottom).
    domains = []
    top = 1.0
    for w in weights:
        h = avail * w / total_w
        bot = top - h
        domains.append([round(max(0, bot), 4), round(top, 4)])
        top = bot - spacing

    def yref(i):
        """Trace-level yaxis reference: 'y', 'y2', 'y3', ..."""
        return "y" if i == 0 else f"y{i + 1}"

    def ykey(i):
        """Layout-level yaxis key: 'yaxis', 'yaxis2', ..."""
        return "yaxis" if i == 0 else f"yaxis{i + 1}"

    panel_idx = {pid: i for i, (pid, _, _) in enumerate(panels)}

    fig = go.Figure()
    t = df["time"]
    shapes = []
    annotations = []

    # Panel title annotations.
    for i, (_, title, _) in enumerate(panels):
        annotations.append(dict(
            text=f"<b>{title}</b>",
            xref="paper", yref="paper",
            x=0.0, y=domains[i][1],
            xanchor="left", yanchor="bottom",
            showarrow=False, font=dict(size=12),
        ))

    # ── Price panel (index 0) ────────────────────────────────────────────
    ya0 = yref(0)

    if "price" in layers:
        if price_mode == "bidask":
            fig.add_trace(go.Scattergl(
                x=t, y=df["local_bid"], name="Local Bid",
                line=dict(color="dodgerblue", width=1),
                yaxis=ya0, legendgroup="price",
            ))
            fig.add_trace(go.Scattergl(
                x=t, y=df["local_ask"], name="Local Ask",
                line=dict(color="tomato", width=1),
                yaxis=ya0, legendgroup="price",
            ))
        else:
            fig.add_trace(go.Scattergl(
                x=t, y=df["local_mid"], name="Local Mid",
                line=dict(color="dodgerblue", width=1),
                yaxis=ya0, legendgroup="price",
            ))

        fig.add_trace(go.Scattergl(
            x=t, y=df["remote_mid"], name="Remote Mid",
            line=dict(color="orange", width=1, dash="dot"),
            yaxis=ya0, legendgroup="price",
        ))

    # Compute tight y-range for price panel from price lines only (not depth
    # bands), so the price action fills the panel instead of being squished.
    price_ymin, price_ymax = None, None
    if "price" in layers:
        cols = ["remote_mid"]
        if price_mode == "bidask":
            cols += ["local_bid", "local_ask"]
        else:
            cols += ["local_mid"]
        vals = pd.concat([df[c] for c in cols], ignore_index=True).dropna()
        vals = vals[vals > 0]  # drop zeros (e.g. remote_mid before feed connects)
        if len(vals) > 0:
            price_ymin = vals.min()
            price_ymax = vals.max()
            pad = (price_ymax - price_ymin) * 0.15
            if pad == 0:
                pad = price_ymax * 0.001  # fallback for flat data
            price_ymin -= pad
            price_ymax += pad

    # ── Depth bands ──────────────────────────────────────────────────────
    if "depth" in layers and "price" in layers:
        mid = df["local_mid"]

        bid_100k = df["local_bid"] - df["liq_depth_buy_100k"] * mid
        ask_100k = df["local_ask"] + df["liq_depth_sell_100k"] * mid

        bid_250k = df["local_bid"] - df["liq_depth_buy_250k"] * mid
        ask_250k = df["local_ask"] + df["liq_depth_sell_250k"] * mid

        fig.add_trace(go.Scatter(
            x=t, y=ask_250k, name="Ask depth 250k",
            line=dict(width=0), showlegend=False, mode="lines",
            yaxis=ya0, legendgroup="depth",
        ))
        fig.add_trace(go.Scatter(
            x=t, y=bid_250k, name="Bid depth 250k",
            line=dict(width=0), showlegend=True, mode="lines",
            fill="tonexty", fillcolor="rgba(135,206,250,0.2)",
            yaxis=ya0, legendgroup="depth",
        ))

    # ── Trade markers ────────────────────────────────────────────────────
    if "trades" in layers and trades_csv_path and os.path.exists(trades_csv_path):
        try:
            tdf = pd.read_csv(trades_csv_path)
        except Exception:
            tdf = pd.DataFrame()
        if not tdf.empty and "time_ms" in tdf.columns:
            tdf["time"] = (
                pd.to_datetime(tdf["time_ms"], unit="ms", utc=True)
                .dt.tz_convert("America/New_York")
                .dt.tz_localize(None)
            )
            # Downsample if too many trades.
            if len(tdf) > 200_000:
                step = len(tdf) // 200_000 + 1
                tdf = tdf.iloc[::step].reset_index(drop=True)

            # Marker size proportional to notional, clamped 4-20px.
            tdf["notional"] = tdf["price"] * tdf["qty"]
            pctile_99 = tdf["notional"].quantile(0.99)
            if pctile_99 > 0:
                tdf["size"] = np.clip(tdf["notional"] / pctile_99 * 16 + 4, 4, 20)
            else:
                tdf["size"] = 6

            for side, color, symbol_marker in [
                ("buy", "green", "triangle-up"),
                ("sell", "red", "triangle-down"),
            ]:
                mask = tdf["side"] == side
                sdf = tdf[mask]
                if sdf.empty:
                    continue
                fig.add_trace(go.Scattergl(
                    x=sdf["time"], y=sdf["price"],
                    name=f"Trades ({side})",
                    mode="markers",
                    marker=dict(
                        color=color, symbol=symbol_marker,
                        size=sdf["size"], opacity=0.7,
                    ),
                    yaxis=ya0, legendgroup="trades",
                ))

    # ── Extra panels ─────────────────────────────────────────────────────
    if "premium" in layers:
        ya = yref(panel_idx["premium"])
        fig.add_trace(go.Scattergl(
            x=t, y=df["premium_bps"], name="Premium (bps)",
            line=dict(color="purple", width=1), yaxis=ya,
        ))
        shapes.append(dict(
            type="line", x0=0, x1=1, y0=0, y1=0,
            xref="paper", yref=ya,
            line=dict(color="gray", width=1, dash="dash"), opacity=0.5,
        ))

    if "vol" in layers:
        ya = yref(panel_idx["vol"])
        fig.add_trace(go.Scattergl(
            x=t, y=df["vol_ems_remote_4s"], name="Vol 4s",
            line=dict(color="green", width=1), yaxis=ya,
        ))
        fig.add_trace(go.Scattergl(
            x=t, y=df["vol_ems_remote_30s"], name="Vol 30s",
            line=dict(color="darkgreen", width=1), yaxis=ya,
        ))

    if "momentum" in layers:
        ya = yref(panel_idx["momentum"])
        fig.add_trace(go.Scattergl(
            x=t, y=df["momentum_1s"], name="Momentum 1s",
            line=dict(color="coral", width=1), yaxis=ya,
        ))
        fig.add_trace(go.Scattergl(
            x=t, y=df["momentum_4s"], name="Momentum 4s",
            line=dict(color="firebrick", width=1), yaxis=ya,
        ))
        shapes.append(dict(
            type="line", x0=0, x1=1, y0=0, y1=0,
            xref="paper", yref=ya,
            line=dict(color="gray", width=1, dash="dash"), opacity=0.5,
        ))

    if "trade_flow" in layers:
        ya = yref(panel_idx["trade_flow"])
        fig.add_trace(go.Scattergl(
            x=t, y=df["trade_rate_ems"], name="Trade Rate",
            line=dict(color="teal", width=1), yaxis=ya,
        ))
        fig.add_trace(go.Scattergl(
            x=t, y=df["trade_notional_signed_ems"], name="Trade Flow (signed)",
            line=dict(color="navy", width=1), yaxis=ya,
        ))
        shapes.append(dict(
            type="line", x0=0, x1=1, y0=0, y1=0,
            xref="paper", yref=ya,
            line=dict(color="gray", width=1, dash="dash"), opacity=0.5,
        ))

    if "book_thick" in layers:
        ya = yref(panel_idx["book_thick"])
        fig.add_trace(go.Scattergl(
            x=t, y=df["deep_bid_notional"], name="Deep Bid Notional",
            line=dict(color="steelblue", width=1), yaxis=ya,
        ))
        fig.add_trace(go.Scattergl(
            x=t, y=df["deep_ask_notional"], name="Deep Ask Notional",
            line=dict(color="indianred", width=1), yaxis=ya,
        ))
        fig.add_trace(go.Scattergl(
            x=t, y=df["local_bb_size"], name="BB Size",
            line=dict(color="steelblue", width=1, dash="dot"), yaxis=ya,
        ))
        fig.add_trace(go.Scattergl(
            x=t, y=df["local_ba_size"], name="BA Size",
            line=dict(color="indianred", width=1, dash="dot"), yaxis=ya,
        ))

    # ── Layout: manual subplot positioning ───────────────────────────────
    # All traces share xaxis="x" (the default).  Each panel gets its own
    # y-axis with a separate vertical domain.  This enables
    # hoversubplots="axis" to show a unified hover across all panels.
    layout_kw = dict(
        template="plotly_white",
        hovermode="x unified",
        hoversubplots="axis",
        dragmode="pan",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=60, r=20, t=40, b=30),
        shapes=shapes,
        annotations=annotations,
        xaxis=dict(
            anchor=yref(n - 1),  # attach x-axis to bottom panel
            rangeslider_visible=False,
            showspikes=True,
            spikemode="across",
            spikethickness=1,
            spikecolor="gray",
            spikedash="dot",
        ),
    )

    for i, (pid, title, _) in enumerate(panels):
        yax = dict(domain=domains[i], anchor="x")
        if pid == "price" and price_ymin is not None:
            yax["range"] = [price_ymin, price_ymax]
        layout_kw[ykey(i)] = yax

    fig.update_layout(**layout_kw)

    # Preserve x-axis zoom/pan when toggling layers or price mode, but NOT
    # when loading new data (csv-store change), since the old range would
    # be from a placeholder or different dataset.
    triggered = (dash.callback_context.triggered or [{}])[0].get("prop_id", "")
    if "csv-store" not in triggered and cur_fig and "layout" in cur_fig:
        prev_range = cur_fig["layout"].get("xaxis", {}).get("range")
        if prev_range:
            fig.update_layout(xaxis_range=prev_range)

    return fig


# ── URL routing ───────────────────────────────────────────────────────────────

@app.callback(
    Output("symbol-dropdown", "value"),
    Output("date-input", "value"),
    Output("price-mode", "value"),
    Output("layer-checklist", "value"),
    Output("load-btn", "n_clicks"),
    Input("url", "search"),
    State("load-btn", "n_clicks"),
    prevent_initial_call=False,
)
def url_to_controls(search, current_clicks):
    """Parse URL query params and set controls on page load."""
    if not search:
        raise dash.exceptions.PreventUpdate

    params = urllib.parse.parse_qs(search.lstrip("?"))

    sym = params.get("symbol", [None])[0]
    date = params.get("date", [None])[0]
    mode = params.get("mode", [None])[0]
    layers_str = params.get("layers", [None])[0]

    out_sym = sym if sym and sym in SYMBOLS else dash.no_update
    out_date = date if date and len(date) == 8 and date.isdigit() else dash.no_update
    out_mode = mode if mode in VALID_MODES else dash.no_update
    out_layers = dash.no_update
    if layers_str:
        parsed_layers = [l for l in layers_str.split(",") if l in VALID_LAYERS]
        if parsed_layers:
            out_layers = parsed_layers

    # Auto-trigger load if symbol+date are present.
    out_clicks = dash.no_update
    if sym and date and len(date) == 8 and date.isdigit():
        out_clicks = (current_clicks or 0) + 1

    return out_sym, out_date, out_mode, out_layers, out_clicks


# Clientside callback: Controls → URL (no page reload, no circular trigger).
app.clientside_callback(
    """
    function(symbol, date, mode, layers) {
        if (!symbol && !date) { return ""; }
        var params = [];
        if (symbol) params.push("symbol=" + encodeURIComponent(symbol));
        if (date) params.push("date=" + encodeURIComponent(date));
        if (mode) params.push("mode=" + encodeURIComponent(mode));
        if (layers && layers.length > 0) params.push("layers=" + layers.join(","));
        var qs = "?" + params.join("&");
        window.history.replaceState(null, "", qs);
        return "";
    }
    """,
    Output("url-dummy", "children"),
    Input("symbol-dropdown", "value"),
    Input("date-input", "value"),
    Input("price-mode", "value"),
    Input("layer-checklist", "value"),
)

# Help panel toggle (purely clientside, no server round-trip).
app.clientside_callback(
    """
    function(n_clicks) {
        var base = {"padding": "12px 16px", "background": "#f8f8f0",
                     "borderBottom": "1px solid #ddd"};
        if (n_clicks % 2 === 1) {
            base["display"] = "block";
        } else {
            base["display"] = "none";
        }
        return base;
    }
    """,
    Output("help-panel", "style"),
    Input("help-toggle-btn", "n_clicks"),
    prevent_initial_call=True,
)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VizData Dash App")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    print(f"Starting VizData on http://localhost:{args.port}")
    print(f"  vizdata binary: {VIZDATA_BIN}")
    print(f"  cache dir:      {CACHE_DIR}")
    print(f"  symbols:        {len(SYMBOLS)} loaded")

    app.run(host="0.0.0.0", port=args.port, debug=args.debug)
