#! /usr/bin/env python


"""

Given a symbol and a market:
 1 - download relevant data sources (Databento or Polygon for USEQ, Databento for CME, local for crypto)
 2 - Calculate returns based on a well-defined sampling interval (eg. 1min).
 3 - Regress returns against each other.


We'll use this to model how correlations behave over time and what we can use as
a reference price.

OK, so let's sketch out the flow of things.

Given a pair of symbols, let's download data and write returns for that period of time.

Then, join across the times and then regress them.

Data sources:
- USEQ symbols: Can use either Polygon (if use_polygon_useq=True) or Databento (default)
- CME symbols: Databento only
- Crypto symbols: Local Hyperliquid data


"""

import argparse
import getpass
import os
import sys
import shutil
import subprocess
import boto3
import tempfile

import json

##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)

##################################
from util.pathing import bindir

from tools import md_exists

from util.chron import dates_list, t_to_secs
from util import mktdata

# Use returner to get... returns.
returner_bin = os.path.join(bindir(), "returner")

import databento as db
from util import symbolizer
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

from sklearn.linear_model import LinearRegression

# Polygon imports
from polygon import RESTClient
import datetime as dt
import pytz

# Getting Databento credentials.
DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")
try:
    with open(DB_SECRET_FILE_PATH, "r") as f:
        secrets = json.load(f)
        DB_API_KEY = secrets["api_key"]
    #logger.info("Databento API key loaded successfully.")
except FileNotFoundError:
    #logger.error(f"Credentials file not found at {SECRET_FILE_PATH}. Please create it.")
    sys.exit(f"Credentials file not found at {DB_SECRET_FILE_PATH} . Please create it.")
except KeyError:
    #logger.error(f"API key not found within {SECRET_FILE_PATH}. Ensure it contains 'api_key'.")
    sys.exit(f"API key not found within {DB_SECRET_FILE_PATH} . Ensure it contains 'api_key'.")
except Exception as e:
    sys.exit(f"Error loading credentials: {e}")

# Getting Polygon credentials.
POLYGON_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.Polygon.creds.json")
POLYGON_API_KEY = None
try:
    with open(POLYGON_SECRET_FILE_PATH, "r") as f:
        secrets = json.load(f)
        POLYGON_API_KEY = secrets["api_key"]
except FileNotFoundError:
    print(f"Warning: Polygon credentials file not found at {POLYGON_SECRET_FILE_PATH}. Polygon features will be disabled.")
except KeyError:
    print(f"Warning: API key not found within {POLYGON_SECRET_FILE_PATH}. Polygon features will be disabled.")
except Exception as e:
    print(f"Warning: Error loading Polygon credentials: {e}. Polygon features will be disabled.")


