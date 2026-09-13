#!/usr/bin/env python3
"""Utilities for managing alpha_rel_wide_mm regression bundles.

This script supports two primary workflows:
  1. Importing an existing TradeArmory regression output directory
     (expects pred_sig.json + ref_sig.json under a coef_lair directory).
  2. Auto-building a regression by invoking an external command, then
     importing the freshly generated files (pass the command via
     --regression-cmd).

All bundles are normalized and written both to the repo copy under
overmind/model_inheritance and optionally to a scratch directory
(defaults to ~/scratch/relwide_modelbank).  A simple catalog json keeps
track of the latest bundles per (venue, symbol).
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[3]
REPO_BANK_ROOT = REPO_ROOT / "overmind" / "model_inheritance"
DEFAULT_SCRATCH_BANK = Path.home() / "scratch" / "relwide_modelbank"
DEFAULT_CATALOG_PATH = REPO_BANK_ROOT / "catalog.json"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Any:
    with path.open("r") as fh:
        return json.load(fh)


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def _deep_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj))


def _symbol_slug(symbol: str) -> str:
    return symbol.upper().replace(":", "_")


def _rename_root_signal(obj: Any, target_name: str) -> Any:
    clone = _deep_copy(obj)
    signals = clone.get("signals")
    if signals and isinstance(signals, list) and signals[0].get("name"):
        signals[0]["name"] = target_name
    return clone


def _bundle_payload(symbol: str, venue: str, pred_path: Path, ref_path: Path,
                    start_date: str, end_date: str, quality: str,
                    built_at: str, source_dir: Path,
                    score: float | None) -> Dict[str, Any]:
    slug = _symbol_slug(symbol)
    pred = _load_json(pred_path)
    ref = _load_json(ref_path)
    pred_norm = _rename_root_signal(pred, f"pred_{slug}")
    ref_norm = _rename_root_signal(ref, f"ref_{slug}")
    metadata = {
        "symbol": symbol,
        "venue": venue,
        "built_at": built_at,
        "start_date": start_date,
        "end_date": end_date,
        "quality": quality,
        "source_dir": str(source_dir),
        "score": score,
        "bundle_version": 1,
    }
    bundle = {
        "metadata": metadata,
        "pred_signal": pred_norm,
        "ref_signal": ref_norm,
    }
    bundle_json = json.dumps(bundle, sort_keys=True)
    metadata["sha256"] = hashlib.sha256(bundle_json.encode("utf-8")).hexdigest()
    return bundle


def _write_bundle(bundle: Dict[str, Any], venue: str, symbol: str,
                  built_at: str, scratch_root: Path) -> Dict[str, Path]:
    slug = _symbol_slug(symbol)
    repo_dir = REPO_BANK_ROOT / venue / slug / built_at
    scratch_dir = scratch_root / venue / slug / built_at
    for target in (repo_dir, scratch_dir):
        _ensure_dir(target)
        _write_json(target / "model_bundle.json", bundle)
        _write_json(target / "metadata.json", bundle["metadata"])
    return {
        "repo": repo_dir / "model_bundle.json",
        "scratch": scratch_dir / "model_bundle.json",
    }


def _load_catalog(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return _load_json(path)


def _update_catalog(path: Path, venue: str, symbol: str,
                    built_at: str, quality: str, bundle_path: Path,
                    metadata: Dict[str, Any]) -> None:
    catalog = _load_catalog(path)
    catalog.setdefault(venue, {})
    catalog[venue].setdefault(symbol, [])
    entries = catalog[venue][symbol]
    entries = [e for e in entries if e.get("built_at") != built_at]
    entries.append({
        "built_at": built_at,
        "quality": quality,
        "bundle_path": str(bundle_path),
        "sha256": metadata["sha256"],
        "start_date": metadata["start_date"],
        "end_date": metadata["end_date"],
        "score": metadata.get("score"),
    })
    # Keep newest first
    entries.sort(key=lambda e: e["built_at"], reverse=True)
    catalog[venue][symbol] = entries
    _ensure_dir(path.parent)
    _write_json(path, catalog)


def _parse_date(date_str: str | None, default: str) -> str:
    return date_str or default


def cmd_import(args: argparse.Namespace) -> None:
    pred_path = Path(args.source_dir) / args.pred_file
    ref_path = Path(args.source_dir) / args.ref_file
    if not pred_path.exists() or not ref_path.exists():
        raise FileNotFoundError("pred_sig/ref_sig json not found in source dir")
    built_at = _parse_date(args.built_at, date.today().strftime("%Y%m%d"))
    start_date = _parse_date(args.start_date, args.default_start)
    end_date = _parse_date(args.end_date, args.default_end)
    bundle = _bundle_payload(
        symbol=args.symbol,
        venue=args.venue,
        pred_path=pred_path,
        ref_path=ref_path,
        start_date=start_date,
        end_date=end_date,
        quality=args.quality,
        built_at=built_at,
        source_dir=Path(args.source_dir),
        score=args.score,
    )
    paths = _write_bundle(bundle, args.venue, args.symbol, built_at,
                          scratch_root=Path(args.scratch_root))
    _update_catalog(Path(args.catalog), args.venue, args.symbol,
                    built_at, args.quality, paths["repo"], bundle["metadata"])
    print(f"Imported bundle for {args.symbol} ({args.venue}) -> {paths['repo']}")


def _run_regression_command(command: str, work_dir: Path) -> None:
    proc = subprocess.run(command, cwd=str(work_dir), shell=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Regression command failed ({proc.returncode})")


def cmd_autobuild(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir).expanduser()
    _ensure_dir(work_dir)
    built_at = date.today().strftime("%Y%m%d")
    job_dir = work_dir / f"autobuild_{args.symbol.replace(':', '_')}_{built_at}"
    if job_dir.exists():
        shutil.rmtree(job_dir)
    _ensure_dir(job_dir)
    if not args.regression_cmd:
        raise ValueError("--regression-cmd is required for autobuild mode")
    formatted_cmd = args.regression_cmd.format(
        symbol=args.symbol,
        venue=args.venue,
        start=args.start_date,
        end=args.end_date,
        workdir=str(job_dir)
    )
    print(f"Running regression: {formatted_cmd}")
    _run_regression_command(formatted_cmd, job_dir)
    source_dir = job_dir / args.source_subdir
    if not source_dir.exists():
        raise FileNotFoundError(f"Expected regression output at {source_dir}")
    import_args = argparse.Namespace(
        symbol=args.symbol,
        venue=args.venue,
        source_dir=str(source_dir),
        pred_file=args.pred_file,
        ref_file=args.ref_file,
        built_at=args.built_at or built_at,
        start_date=args.start_date,
        end_date=args.end_date,
        default_start=args.start_date,
        default_end=args.end_date,
        quality=args.quality,
        score=args.score,
        scratch_root=args.scratch_root,
        catalog=args.catalog,
    )
    cmd_import(import_args)


def cmd_list(args: argparse.Namespace) -> None:
    catalog = _load_catalog(Path(args.catalog))
    venue_entries = catalog.get(args.venue, {})
    if args.symbol:
        entries = venue_entries.get(args.symbol, [])
        for entry in entries:
            print(json.dumps(entry, indent=2))
    else:
        for symbol, entries in venue_entries.items():
            print(symbol)
            for entry in entries:
                print(f"  {entry['built_at']} {entry['quality']} -> {entry['bundle_path']}")


def add_common_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--symbol", required=True, help="Symbol id, e.g. xyz:CL")
    sub.add_argument("--venue", default="Hyperliquid", help="Venue name")
    sub.add_argument("--quality", default="provisional",
                     choices=["provisional", "validated"],
                     help="Quality tier label")
    sub.add_argument("--scratch-root", default=str(DEFAULT_SCRATCH_BANK),
                     help="Scratch model bank root")
    sub.add_argument("--catalog", default=str(DEFAULT_CATALOG_PATH),
                     help="Catalog json path")
    sub.add_argument("--start-date", dest="start_date",
                     default=(date.today().replace(day=1).strftime("%Y%m%d")),
                     help="Regression window start (YYYYMMDD)")
    sub.add_argument("--end-date", dest="end_date",
                     default=date.today().strftime("%Y%m%d"),
                     help="Regression window end (YYYYMMDD)")
    sub.add_argument("--score", type=float, default=None,
                     help="Optional fit score")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage relwide model bundles")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="Import existing regression outputs")
    add_common_args(import_parser)
    import_parser.add_argument("--source-dir", required=True,
                               help="Directory containing pred_sig/ref_sig json")
    import_parser.add_argument("--pred-file", default="pred_sig.json")
    import_parser.add_argument("--ref-file", default="ref_sig.json")
    import_parser.add_argument("--built-at",
                               help="Build stamp YYYYMMDD (default today)")
    import_parser.set_defaults(func=cmd_import, default_start=None, default_end=None)

    autobuild_parser = subparsers.add_parser("autobuild", help="Run regression then import")
    add_common_args(autobuild_parser)
    autobuild_parser.add_argument("--regression-cmd", required=True,
                                  help="Shell command to run regression."
                                       " Format placeholders: {symbol},{venue},{start},{end},{workdir}")
    autobuild_parser.add_argument("--work-dir", default=str(Path.home() / "scratch" / "relwide_autobuild"))
    autobuild_parser.add_argument("--source-subdir", default="coef_lair",
                                  help="Subdirectory under work dir containing pred/ref json")
    autobuild_parser.add_argument("--pred-file", default="pred_sig.json")
    autobuild_parser.add_argument("--ref-file", default="ref_sig.json")
    autobuild_parser.add_argument("--built-at", help="Override build stamp")
    autobuild_parser.set_defaults(func=cmd_autobuild)

    list_parser = subparsers.add_parser("list", help="List bundles in the catalog")
    list_parser.add_argument("--venue", default="Hyperliquid")
    list_parser.add_argument("--symbol", help="Filter to single symbol")
    list_parser.add_argument("--catalog", default=str(DEFAULT_CATALOG_PATH))
    list_parser.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
