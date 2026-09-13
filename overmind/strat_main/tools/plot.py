#!/usr/bin/env python3

import argparse
import math
import sys
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import ScalarFormatter

# Custom formatter to force a specific scientific notation exponent
class FixedOrderFormatter(ScalarFormatter):
    def __init__(self, order_of_mag, useMathText=True):
        super().__init__(useMathText=useMathText)
        self.force_order = order_of_mag

    def _set_order_of_magnitude(self):
        # Override the automatic detection
        self.orderOfMagnitude = self.force_order

def plot(in_file, out_file):
    # 1. Read data from file (default is stdin)
    # We define headers manually since the input likely doesn't have them based on your description.
    headers = ['Symbol', 'Time', 'Position', 'Volume', 'ClosedPnl', 'TotalPnl']
    
    try:
        df = pd.read_csv(in_file, header=None, names=headers)
    except Exception as e:
        sys.exit(f"Error reading input csv {in_file}: {e}")

    # 2. Parse the 'Time' and 'Symbol' columns
    # The format is '20260102 09:31:14.207411834 EST'. 
    # Pandas handles nanoseconds well, but standard parsing might choke on "EST".
    # We strip " EST" and parse the specific format for speed and reliability.
    try:
        date = df['Time'].iloc[-1][:8]
        # Remove timezone suffix to simplify parsing (assuming local relative time is sufficient)
        df['Time'] = df['Time'].astype(str).str.replace(r' [A-Z]{3}$', '', regex=True)
        # Parse: YYYYMMDD HH:MM:SS.nanoseconds
        df['Time'] = pd.to_datetime(df['Time'], format='%Y%m%d %H:%M:%S.%f')
    except IndexError:
        print("Empty input, nothing to plot")
        return
    except Exception as e:
        sys.exit(f"Error parsing timestamps: {e}")
    try:
        sym = df['Symbol'][0]
        if not all(x == sym for x in df['Symbol']):
            raise ValueError("Multiple symbols given")
    except Exception as e:
        sys.exit(f"Error parsing symbols: {e}")

    # 3. Setup the Plot with adequate left margin for the extra axis
    fig, ax_vol = plt.subplots(figsize=(14, 8))
    fig.subplots_adjust(left=0.15, right=0.9)  # Increase left margin for Position axis
    fig.suptitle(f"{sym} {date}", fontsize=16)

    # 3a. Determine the "Master" Order of Magnitude
    max_vol = df['Volume'].max()
    max_pos = df['Position'].abs().max()
    global_max = min(max_vol, max_pos)
    if global_max > 0:
        target_order = math.floor(math.log10(global_max))
    else:
        target_order = 0

    # --- LAYER 1: Volume (Left Axis 1 - Bottom) ---
    color_vol = 'silver'
    ax_vol.fill_between(df['Time'], 0, df['Volume'], color=color_vol, alpha=0.4, label='Volume')

    ax_vol.set_xlabel('Time')
    ax_vol.set_ylabel('Volume ($)', color='gray', fontweight='bold')
    ax_vol.tick_params(axis='y', labelcolor='gray')

    # Format X-axis
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    plt.xticks(rotation=45)

    # Force Volume to use the target order of magnitude
    fmt_vol = FixedOrderFormatter(target_order, useMathText=True)
    fmt_vol.set_scientific(True)
    fmt_vol.set_powerlimits((0, 0))
    ax_vol.yaxis.set_major_formatter(fmt_vol)

    # --- LAYER 2: Position (Left Axis 2 - Middle) ---
    # Create a twin axis (defaults to right), then move it to the left
    ax_pos = ax_vol.twinx()
    ax_pos.spines['left'].set_position(('outward', 60)) # Offset spine to the left by 60 points
    ax_pos.yaxis.set_ticks_position('left')
    ax_pos.yaxis.set_label_position('left')

    # Center the Position axis around 0
    pos_max = df['Position'].abs().max()
    limit = pos_max * 1.1 if pos_max != 0 else 1
    ax_pos.set_ylim(-limit, limit)

    # color_pos = '#555555' # Dark Gray
    color_pos = '#008000' # Green
    ax_pos.fill_between(df['Time'], 0, df['Position'], color=color_pos, alpha=0.3, label='Position')

    ax_pos.set_ylabel('Position ($)', color=color_pos, fontweight='bold')
    ax_pos.tick_params(axis='y', labelcolor=color_pos)

    # Remove the 'right' spine for this axis so it doesn't draw a line on the PnL side
    ax_pos.spines['right'].set_visible(False)

    # Force Position to use the SAME target order of magnitude
    fmt_pos = FixedOrderFormatter(target_order, useMathText=True)
    fmt_pos.set_scientific(True)
    fmt_pos.set_powerlimits((0, 0))
    ax_pos.yaxis.set_major_formatter(fmt_pos)

    # --- LAYER 3: PnL (Right Axis - Top) ---
    ax_pnl = ax_vol.twinx()

    line_closed = ax_pnl.plot(df['Time'], df['ClosedPnl'], color='tab:orange', linewidth=2, label='Closed PnL')
    line_total = ax_pnl.plot(df['Time'], df['TotalPnl'], color='tab:blue', linewidth=2, label='Total PnL')

    # Center the PnL axis around 0
    pnl_max = max(df['ClosedPnl'].abs().max(), df['TotalPnl'].abs().max())
    limit = pnl_max * 1.1 if pnl_max > 0 else 1
    ax_pnl.set_ylim(-limit, limit)

    ax_pnl.set_ylabel('PnL ($)', color='black', fontweight='bold')
    ax_pnl.tick_params(axis='y', labelcolor='black')

    # Ensure PnL axis background is transparent
    ax_pnl.set_zorder(10)
    ax_pnl.patch.set_visible(False)

    # 4. Unified Legend
    # We collect handles from all 3 axes
    import matplotlib.patches as mpatches
    patch_vol = mpatches.Patch(color=color_vol, alpha=0.4, label='Volume')
    patch_pos = mpatches.Patch(color=color_pos, alpha=0.3, label='Position')

    lines = [patch_pos, patch_vol] + line_closed + line_total
    labels = [l.get_label() for l in lines]

    # Place legend (using ax_vol to anchor it)
    ax_vol.legend(lines, labels, loc='upper left', framealpha=0.9)

    # 5. If out_file is given, save to file. Otherwise plot it.
    if out_file:
        plt.savefig(out_file)
        print(f"Plot saved to {out_file}")
    else:
        plt.show()

def main():
    parser = argparse.ArgumentParser(epilog="""
example usage:
    cat trades_20260102.csv | pnl.py --time | grep GOOGL | plot.py
""")
    parser.add_argument("--in-file", default=sys.stdin)
    parser.add_argument("--out-file")
    args = parser.parse_args()

    plot(args.in_file, args.out_file)

if __name__ == "__main__":
    main()
