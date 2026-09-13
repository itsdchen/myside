"""Get market data onto a remote box, two ways:

1. `ensure(alias, md_exists_args)` — runs md_exists.py on the remote to
   pull from the user's S3 bucket. Preferred long-term; requires the
   read-only AWS creds at ~/.aws/credentials on the remote.

2. `stage_local(alias, paths)` — rsyncs an explicit list of local gzpbf
   files to the remote. Used when AWS creds aren't provisioned yet, or
   for small one-off pushes.

═══════════════════════════════════════════════════════════════════════
What mktdata pktrade needs to run a sim (the "staging pattern")
═══════════════════════════════════════════════════════════════════════
A sim of conf C on date D opens market data for:

  - Every (symbol, market) pair referenced by C — both as a `traded_symbol`
    AND as a signal source. Inspect the conf's `pktraders[*].ordex.markets`,
    `pktraders[*].signals[*].symbol`+`books`, `pktraders[*].tempos[*]`, etc.

  - Each (sym, market) needs the right CHANNEL(s) for its venue:
        Hyperliquid  → both `l2Book` and `trades`
        TopBookCme   → `quotes`
        (other venues TBD as they come up)

  - BOTH date D and date D-1 (previous day) — pktrade uses prior-day data
    for warmup / overnight state.

  - File naming: ~/tardis_datasets/gzpbf/<market>/<sym>_<channel>_<YYYYMMDD>.gzpbf

A complete pktrade event-by-event list of what files it opens is in the
strace output if you ever need to debug — `strace -f -e openat | grep gzpbf`.

A smarter "what does this conf need" helper could call `pktrade --dry-run`
(the same approach md_exists.py uses) to extract the subscription list.
Worth building when staging gets painful manually.
═══════════════════════════════════════════════════════════════════════
"""

from pathlib import Path

from . import _ssh


REMOTE_MD_EXISTS = "~/bin/md_exists.py"
REMOTE_DATA_DIR = "~/tardis_datasets/gzpbf"
REMOTE_VENV_PY = "~/.venvs/v1/bin/python"


def ensure(alias: str, md_exists_args: list, timeout: float = 1800.0) -> None:
    """Ensure market data is present on `alias` by invoking md_exists.py there.

    Args:
        alias: ssh alias of the remote box.
        md_exists_args: list of args to pass to md_exists.py, NOT including
            --localdir or --aws (we always pass --aws and the remote default
            data dir). Example: ["--market", "TopBookCme", "--sym", "NQ",
            "--start", "20260101", "--end", "20260131"].
        timeout: seconds to allow for the download (defaults to 30 min for
            large fetches).

    Raises:
        _ssh.SshError if md_exists.py returns non-zero.

    Example:
        from overmind.swarmhost.mktdata import ensure
        ensure("hetzner-1", ["--market", "TopBookCme", "--sym", "NQ",
                             "--start", "20260101", "--end", "20260131"])
    """
    args_str = " ".join(_shell_quote(a) for a in md_exists_args)
    cmd = (
        f"{REMOTE_VENV_PY} {REMOTE_MD_EXISTS} "
        f"--aws --localdir {REMOTE_DATA_DIR} {args_str}"
    )
    _ssh.run(alias, cmd, timeout=timeout)