class HistReturns:

    def __init__(self, syms_to_market, work_dir, start_d, end_d):

        self.syms_to_market = syms_to_market
        self.start_d = start_d
        self.end_d = end_d
        self.dates = dates_list(start_d, end_d)

        self.use_polygon_useq = True

        self.useq_syms = []
        self.cme_syms = []
        self.crypto_syms = []

        self.work_dir = work_dir

        # Give a default work_dir if needed.
        if work_dir:
            self.work_dir = work_dir
            if not os.path.exists(work_dir):
                os.mkdir(work_dir)
        else:
            hmdir = os.path.join("/home", os.getlogin(), "scratch/histreturns")
            if not os.path.exists(hmdir):
                os.mkdir(hmdir)
            self.work_dir = tempfile.TemporaryDirectory(dir=hmdir).name

        self.work_dir_prices = os.path.join(self.work_dir, "prices")
        if not os.path.exists(self.work_dir_prices):
            os.mkdir(self.work_dir_prices)


        for sym, mkt in syms_to_market.items():
            if mkt == "Hyperliquid":
                self.crypto_syms.append(sym)
            elif mkt == "CME":
                self.cme_syms.append(sym)
            elif mkt == "USEQ":
                self.useq_syms.append(sym)


        # Given a
        self.syms_to_returns = {}

        self.cache_dir = os.path.join(self.work_dir, "cache")
        if not os.path.exists(self.cache_dir):
            os.mkdir(self.cache_dir)

        # sym -> cache file.
        # As we download and contain these, do it.
        self.returns_caches = {}

        # For each symbol, define what its cache would be.
        for sym in self.syms_to_market:
            cache_path = os.path.join(self.cache_dir, "{}_{}_{}_returns.csv".format(sym, self.start_d, self.end_d))
            self.returns_caches[sym] = cache_path

        # Default sampling frequency for all markets (keep consistent to avoid mismatched regressions)
        self.sampling_freq = "1min"

    def _load_cache_if_valid(self, sym):
        cache_path = self.returns_caches[sym]
        if not os.path.exists(cache_path):
            return None

        cached_df = pd.read_csv(cache_path)
        expected_seconds = pd.Timedelta(self.sampling_freq).total_seconds()

        if 'time_diff' not in cached_df.columns:
            print(f"Cache {cache_path} missing time_diff column; regenerating")
            return None

        time_diffs = cached_df['time_diff'].dropna()
        if time_diffs.empty:
            print(f"Cache {cache_path} empty time_diff; regenerating")
            return None

        if not np.isclose(time_diffs.median(), expected_seconds, atol=1.0):
            print(f"Cache {cache_path} frequency mismatch; regenerating")
            return None

        return cached_df

    def get_returns_useq(self, sym):
        # Check if this exists already.
        returns_df = self._load_cache_if_valid(sym)
        if returns_df is not None:
            print("{} already exists".format(self.returns_caches[sym]))
            return returns_df

        # Check which data source to use
        if self.use_polygon_useq:
            returns_df = self._get_returns_useq_polygon(sym)
        else:
            returns_df = self._get_returns_useq_databento(sym)
        # Write to cache
        returns_df.to_csv(self.returns_caches[sym], index=False)
        return 

    def _finalize_returns_df(self, df, sym):
        """Resample to the configured frequency and compute returns without bridging large gaps."""

        df = df.sort_values('ts_event')
        df = df.set_index('ts_event')

        resampled = df[['mid_px']].resample(self.sampling_freq).last().dropna()
        resampled['symbol'] = sym

        resampled['time_diff'] = resampled.index.to_series().diff().dt.total_seconds()
        # Backward-looking returns: timestamp T has return from (T-1) to T
        resampled['returns'] = resampled['mid_px'].pct_change()

        # Gap mask: filter out returns that bridge large time gaps
        gap_mask = (resampled['time_diff'].isna()) | (resampled['time_diff'] >= 100)
        if gap_mask.any():
            resampled.loc[gap_mask, 'returns'] = np.nan

        resampled = resampled.dropna(subset=['returns']).reset_index()
        resampled['ts_s'] = resampled['ts_event'].apply(lambda x: int(x.timestamp()))

        return resampled[['ts_event', 'ts_s', 'mid_px', 'returns', 'symbol', 'time_diff']]

    def _get_returns_useq_databento(self, sym):
        """Fetch USEQ returns data from Databento."""
        print("Getting histdata from databento")
        # Otherwise, do the work.
        client = db.Historical(DB_API_KEY)

        start_d_format = "{}-{}-{}T00:00:00".format(self.start_d[:4], self.start_d[4:6], self.start_d[6:])
        end_d_format = "{}-{}-{}T00:00:00".format(self.end_d[:4], self.end_d[4:6], self.end_d[6:])

        px_data = client.timeseries.get_range(
            dataset="EQUS.MINI",
            symbols=[sym],
            stype_in="raw_symbol",
            schema="bbo-1m",
            start=start_d_format,
            end=end_d_format,
        )

        px_df = px_data.to_df()
        #print(px_df.head())

        px_df2 = px_df[['ts_event', 'bid_px_00', 'ask_px_00', 'symbol']].copy()

        # make sure ts_event is a datetime (with UTC) before computing gaps/returns
        px_df2['ts_event'] = pd.to_datetime(px_df2['ts_event'], utc=True)

        # NOTE: Databento "bbo-1m" provides the LAST BBO at each 1-minute interval
        # Timestamp 09:31:00 contains BBO from END of that minute (~09:31:59)
        # With backward-looking returns and close prices, this aligns with Polygon's timing

        # mid-price and per-event returns
        px_df2['mid_px'] = (px_df2['bid_px_00'] + px_df2['ask_px_00']) / 2
        px_df2 = self._finalize_returns_df(px_df2, sym)

        return px_df2

    def _get_returns_useq_polygon(self, sym):
        """Fetch USEQ returns data from Polygon."""
        if POLYGON_API_KEY is None:
            raise ValueError("Polygon API key not loaded. Cannot fetch data from Polygon.")

        print("Getting histdata from Polygon")
        client = RESTClient(POLYGON_API_KEY)

        # Parse dates
        start_date = dt.datetime.strptime(self.start_d, "%Y%m%d").date()
        end_date = dt.datetime.strptime(self.end_d, "%Y%m%d").date()

        # Convert to milliseconds for Polygon API
        et_tz = pytz.timezone("America/New_York")
        start_dt = et_tz.localize(dt.datetime.combine(start_date, dt.datetime.min.time()))
        end_dt = et_tz.localize(dt.datetime.combine(end_date, dt.datetime.min.time()))

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        # Fetch 1-minute aggregates from Polygon
        aggs = []
        for agg in client.list_aggs(
            ticker=sym,
            multiplier=1,
            timespan="minute",
            from_=start_ms,
            to=end_ms,
            limit=50000
        ):
            # Use close price (last trade in bar, ~end of minute)
            # This aligns with Databento's "last BBO" timing convention
            mid_px = agg.close

            aggs.append({
                "timestamp": agg.timestamp,
                "open": agg.open,
                "high": agg.high,
                "low": agg.low,
                "close": agg.close,
                "volume": agg.volume,
                "mid_px": mid_px,
            })

        if not aggs:
            raise ValueError(f"No data returned from Polygon for {sym}")

        print(f"Fetched {len(aggs)} aggregate bars from Polygon")

        # Convert to DataFrame
        px_df2 = pd.DataFrame(aggs)

        # Convert timestamp from milliseconds to datetime
        px_df2['ts_event'] = pd.to_datetime(px_df2['timestamp'], unit='ms', utc=True)
        # Add symbol column for consistency with Databento format
        px_df2['symbol'] = sym

        # Filter out large gaps only when computing returns so we retain the opening bar of each session
        px_df2 = self._finalize_returns_df(px_df2, sym)

        return px_df2




    def get_returns_cme(self, sym):
        # Check if this exists already.
        if os.path.exists(self.returns_caches[sym]):
            cached_df = self._load_cache_if_valid(sym)
            if cached_df is not None:
                print("{} already exists".format(self.returns_caches[sym]))
                return cached_df

        print("Getting histdata from databento")
        # Otherwise, do the work.
        client = db.Historical(DB_API_KEY)
        my_sym = symbolizer.ours_db_map[sym]

        start_d_format = "{}-{}-{}T00:00:00".format(self.start_d[:4], self.start_d[4:6], self.start_d[6:])
        end_d_format = "{}-{}-{}T00:00:00".format(self.end_d[:4], self.end_d[4:6], self.end_d[6:])

        px_data = client.timeseries.get_range(
            dataset="GLBX.MDP3",
            symbols=[my_sym],
            stype_in="continuous",
            schema="bbo-1m",
            start=start_d_format,
            end=end_d_format,
            # no limit means none.
            #limit=1000,
        )

        px_df = px_data.to_df()
        #print(px_df.head())

        px_df2 = px_df[['ts_event', 'bid_px_00', 'ask_px_00', 'symbol']].copy()

        # 2. make sure ts_event is a datetime (with UTC)
        px_df2['ts_event'] = pd.to_datetime(px_df2['ts_event'], utc=True)

        # NOTE: Databento "bbo-1m" provides the LAST BBO at each 1-minute interval
        # Timestamp 09:31:00 contains BBO from END of that minute (~09:31:59)
        # With backward-looking returns and close prices, this aligns with Polygon's timing

        # 4. add mid‐price
        px_df2['mid_px'] = (px_df2['bid_px_00'] + px_df2['ask_px_00']) / 2
        px_df2 = self._finalize_returns_df(px_df2, sym)

        px_df2.to_csv(self.returns_caches[sym], index=False)


        return px_df2

    # Check for market data.
    def get_returns_crypto(self, sym):
        if not os.path.exists(returner_bin):
            print(f"returner binary not found at {returner_bin} | please compile it first.")
            return

        # Check if exists.
        if os.path.exists(self.returns_caches[sym]):
            print("{} already exists".format(self.returns_caches[sym]))
            return pd.read_csv(self.returns_caches[sym])


        syms_books_do_dates = {(sym, "Hyperliquid") : self.dates}
        crypto_data_dir = "{}/tardis_datasets/gzpbf".format(os.getenv("HOME"))
        print("Crypto: checking for histdata")
        md_exists.check_for_data(syms_books_do_dates, crypto_data_dir, dl_automatically=True)

        # Now, let's get the returns. I'll scribe this out with the returner
        returner_cmds = []
        returner_dests = []
        for one_date in self.dates:
            returner_dest = os.path.join(self.work_dir_prices, "{}_{}_returns.csv".format(sym, one_date))
            if not os.path.exists(returner_dest):
                returner_cmd = "{} --symbol {} --date {} --out {}".format(returner_bin, sym, one_date, returner_dest)
                #print(returner_cmd)
                returner_cmds.append(returner_cmd)
            returner_dests.append(returner_dest)
        # Now, run these things.
        for one_cmd in returner_cmds:
            subprocess.run(one_cmd, shell=True)

        # Read all of these in a csv
        cols = ["ts_str","ts_s","mid_px","returns"]
        all_returns = pd.concat([pd.read_csv(one_dest, header=None, names=cols) for one_dest in returner_dests])

        # FIX: Shift crypto returns forward to align with Polygon/Databento timing
        # returner.cc samples at exact minute boundaries (09:30:00, 09:31:00)
        # Polygon close prices are at end of minute (~09:30:59, ~09:31:59)
        # Shifting forward aligns the return periods correctly
        all_returns['returns'] = all_returns['returns'].shift(-1)
        all_returns = all_returns.dropna(subset=['returns'])

        # Write the cache file.
        all_returns.to_csv(self.returns_caches[sym], index=False)
        return all_returns

    # Lot of conditions but:
    # I want to select between

