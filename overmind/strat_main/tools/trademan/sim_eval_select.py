"""Interactive (strat, sym) pair selector for sim-eval."""

import sys


def interactive_select(pairs):
    """Present numbered list of (alias, group, strat, sym) pairs for selection.

    Args:
        pairs: list of dicts with keys [alias, group, strat, sym]

    Returns:
        list of selected pair dicts
    """
    if not pairs:
        print("No (strat, sym) pairs found.")
        sys.exit(1)

    # Group by strat for cleaner display
    strat_key = lambda p: (p["alias"], p["group"], p["strat"])
    current_strat = None

    print()
    print("=" * 80)
    print("Available (strat, symbol) pairs:")
    print("=" * 80)

    for i, pair in enumerate(pairs, 1):
        sk = strat_key(pair)
        if sk != current_strat:
            current_strat = sk
            print(f"\n  [{pair['alias']}] {pair['group']}/{pair['strat']}")
        print(f"    {i:3d}. {pair['sym']}")

    print()
    print("-" * 80)
    print("  Enter numbers: 1,3,5-8    Select all: all    Quit: q")
    print("-" * 80)

    while True:
        try:
            selection = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

        if selection.lower() == "q":
            print("Aborted.")
            sys.exit(0)

        if selection.lower() == "all":
            print(f"Selected all {len(pairs)} pair(s).")
            return pairs

        try:
            indices = _parse_selection(selection, len(pairs))
            selected = [pairs[i - 1] for i in indices]

            print(f"\nSelected {len(selected)} pair(s):")
            for p in selected:
                print(f"  [{p['alias']}] {p['group']}/{p['strat']}  {p['sym']}")

            confirm = input("Proceed? [y/n] ").strip().lower()
            if confirm in ("y", "yes", ""):
                return selected
            print("Try again...")
        except ValueError as e:
            print(f"Invalid selection: {e}")


def _parse_selection(selection_str, max_index):
    """Parse '1,3,5-8,10' into sorted list of 1-based indices."""
    indices = set()
    for part in selection_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start.strip()), int(end.strip())
            if start < 1 or end > max_index or start > end:
                raise ValueError(f"range {start}-{end} out of bounds (1-{max_index})")
            indices.update(range(start, end + 1))
        else:
            idx = int(part)
            if idx < 1 or idx > max_index:
                raise ValueError(f"index {idx} out of bounds (1-{max_index})")
            indices.add(idx)
    if not indices:
        raise ValueError("empty selection")
    return sorted(indices)