def discover_for_conf(conf_path: Path, dates: list, local_pktrade: Path,
                      include_prev_day: bool = True, local_root: Path = None) -> dict:
    """Figure out which gzpbf files a conf needs, and which exist locally.

    Same discovery logic stage_for_conf uses, but stops before any rsync.
    Pulled out so callers can pre-check + prompt before kicking off the
    parallel staging (otherwise each host's `stage_for_conf` separately
    discovers the missing files and you get N copies of the same error).

    Args:
        conf_path: local path to the pktrade conf.
        dates: list of YYYYMMDD strings to sim.
        local_pktrade: path to local pktrade binary (used for --dry-run).
        include_prev_day: include the prior *calendar* day for warmup and
            infill weekend dates between sim weekdays. Needed for overnight
            sessions (18:00 prev → 09:30 today) on 24/7 feeds (HL, BTC).
        local_root: local gzpbf root (defaults to ~/tardis_datasets/gzpbf).

    Returns: dict with keys
        "existing": list[Path] — files that exist on local disk
        "missing":  list[Path] — files this conf needs but local doesn't have
        "pairs":    set[(sym, market)] — what pktrade --dry-run reported
    """
    import subprocess
    if local_root is None:
        local_root = Path.home() / "tardis_datasets" / "gzpbf"
    local_root = Path(local_root).expanduser().resolve()

    # 1. Extract (sym, mkt) pairs via pktrade --dry-run.
    dryrun_date = sorted(dates)[0]
    cmd = [str(local_pktrade), "--date", dryrun_date,
           "--conf", str(conf_path), "--dry-run"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    pairs = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not (line.startswith("(") and line.endswith(")")):
            continue
        inner = line[1:-1]
        parts = [p.strip() for p in inner.split(",", 1)]
        if len(parts) != 2:
            continue
        book, sym = parts
        sym = sym.replace("/", "_")
        if "USDT" in sym:
            continue
        pairs.add((sym, book))

    if not pairs:
        raise RuntimeError(
            f"pktrade --dry-run on {conf_path} extracted no (sym, market) pairs. "
            f"stdout tail: {proc.stdout[-500:]!r}"
        )

    # 2. Map (sym, market) → channels.
    import sys as _sys
    _strat_main = Path(__file__).resolve().parents[1] / "strat_main"
    if _strat_main.exists() and str(_strat_main) not in _sys.path:
        _sys.path.insert(0, str(_strat_main))
    try:
        from util.mktdata import books_to_channels  # type: ignore
    except ImportError:
        books_to_channels = {
            "Hyperliquid": ["trades", "l2Book"],
            "TopBookCme": ["quotes"],
            "TopBookEquity": ["quotes"],
            "BinanceFutures": ["depth", "depthSnapshot", "bookTicker", "trade"],
            "BinanceSPOT": ["depth", "depthSnapshot", "bookTicker", "trade"],
        }

    # 3. Expand dates with warmup + weekend infill.
    # For overnight sessions (18:00 prev → 09:30 today), the prev calendar
    # day can be Sat/Sun and 24/7 feeds (HL, BTC) need data for that day.
    # The old logic skipped to the prev weekday, which broke uninitialized
    # rel_mid signals on Mon-after-weekend overnight sims for HL-natives
    # (e.g. PURRDAT/PURR firing on Sat HL events with garbage NQ mid).
    # Fix: include the prev calendar day, and infill weekend dates that
    # fall *between* consecutive sim weekdays for the same reason.
    all_dates = list(dates)
    if include_prev_day:
        from datetime import date, timedelta
        first = date(int(min(dates)[:4]), int(min(dates)[4:6]), int(min(dates)[6:8]))
        last = date(int(max(dates)[:4]), int(max(dates)[4:6]), int(max(dates)[6:8]))
        cur = first - timedelta(days=1)
        while cur <= last:
            all_dates.append(cur.strftime("%Y%m%d"))
            cur += timedelta(days=1)
        all_dates = sorted(set(all_dates))

    # 4. Build file list, partition into existing/missing.
    files = []
    for sym, mkt in pairs:
        channels = books_to_channels.get(mkt)
        if not channels:
            print(f"  warn: no channels mapping for market {mkt!r}; skipping {sym}")
            continue
        for ch in channels:
            for d in all_dates:
                files.append(local_root / mkt / f"{sym}_{ch}_{d}.gzpbf")
    existing = [f for f in files if f.exists()]
    missing = [f for f in files if not f.exists()]
    return {"existing": existing, "missing": missing, "pairs": pairs}


def s3_fetch_missing(missing_paths: list, local_root: Path = None) -> dict:
    """Try to fetch missing gzpbf files from S3 to local using md_exists.

    Pulls only what's missing — parses each missing path into (sym, market,
    channel, date), groups by (sym, market) and feeds md_exists's
    `check_for_data(..., dl_automatically=True)` which downloads without
    prompting.

    Args:
        missing_paths: list[Path] from discover_for_conf()["missing"].
        local_root: local gzpbf root (defaults to ~/tardis_datasets/gzpbf).

    Returns: dict with keys
        "attempted": int — number of files we tried to fetch
        "fetched":   list[Path] — files now present on local that weren't before
        "still_missing": list[Path] — files S3 didn't have either
        "error":     str | None — populated if S3 setup failed entirely

    Never raises — caller treats it as best-effort.
    """
    if local_root is None:
        local_root = Path.home() / "tardis_datasets" / "gzpbf"
    local_root = Path(local_root).expanduser().resolve()
    result = {"attempted": len(missing_paths), "fetched": [],
              "still_missing": [], "error": None}
    if not missing_paths:
        return result

    # Group missing paths into the {(sym, market): [dates]} dict that
    # check_for_data expects. Each path is .../<market>/<sym>_<channel>_<date>.gzpbf.
    syms_books_to_dates: dict = {}
    for p in missing_paths:
        p = Path(p)
        try:
            mkt = p.parent.name
            stem = p.stem  # e.g. "xyz:SP500_l2Book_20260601"
            # Last 8 chars are the date; the chunk before the last '_' is the channel.
            date_str = stem[-8:]
            rest = stem[:-9]  # drop "_<date>"
            sym, _channel = rest.rsplit("_", 1)
        except (IndexError, ValueError):
            # Can't parse — skip; caller will see this in still_missing.
            continue
        syms_books_to_dates.setdefault((sym, mkt), []).append(date_str)

    # Try to import md_exists. We add overmind/strat_main to sys.path on the
    # fly (mktdata.py already does this for books_to_channels).
    import sys as _sys
    _strat_main = Path(__file__).resolve().parents[1] / "strat_main"
    if _strat_main.exists() and str(_strat_main) not in _sys.path:
        _sys.path.insert(0, str(_strat_main))
    try:
        import tools.md_exists as md_exists  # type: ignore
    except Exception as e:
        result["error"] = f"could not import md_exists ({e})"
        result["still_missing"] = list(missing_paths)
        return result

    # Run the S3 fetch in auto-confirm mode. Any individual download failure
    # is swallowed by check_for_data (it removes the dest file and continues).
    try:
        md_exists.check_for_data(syms_books_to_dates, str(local_root),
                                  dl_automatically=True)
    except Exception as e:
        result["error"] = f"check_for_data raised: {e}"

    # Re-check what's now on disk vs what we started with.
    for p in missing_paths:
        if Path(p).exists():
            result["fetched"].append(p)
        else:
            result["still_missing"].append(p)
    return result


def stage_for_conf(alias: str, conf_path: Path, dates: list, local_pktrade: Path,
                   include_prev_day: bool = True, local_root: Path = None,
                   timeout: float = 600.0, allow_missing: bool = False) -> int:
    """Stage exactly the mktdata files a conf needs to sim the given dates.

    Uses the same `pktrade --dry-run` trick `md_exists.py` uses to extract
    the (symbol, market) pairs the conf subscribes to. Maps each pair to its
    channels via util.mktdata.books_to_channels, expands to gzpbf file paths
    for the date range (plus prev-day for warmup if requested), then calls
    stage_local to rsync them up.

    Args:
        alias: ssh alias to push to.
        conf_path: local path to the pktrade conf.
        dates: list of YYYYMMDD strings to sim.
        local_pktrade: path to the local pktrade binary (used for --dry-run).
            Can be either the regular bin/pktrade or jammy.bin/pktrade — only
            its subscription-extraction output matters here.
        include_prev_day: pktrade reads the previous trading day's data for
            warmup. With include_prev_day=True (default), we add the weekday
            before the earliest date in `dates` to the staging set.
        local_root: local gzpbf root (defaults to ~/tardis_datasets/gzpbf).
        timeout: rsync timeout.
        allow_missing: if True, stage what's available and silently skip
            files that don't exist locally (caller has already inspected
            via discover_for_conf and chosen to proceed). Default False:
            raise FileNotFoundError on the first missing file.

    Returns: number of files staged. (rsync skips already-present.)

    Raises:
        RuntimeError if pktrade --dry-run fails or returns no subscriptions.
        FileNotFoundError if any expected file is missing locally and
            allow_missing=False.
    """
    if local_root is None:
        local_root = Path.home() / "tardis_datasets" / "gzpbf"
    local_root = Path(local_root).expanduser().resolve()

    disc = discover_for_conf(conf_path, dates, local_pktrade,
                              include_prev_day=include_prev_day,
                              local_root=local_root)
    if disc["missing"] and not allow_missing:
        raise FileNotFoundError(
            f"{len(disc['missing'])} expected mktdata files not found locally; "
            f"first: {disc['missing'][0]}. "
            f"Use discover_for_conf to inspect and stage_for_conf(allow_missing=True) "
            f"if you want to stage what's available."
        )
    return stage_local(alias, [str(f) for f in disc["existing"]],
                       local_root=local_root, timeout=timeout)


def stage_local(alias: str, paths: list, local_root: Path = None, timeout: float = 600.0) -> int:
    """rsync an explicit list of gzpbf files from local to remote.

    Preserves the per-market subdir layout (e.g. Hyperliquid/foo.gzpbf
    lands under ~/tardis_datasets/gzpbf/Hyperliquid/foo.gzpbf on remote).

    Args:
        alias: ssh alias to push to.
        paths: iterable of paths to gzpbf files. May be absolute or
            relative to `local_root`. Files are grouped by their immediate
            parent dir name (the market name) for the rsync.
        local_root: defaults to ~/tardis_datasets/gzpbf. Path components
            BELOW this are mirrored on the remote; components above are
            stripped.
        timeout: seconds for the whole transfer.

    Returns: number of files transferred (counted at the local side; rsync
        on remote with --inplace will skip anything already matching).

    Raises:
        _ssh.SshError on rsync failure.

    Example:
        from overmind.swarmhost.mktdata import stage_local
        stage_local("hetz0", [
            "~/tardis_datasets/gzpbf/Hyperliquid/BTC_l2Book_20260529.gzpbf",
            "~/tardis_datasets/gzpbf/TopBookCme/ES_quotes_20260529.gzpbf",
        ])
    """
    if local_root is None:
        local_root = Path.home() / "tardis_datasets" / "gzpbf"
    local_root = Path(local_root).expanduser().resolve()

    # Group by market subdir so we issue one rsync per market — fewer
    # SSH handshakes than one-rsync-per-file.
    by_market: dict = {}
    for p in paths:
        full = Path(p).expanduser().resolve()
        if not full.exists():
            raise FileNotFoundError(f"local gzpbf not found: {full}")
        rel = full.relative_to(local_root)
        market = rel.parts[0] if len(rel.parts) > 1 else ""
        by_market.setdefault(market, []).append(full)

    n = 0
    for market, files in by_market.items():
        remote_dir = f"{REMOTE_DATA_DIR}/{market}/" if market else f"{REMOTE_DATA_DIR}/"
        # Ensure remote subdir exists. Cheap one-shot ssh per market.
        _ssh.run(alias, f"mkdir -p {remote_dir}", timeout=15)
        # rsync_push handles single-file or directory; we use multiple
        # files in one shot via a single rsync invocation.
        for f in files:
            _ssh.rsync_push(alias, f, remote_dir, timeout=timeout)
            n += 1
    return n


def _shell_quote(s: str) -> str:
    """Shell-quote a single arg. Avoids importing shlex for a one-liner."""
    if not s or any(c in s for c in " \t\n'\"\\$`"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s