# Matches up return timings between two symbols.
def get_matching_returns(histregs_obj, X_sym, y_sym, start_d, end_d, start_h, end_h, tz="ET"):
    """
    Matches returns between X_sym (independent) and y_sym (dependent) based on timestamps.

    Returns DataFrame with columns: t_1 (X_sym time), ret_1 (X_sym return),
                                     t_2 (y_sym time), ret_2 (y_sym return)
    """
    # Just get the times.
    regress_dates = dates_list(start_d, end_d)


    # Get the returns.
    X_sym_times_and_returns = []
    y_sym_times_and_returns = []


    # Get the returns.
    X_sym_df = pd.read_csv(histregs_obj.returns_caches[X_sym])
    y_sym_df = pd.read_csv(histregs_obj.returns_caches[y_sym])

    for one_date in regress_dates:
        datetime_start = "{} {} {}".format(one_date, start_h, tz)
        datetime_end = "{} {} {}".format(one_date, end_h, tz)

        start_s = t_to_secs(datetime_start)
        end_s = t_to_secs(datetime_end)


        # Let's get stuff for X_sym (independent variable)
        result_X = [
            (ts, ret)
            for ts, ret in zip(X_sym_df['ts_s'], X_sym_df['returns'])
            if start_s <= ts <= end_s
        ]

        result_y = [
            (ts, ret)
            for ts, ret in zip(y_sym_df['ts_s'], y_sym_df['returns'])
            if start_s <= ts <= end_s
        ]

        X_sym_times_and_returns.extend(result_X)
        y_sym_times_and_returns.extend(result_y)


    #13300
    #--------------------------------
    #19050

    # More lines in one side vs another I guess.


    # OK, at the end, we should have a list of pairs.
    # Now we have to do the pairwise inclusion-exclusion thing. Where we have to pair them up if they
    # are within x amount of each other.
    matched_pairs = []
    i, j = 0, 0
    n1, n2 = len(X_sym_times_and_returns), len(y_sym_times_and_returns)

    while i < n1 and j < n2:
        ts1, ret1 = X_sym_times_and_returns[i]
        ts2, ret2 = y_sym_times_and_returns[j]
        # compute difference in seconds
        diff = ts1 - ts2

        if abs(diff) <= 5:
            matched_pairs.append(((ts1, ret1), (ts2, ret2)))
            # advance the earlier one
            if ts1 < ts2:
                i += 1
            else:
                j += 1
        else:
            # no match: advance the earlier timestamp
            if ts1 < ts2:
                i += 1
            else:
                j += 1

    # ── 2) build a DataFrame ──
    # ret_1 = X_sym returns (independent), ret_2 = y_sym returns (dependent)
    df_pairs = pd.DataFrame([
        {"t_1": ts1, "ret_1": ret1, "t_2": ts2, "ret_2": ret2}
        for (ts1, ret1), (ts2, ret2) in matched_pairs
    ])

    return df_pairs


