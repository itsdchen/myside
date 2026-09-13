#!/usr/bin/env python3
"""
feedquality_analyze: aggregate feedquality binary outputs across days and
compute derived metrics not produced by the binary itself.

Loads <prefix>_<date>_{grid,spikes,recall,summary}.csv across all dates
matching the prefix. Reports:

  - Per-feed totals (precision / recall / F1) summed across days
  - Uptime: % of grid samples where each feed was fresh
  - Lead/lag cross-correlation peak per feed
  - Predictive R^2 of HL returns from each ext feed (single + joint regression)
  - Ext-spike magnitude percentile distribution
  - HL-spike magnitude percentile distribution
  - Recall broken out by HL-spike size bucket

Usage:
    feedquality_analyze.py --prefix /tmp/feedquality_silver
    feedquality_analyze.py --prefix /tmp/feedquality_silver --max-lag-steps 10
    feedquality_analyze.py --prefix /tmp/feedquality_silver --out report.txt
"""

import argparse
import glob
import re
import sys

import numpy as np
import pandas as pd
from numpy.linalg import lstsq, LinAlgError


def load_all(prefix):
    # Match exactly <prefix>_<8 digits>_grid.csv — anything in between (e.g.
    # _fine_) belongs to a different prefix and would otherwise double-count
    # the same date.
    grid_paths = sorted(glob.glob(f"{prefix}_" + "[0-9]" * 8 + "_grid.csv"))
    if not grid_paths:
        sys.exit(f"No files match {prefix}_<YYYYMMDD>_grid.csv")
    dates = []
    for p in grid_paths:
        m = re.search(r"_(\d{8})_grid\.csv$", p)
        if m:
            dates.append(m.group(1))
    dates = sorted(set(dates))

    grids, spikes_, recalls, summaries = [], [], [], []
    for d in dates:
        g = pd.read_csv(f"{prefix}_{d}_grid.csv")
        g["date"] = d
        grids.append(g)
        for kind, holder in [
            ("spikes", spikes_),
            ("recall", recalls),
            ("summary", summaries),
        ]:
            try:
                df = pd.read_csv(f"{prefix}_{d}_{kind}.csv")
                if not df.empty:
                    df["date"] = d
                    holder.append(df)
            except (FileNotFoundError, pd.errors.EmptyDataError):
                pass

    return {
        "dates": dates,
        "grid": pd.concat(grids, ignore_index=True) if grids else pd.DataFrame(),
        "spikes": pd.concat(spikes_, ignore_index=True) if spikes_ else pd.DataFrame(),
        "recall": pd.concat(recalls, ignore_index=True) if recalls else pd.DataFrame(),
        "summary": pd.concat(summaries, ignore_index=True) if summaries else pd.DataFrame(),
    }


def get_ext_labels(grid):
    return [c[:-4] for c in grid.columns if c.endswith("_mid") and c != "hl_mid"]


def compute_returns(grid, ext_labels, staleness_ms=60_000):
    g = grid.sort_values(["date", "time_ms"]).reset_index(drop=True)
    g["hl_ret_bps"] = g["hl_mid"].pct_change() * 1e4
    g["hl_fresh"] = g["hl_age_ms"] < staleness_ms
    for label in ext_labels:
        g[f"{label}_ret_bps"] = g[f"{label}_mid"].pct_change() * 1e4
        g[f"{label}_fresh"] = g[f"{label}_age_ms"].between(0, staleness_ms)
    # pct_change yields +/-inf when the prior value is zero — happens at the
    # first valid tick after the binary's pre-init zero rows. Coerce to NaN so
    # downstream notna()/isfinite() filters handle them uniformly.
    ret_cols = ["hl_ret_bps"] + [f"{l}_ret_bps" for l in ext_labels]
    g[ret_cols] = g[ret_cols].replace([np.inf, -np.inf], np.nan)
    # Don't compare last sample of day N to first sample of day N+1.
    boundaries = g["date"].ne(g["date"].shift())
    g.loc[boundaries, ret_cols] = np.nan
    return g


def crosscorr(g, ext_labels, max_lag_steps):
    """For each ext, correlate ext_ret(t) with hl_ret(t+lag) over -L..+L. A
    positive peak lag means ext leads HL by that many grid steps.

    Important: shift on the full-grid HL series first, then mask to fresh +
    same-date rows. Shifting after filtering would make "+1 step" mean "next
    fresh row" and would bridge day boundaries."""
    rows = []
    same_date = g["date"].eq(g["date"].shift(0))  # always true; placeholder
    for label in ext_labels:
        if f"{label}_ret_bps" not in g.columns:
            continue
        for lag in range(-max_lag_steps, max_lag_steps + 1):
            # Shift HL on the FULL grid so lag is in real grid steps. Also
            # require the shifted HL came from the same date as the ext sample
            # (don't bridge day boundaries).
            shifted_hl = g["hl_ret_bps"].shift(-lag)
            shifted_date = g["date"].shift(-lag)
            same_date = g["date"].eq(shifted_date)
            ok = (g["hl_fresh"] & g[f"{label}_fresh"] & same_date
                  & shifted_hl.notna() & g[f"{label}_ret_bps"].notna())
            if ok.sum() < 50:
                continue
            c = np.corrcoef(g.loc[ok, f"{label}_ret_bps"],
                            shifted_hl[ok])[0, 1]
            rows.append({"source": label, "lag_steps": lag, "corr": c})
    if not rows:
        return pd.DataFrame(columns=["source", "lag_steps", "corr"])
    return pd.DataFrame(rows)


