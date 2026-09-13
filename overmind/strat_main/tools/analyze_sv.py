#!/usr/bin/env python3
"""
Analyze simulation results to understand how different parameter values
compare when other parameters are held constant.

Sample command: 
~/tradefi/retraded/pybin/analyze_sv.py --metric sharpe --top-n-percent 10  nvda_usd/results.txt

"""

import argparse
import pandas as pd
import numpy as np
from collections import defaultdict
from itertools import combinations
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description='Analyze simulation results to compare parameter impacts'
    )
    parser.add_argument(
        'results_file',
        help='Path to results.txt file (CSV format)'
    )
    parser.add_argument(
        '--metric',
        default='sim_score',
        help='Metric to optimize for (default: sim_score)'
    )
    parser.add_argument(
        '--top-n-percent',
        type=float,
        default=10.0,
        help='Top N percent for frequency analysis (default: 10.0)'
    )
    return parser.parse_args()


def load_results(filepath):
    """Load results CSV and identify parameter vs metric columns."""
    df = pd.read_csv(filepath)

    # Known metric columns (everything after the parameter columns)
    metric_cols = ['avg_pnl', 'avg_closed_pnl', 'avg_comm', 'sharpe', 'num_trds', 'sim_score']

    # Parameter columns are everything except 'variant' and metric columns
    param_cols = [col for col in df.columns
                  if col not in ['variant'] + metric_cols]

    return df, param_cols, metric_cols


def find_matched_pairs(df, param_cols, target_param, value1, value2):
    """
    Find rows where target_param differs (value1 vs value2) but all
    other parameters are held constant.

    Returns: list of tuples (row_idx_with_value1, row_idx_with_value2)
    """
    # Get rows with each value
    df_v1 = df[df[target_param] == value1]
    df_v2 = df[df[target_param] == value2]

    # Other parameters to match on
    other_params = [p for p in param_cols if p != target_param]

    # Find matches
    matches = []
    for idx1, row1 in df_v1.iterrows():
        # Look for matching row in df_v2
        mask = pd.Series([True] * len(df_v2), index=df_v2.index)
        for param in other_params:
            mask &= (df_v2[param] == row1[param])

        matching_rows = df_v2[mask]
        if len(matching_rows) > 0:
            # Take first match (should only be one in a proper grid search)
            idx2 = matching_rows.index[0]
            matches.append((idx1, idx2))

    return matches


def analyze_parameter_comparison(df, param_cols, param_name, value1, value2, metric):
    """
    Analyze comparison between two values of a parameter.
    Returns statistics dict.
    """
    matches = find_matched_pairs(df, param_cols, param_name, value1, value2)

    if len(matches) == 0:
        return None

    # Extract metric values for each match
    differences = []
    wins = 0
    losses = 0
    ties = 0

    for idx1, idx2 in matches:
        val1 = df.loc[idx1, metric]
        val2 = df.loc[idx2, metric]
        diff = val1 - val2  # positive means value1 is better
        differences.append(diff)

        if diff > 0:
            wins += 1
        elif diff < 0:
            losses += 1
        else:
            ties += 1

    differences = np.array(differences)
    total = len(matches)

    return {
        'n_comparisons': total,
        'win_rate': 100 * wins / total if total > 0 else 0,
        'loss_rate': 100 * losses / total if total > 0 else 0,
        'win_count': wins,
        'loss_count': losses,
        'tie_rate': 100 * ties / total if total > 0 else 0,
        'avg_diff': np.mean(differences),
        'median_diff': np.median(differences),
        'std_diff': np.std(differences),
        'min_diff': np.min(differences),
        'max_diff': np.max(differences),
    }


def calculate_value_ranks(df, param_cols, param_name, metric):
    """
    Calculate average rank and top-N frequency for each value of a parameter.
    """
    # Get unique values for this parameter
    unique_vals = sorted(df[param_name].unique())

    # Rank all rows by metric (higher is better)
    df['_rank'] = df[metric].rank(ascending=False, method='min')

    value_stats = {}
    for val in unique_vals:
        rows = df[df[param_name] == val]
        value_stats[val] = {
            'avg_rank': rows['_rank'].mean(),
            'median_rank': rows['_rank'].median(),
            'count': len(rows)
        }

    df.drop('_rank', axis=1, inplace=True)
    return value_stats