def regress_syms(histregs_obj, X_sym, y_sym, start_d, end_d, start_h, end_h, tz="ET", plot=False):
    """
    Performs linear regression: y_sym ~ X_sym

    Returns beta showing how much y_sym moves per 1% move in X_sym.

    Example:
        regress_syms(hr, "BTC", "MSTR", ...)
        -> Regression: MSTR ~ BTC
        -> Beta: how much MSTR moves per 1% BTC move

    Args:
        X_sym: Independent variable (X) - the factor/benchmark
        y_sym: Dependent variable (y) - the asset being explained
    """
    df_pairs = get_matching_returns(histregs_obj, X_sym, y_sym, start_d, end_d, start_h, end_h, tz)
    #print(df_pairs)
    # Just do a sanity check.
    nan_rows = df_pairs[df_pairs.isna().any(axis=1)]

    # Remove outliers (probably due to improper handling. Should not see many >1% moves in one window).
    threshold = 0.01

    # ── Filter out rows where abs(ret_1) > threshold OR abs(ret_2) > threshold ──
    # Create two boolean conditions
    cond_1 = df_pairs['ret_1'].abs() > threshold
    cond_2 = df_pairs['ret_2'].abs() > threshold
    # Combine conditions with OR (|) to find rows to be REMOVED (the outliers)
    outlier_mask = cond_1 | cond_2

    # Use the NOT operator (~) to select rows that are NOT outliers
    df_filtered = df_pairs[~outlier_mask].copy()

    # To view these. 
    removed_rows = df_pairs[outlier_mask]
    #print(f"Original DataFrame:\n{df_pairs}")
    print("\nRemoved {} Outlier Rows".format(len(removed_rows)))
    #print("\nRemoved Outlier Rows (abs > 0.10 in either column):\n", removed_rows)
    #print("\nFiltered DataFrame:\n", df_filtered)

    if len(nan_rows) > 0:
        print("NAN ROWS - please check your data")
        print(nan_rows)
        return 0

    # ── 3) linear regression of ret_2 vs ret_1 ──
    X = df_filtered[['ret_1']].values.reshape(-1, 1)  # feature matrix
    y = df_filtered['ret_2'].values                  # target vector


    model = LinearRegression()
    model.fit(X, y)

    if plot:
        plt.figure()             # ← start a brand-new figure
        sns.scatterplot(data=df_filtered, x='ret_1', y='ret_2')
        plt.show()               # ensure it renders immediately in a notebook


    print("Coefficient (β): {:.4f}".format(model.coef_[0]))
    #print("Intercept (α):", model.intercept_)
    print("R²:              {:.4}".format(model.score(X, y)))

    return {"beta": model.coef_[0], "r2": model.score(X, y)}