def regress(g, ext_labels, lag, only=None):
    """OLS: hl_ret(t+lag) ~ const + ext_rets(t). Returns coefs, R^2, n.

    Same shift-then-mask discipline as crosscorr."""
    cols = [l for l in ext_labels if (only is None or l == only)]
    if not cols:
        return None
    shifted_hl = g["hl_ret_bps"].shift(-lag)
    shifted_date = g["date"].shift(-lag)
    same_date = g["date"].eq(shifted_date)
    mask = g["hl_fresh"] & same_date
    for l in cols:
        if f"{l}_ret_bps" not in g.columns:
            return None
        mask = mask & g[f"{l}_fresh"]
    y = shifted_hl
    X = g[[f"{l}_ret_bps" for l in cols]]
    ok = mask & y.notna() & X.notna().all(axis=1)
    if ok.sum() < 100:
        return None
    yv = y[ok].values
    Xv = X[ok].values
    Xa = np.column_stack([np.ones(len(Xv)), Xv])
    try:
        coefs, *_ = lstsq(Xa, yv, rcond=None)
    except LinAlgError:
        return None
    yhat = Xa @ coefs
    ss_res = np.sum((yv - yhat) ** 2)
    ss_tot = np.sum((yv - yv.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return {"intercept": coefs[0],
            "coefs": dict(zip([f"{l}_ret_bps" for l in cols], coefs[1:])),
            "r2": r2, "n": int(ok.sum())}


def fmt_table(df, indent="  "):
    s = df.to_string()
    return "\n".join(indent + line for line in s.split("\n"))


def write_section(out, title):
    out.write(f"\n=== {title} ===\n")


def report(prefix, max_lag_steps, out):
    data = load_all(prefix)
    grid = data["grid"]
    spikes = data["spikes"]
    recall = data["recall"]
    summary = data["summary"]
    ext_labels = get_ext_labels(grid)

    write_section(out, "Configuration")
    out.write(f"prefix:    {prefix}\n")
    out.write(f"dates:     {len(data['dates'])} ({data['dates'][0]} .. "
              f"{data['dates'][-1]})\n")
    out.write(f"externals: {ext_labels}\n")
    if not summary.empty:
        hl_label = summary[summary["is_hl"] == 1]["source"].iloc[0]
        out.write(f"hl_label:  {hl_label}\n")
    out.write(f"grid step: {grid['time_ms'].diff().median():.0f} ms (median)\n")

    # --- Totals from summary CSVs.
    if not summary.empty:
        write_section(out, f"Per-feed totals (summed over {len(data['dates'])} days)")
        ext = summary[summary["is_hl"] == 0].groupby("source").agg(
            ticks=("tick_count", "sum"),
            ext_spikes=("ext_spike_events", "sum"),
            hl_followed=("hl_followed", "sum"),
            spike_reverted=("spike_reverted", "sum"),
            basis_shift=("basis_shift", "sum"),
            led_hl=("led_hl", "sum"),
            missed_hl=("missed_hl", "sum"),
        )
        ext["precision_pct"] = 100.0 * ext["hl_followed"] / ext["ext_spikes"].replace(0, np.nan)
        hl_spike_total = ext["led_hl"] + ext["missed_hl"]
        ext["recall_pct"] = 100.0 * ext["led_hl"] / hl_spike_total.replace(0, np.nan)
        p, r = ext["precision_pct"], ext["recall_pct"]
        ext["f1_pct"] = 2 * p * r / (p + r).replace(0, np.nan)
        out.write(fmt_table(ext.round(2)) + "\n")

        hl_rows = summary[summary["is_hl"] == 1]
        out.write(f"\n  HL ticks total:     {int(hl_rows['tick_count'].sum())}\n")
        out.write(f"  HL spike events:    {int(hl_rows['hl_spike_events_total'].sum())}\n")

    # --- Uptime per ext feed.
    write_section(out, "Uptime (% of grid samples where feed was fresh, <60s old)")
    for label in ext_labels:
        col = f"{label}_age_ms"
        fresh = (grid[col] >= 0) & (grid[col] < 60_000)
        out.write(f"  {label}: {fresh.mean() * 100:.2f}%  "
                  f"(median age {grid[col].median():.0f} ms)\n")

    # --- Lead/lag cross-correlation.
    g = compute_returns(grid, ext_labels)
    grid_ms = float(grid["time_ms"].diff().median())
    write_section(out, f"Lead/lag cross-correlation "
                  f"(steps of {grid_ms:.0f} ms)")
    cc = crosscorr(g, ext_labels, max_lag_steps)
    if cc.empty:
        out.write("  (no fresh samples available for any feed)\n")
        cc_groups = []
    else:
        cc_groups = list(cc.groupby("source"))
    for label, df in cc_groups:
        df = df.sort_values("lag_steps")
        out.write(f"  {label}:\n")
        valid = df["corr"].notna()
        if not valid.any():
            out.write("    (no valid samples — feed may be too sparse)\n")
            continue
        best = df.loc[df.loc[valid, "corr"].abs().idxmax()]
        for _, row in df.iterrows():
            if pd.isna(row["corr"]):
                continue
            marker = "  <-- peak" if row["lag_steps"] == best["lag_steps"] else ""
            out.write(f"    lag {int(row['lag_steps']):+3d} steps "
                      f"({int(row['lag_steps']) * grid_ms:+6.0f} ms): "
                      f"corr = {row['corr']:+.4f}{marker}\n")
        lag_ms = best["lag_steps"] * grid_ms
        interp = ("ext leads HL" if lag_ms > 0
                  else "HL leads ext" if lag_ms < 0 else "synchronous")
        out.write(f"    => peak at {lag_ms:+.0f} ms ({interp}, "
                  f"corr={best['corr']:+.4f})\n")

    # --- Predictive R^2 at lag 0.
    write_section(out, "Predictive R^2 of HL returns at lag 0")
    out.write("  (single-feed regressions: HL_ret = const + beta * ext_ret)\n")
    for label in ext_labels:
        res = regress(g, ext_labels, 0, only=label)
        if res:
            beta = list(res["coefs"].values())[0]
            out.write(f"    {label}: R^2={res['r2']:.4f}  beta={beta:+.3f}  "
                      f"(n={res['n']:,})\n")
    res = regress(g, ext_labels, 0)
    if res:
        out.write("  joint regression (all feeds together):\n")
        out.write(f"    R^2 = {res['r2']:.4f}  (n={res['n']:,})\n")
        for k, v in res["coefs"].items():
            out.write(f"    {k}: beta={v:+.3f}\n")

    # --- Predictive R^2 at +1 step (next-period prediction).
    res1 = regress(g, ext_labels, 1)
    if res1:
        out.write(f"  joint at lag +1 step "
                  f"({grid_ms:.0f}ms ahead): R^2 = {res1['r2']:.4f}\n")

    # --- Ext-spike magnitude distribution.
    if not spikes.empty:
        write_section(out, "Ext-spike magnitude (|r0_bps|) percentiles")
        for label, df in spikes.groupby("source"):
            mags = df["r0_bps"].abs()
            q = mags.quantile([0.5, 0.75, 0.9, 0.99]).to_dict()
            out.write(f"  {label} (n={len(df):,}): median={q[0.5]:.2f}  "
                      f"p75={q[0.75]:.2f}  p90={q[0.9]:.2f}  "
                      f"p99={q[0.99]:.2f}  max={mags.max():.2f}\n")

    # --- HL-spike magnitude distribution.
    if not recall.empty:
        write_section(out, "HL-spike magnitude (|hl_change_bps|) percentiles")
        # Each HL spike emits one recall row per ext; dedup by (date, start_ms).
        hl_only = recall.drop_duplicates(["date", "start_ms"])
        mags = hl_only["hl_change_bps"].abs()
        q = mags.quantile([0.5, 0.75, 0.9, 0.99]).to_dict()
        out.write(f"  total HL spikes: {len(hl_only):,}\n")
        out.write(f"  median={q[0.5]:.2f}  p75={q[0.75]:.2f}  "
                  f"p90={q[0.9]:.2f}  p99={q[0.99]:.2f}  max={mags.max():.2f}\n")

    # --- Recall conditional on HL-spike size.
    if not recall.empty:
        write_section(out, "Recall by HL-spike size bucket")
        out.write("  (rows: source × HL-spike-size; led_pct = % of HL spikes "
                  "that ext predicted)\n")
        rec = recall.copy()
        rec["abs_hl_change"] = rec["hl_change_bps"].abs()
        bins = [0, 5, 10, 20, 50, np.inf]
        labels_b = ["<5", "5-10", "10-20", "20-50", "50+"]
        rec["mag_bin"] = pd.cut(rec["abs_hl_change"], bins=bins,
                                 labels=labels_b, right=False)
        rec["led"] = (rec["classification"] == "EXT_LED").astype(int)
        tab = rec.groupby(["source", "mag_bin"], observed=True)["led"].agg(
            n="count", led_pct=lambda x: round(100 * x.mean(), 1))
        out.write(fmt_table(tab) + "\n")

    write_section(out, "End")


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prefix", required=True,
                   help="Glob prefix; loads <prefix>_<date>_*.csv")
    p.add_argument("--max-lag-steps", type=int, default=5,
                   help="Cross-correlation lag range (default: +/-5 grid steps)")
    p.add_argument("--out", help="Write report to file (default: stdout)")
    args = p.parse_args()

    if args.out:
        with open(args.out, "w") as f:
            report(args.prefix, args.max_lag_steps, f)
        print(f"Wrote report to {args.out}")
    else:
        report(args.prefix, args.max_lag_steps, sys.stdout)


if __name__ == "__main__":
    main()