def calculate_top_n_frequency(df, param_cols, param_name, metric, top_n_percent):
    """Calculate how often each parameter value appears in top N%."""
    n_top = max(1, int(len(df) * top_n_percent / 100))

    # Get top N rows
    top_rows = df.nlargest(n_top, metric)

    # Count frequency of each value
    unique_vals = sorted(df[param_name].unique())
    frequencies = {}

    for val in unique_vals:
        count_in_top = len(top_rows[top_rows[param_name] == val])
        total_count = len(df[df[param_name] == val])
        frequencies[val] = {
            'count_in_top_n': count_in_top,
            'total_count': total_count,
            'frequency_pct': 100 * count_in_top / total_count if total_count > 0 else 0
        }

    return frequencies


def format_value(val):
    """Format a value, removing numpy type prefixes."""
    val_str = str(val)
    # Remove numpy type prefixes like 'np.float64(' and 'np.int64('
    if val_str.startswith('np.'):
        # Extract just the value
        import re
        match = re.search(r'\((.*?)\)', val_str)
        if match:
            return match.group(1)
    return val_str


def analyze_parameter(df, param_cols, param_name, metric, top_n_percent):
    """Complete analysis for a single parameter."""
    unique_vals = sorted(df[param_name].unique())

    # Skip parameters with only one value
    if len(unique_vals) == 1:
        return

    print(f"\nParameter: {param_name}")

    # Get rank and frequency stats
    rank_stats = calculate_value_ranks(df, param_cols, param_name, metric)
    freq_stats = calculate_top_n_frequency(df, param_cols, param_name, metric, top_n_percent)

    # Compute all pairwise comparisons
    comparison_matrix = {}
    for val1, val2 in combinations(unique_vals, 2):
        stats = analyze_parameter_comparison(df, param_cols, param_name, val1, val2, metric)
        if stats:
            comparison_matrix[(val1, val2)] = stats

    # Build table header
    header_parts = ["Value", "AvgRank", f"Top{top_n_percent:.0f}%"]
    for val in unique_vals:
        header_parts.append(f"vs {format_value(val)}")

    # Calculate column widths
    col_widths = [max(len(header_parts[i]), 12) for i in range(len(header_parts))]

    # Print header
    header_line = "  ".join(part.ljust(col_widths[i]) for i, part in enumerate(header_parts))
    print(header_line)
    print("-" * len(header_line))

    # Print each row
    for val in unique_vals:
        row_parts = []

        # Value
        row_parts.append(format_value(val).ljust(col_widths[0]))

        # AvgRank
        row_parts.append(f"{rank_stats[val]['avg_rank']:.1f}".ljust(col_widths[1]))

        # Top N%
        row_parts.append(f"{freq_stats[val]['frequency_pct']:.1f}%".ljust(col_widths[2]))

        # Pairwise comparisons
        for i, other_val in enumerate(unique_vals):
            col_idx = 3 + i
            if val == other_val:
                row_parts.append("-".ljust(col_widths[col_idx]))
            elif (val, other_val) in comparison_matrix:
                stats = comparison_matrix[(val, other_val)]
                cell = f"{stats['win_count']}/{stats['loss_count']} ({stats['avg_diff']:+.1f})"
                row_parts.append(cell.ljust(col_widths[col_idx]))
            elif (other_val, val) in comparison_matrix:
                stats = comparison_matrix[(other_val, val)]
                cell = f"{stats['loss_count']}/{stats['win_count']} ({-stats['avg_diff']:+.1f})"
                row_parts.append(cell.ljust(col_widths[col_idx]))
            else:
                row_parts.append("N/A".ljust(col_widths[col_idx]))

        print("  ".join(row_parts))


def main():
    args = parse_args()

    # Load data
    df, param_cols, metric_cols = load_results(args.results_file)

    # Validate metric
    if args.metric not in metric_cols:
        print(f"Error: Metric '{args.metric}' not found in results.")
        print(f"Available metrics: {metric_cols}")
        sys.exit(1)

    print(f"Loaded {len(df)} variants, optimizing {args.metric}, top-{args.top_n_percent}%")
    print(f"AvgRank: lower is better (1=best). Top{args.top_n_percent:.0f}%: % of configs with this value in top {args.top_n_percent:.0f}%.")
    print(f"+/-: wins/losses in head-to-head comparisons. Avg diff: average {args.metric} difference.\n")

    # Analyze each parameter
    for param in param_cols:
        analyze_parameter(df, param_cols, param, args.metric, args.top_n_percent)


if __name__ == '__main__':
    main()