def score_model(coef, intercept, histregs_obj, X_sym, y_sym, start_d, end_d, start_h, end_h, tz="ET", plot=False):
    """Score a model with given coefficients on y_sym ~ X_sym regression."""
    df_pairs = get_matching_returns(histregs_obj, X_sym, y_sym, start_d, end_d, start_h, end_h, tz)

    X = df_pairs[['ret_1']].values.reshape(-1, 1)  # feature matrix
    y = df_pairs['ret_2'].values                  # target vector

    model = LinearRegression()
    model.intercept_      = intercept
    model.coef_           = np.array([coef])
    model.n_features_in_  = 1    # number of features in the model

    # — now you can get R² just like a normal model —
    r2 = model.score(X, y)
    print("R²:    {:.4f}".format(r2))


def update_regression_stats(csv_path, X_sym, y_sym, period, start_date, end_date, beta, r2):
    """
    Update or insert regression stats (y_sym ~ X_sym) into a CSV file.
    If a row with matching (X_sym, y_sym, period, start_date, end_date) exists, it updates that row.
    Otherwise, it appends a new row.

    Args:
        csv_path: Path to the CSV file
        X_sym: Independent variable symbol (e.g., "BTC")
        y_sym: Dependent variable symbol (e.g., "MSTR")
        period: Time period label (e.g., "morning", "day", "night")
        start_date: Start date string (e.g., "20250901")
        end_date: End date string (e.g., "20250922")
        beta: Regression coefficient (how much y_sym moves per 1% X_sym move)
        r2: R-squared value
    """
    # Read existing CSV or create new DataFrame
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path, dtype={'X_sym': str, 'y_sym': str, 'period': str, 'start_date': str, 'end_date': str})
    else:
        df = pd.DataFrame(columns=['X_sym', 'y_sym', 'period', 'start_date', 'end_date', 'beta', 'r2'])

    # Ensure comparison values are strings
    X_sym = str(X_sym)
    y_sym = str(y_sym)
    period = str(period)
    start_date = str(start_date)
    end_date = str(end_date)

    # Create mask to find existing row
    mask = (
        (df['X_sym'] == X_sym) &
        (df['y_sym'] == y_sym) &
        (df['period'] == period) &
        (df['start_date'] == start_date) &
        (df['end_date'] == end_date)
    )

    # Prepare new data
    new_data = {
        'X_sym': X_sym,
        'y_sym': y_sym,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'beta': beta,
        'r2': r2,
    }

    if mask.any():
        # Update existing row
        for col, val in new_data.items():
            df.loc[mask, col] = val
        print(f"Updated existing row: {y_sym} ~ {X_sym} ({period})")
    else:
        # Append new row
        new_row = pd.DataFrame([new_data])
        df = pd.concat([df, new_row], ignore_index=True)
        print(f"Added new row: {y_sym} ~ {X_sym} ({period})")

    # Save to CSV
    df.to_csv(csv_path, index=False)
    print(f"Saved to {csv_path}")



# OK, this gets us returns for databento.
def hist_stocks(scratch_dir, dates, sym):
    # Establish connection and authenticate
    client = db.Historical(DB_API_KEY)

    # Authenticated request
    #print(client.metadata.list_datasets())
    #my_sym = symbolizer.ours_db_map[sym]

    # Did the symbology need to be right? Ugh.

    # Symbolizer doesnt need to be done for cash equities.
    my_sym = sym

    data = client.timeseries.get_range(
        dataset="EQUS.MINI",
        symbols=[my_sym],
        stype_in="raw_symbol",
        schema="bbo-1m",
        start="2025-04-04T15:00:00",
        end="2025-04-04T22:10:00",
        limit=1000,
    )

    df = data.to_df()

    df2 = df[['ts_event', 'bid_px_00', 'ask_px_00', 'symbol']].copy()

    # 2. make sure ts_event is a datetime (with UTC)
    df2['ts_event'] = pd.to_datetime(df2['ts_event'], utc=True)

    # 3. add seconds‐since‐epoch
    #    (pandas stores datetimes as nanoseconds since epoch internally)
    df2['ts_epoch'] = df2['ts_event'].astype('int64') // 10**9

    # 4. add mid‐price
    df2['mid_px'] = (df2['bid_px_00'] + df2['ask_px_00']) / 2
    df2['returns'] = df2['mid_px'].pct_change()
    df2 = df2.iloc[1:].reset_index(drop=True)

    print(df2)

    pass



# OK, this gets us returns for databento.
def hist_cme(scratch_dir, dates, sym):
    # Establish connection and authenticate
    client = db.Historical(DB_API_KEY)

    # Authenticated request
    #print(client.metadata.list_datasets())
    my_sym = symbolizer.ours_db_map[sym]

    # Did the symbology need to be right? Ugh.

    # Let's get the symbology right.
    result = client.symbology.resolve(
        dataset="GLBX.MDP3",
        symbols=["ES.v.0"],
        stype_in="continuous",
        stype_out="instrument_id",
        start_date="2025-03-04",
        end_date="2025-04-24",
    )
    #print(type(result))

    print("----------------------------------------------")
    # Well this is a little silly. Do I have to do the translation again??
    second_result = client.symbology.resolve(
        dataset="GLBX.MDP3",
        symbols=["5002"],
        stype_in="instrument_id",
        stype_out="raw_symbol",
        start_date="2025-03-04",
        end_date="2025-04-24",
    )
    # And the second_result is the thing that you use. OK.

    data = client.timeseries.get_range(
        dataset="GLBX.MDP3",
        symbols=[my_sym],
        stype_in="continuous",
        schema="bbo-1m",
        start="2025-04-04T00:00:00",
        end="2025-04-04T00:10:00",
        limit=1000,
    )

    df = data.to_df()

    df2 = df[['ts_event', 'bid_px_00', 'ask_px_00', 'symbol']].copy()

    # 2. make sure ts_event is a datetime (with UTC)
    df2['ts_event'] = pd.to_datetime(df2['ts_event'], utc=True)

    # 3. add seconds‐since‐epoch
    #    (pandas stores datetimes as nanoseconds since epoch internally)
    df2['ts_epoch'] = df2['ts_event'].astype('int64') // 10**9

    # 4. add mid‐price
    df2['mid_px'] = (df2['bid_px_00'] + df2['ask_px_00']) / 2
    df2['returns'] = df2['mid_px'].pct_change()
    df2 = df2.iloc[1:].reset_index(drop=True)

    #print(df2)

    pass

def main():
    parser = argparse.ArgumentParser()

    pass

    start_d = "20250420"
    # Not inclusive.
    end_d = "20250422"
    dates = dates_list(start_d, end_d)

    tmp_dir = os.path.join("/home/{}/scratch/histreturns/".format(getpass.getuser()))
    syms_to_market = {"ES" : "CME", "BTC" : "Hyperliquid"}

    return

    hr = HistReturns(syms_to_market, tmp_dir, start_d, end_d)
    # Can we get the cme returns?
    #hr.get_returns_cme("ES")

    #hr.get_returns_crypto("BTC")

    hr.regress_syms("ES", "BTC", "20250420", "20250421", "09:00", "15:59")


    #hist_cme(tmp_dir, dates, "ES")




if __name__ == "__main__":
    main()
