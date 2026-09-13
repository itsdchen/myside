#! /usr/bin/env python3


"""
Given a set of TradeAcademy conffiles, figure out if mktdata for those things exists or if we need to copy it over from a hard drive.

Asks for the user's feedback, and then copies things over.

Example usage:

If we give --aws
then we try to grab the files from aws instead.

./md_exists.py --localdir /home/dchen/tardis_datasets/gzpbf --confs ~/modeltrain/20230602/*/modelbuild.conf --aws

# Use this to figure out what data we need to generate. 
./md_exists.py --market TopBookCme --sym NQ --start 20251120 --end 20251125


TODO: implement correctly for TradeLair. Not a focus rn tho.

Note: There is a little bit of inconsistency here, because we don't necessarily download data for the last day in the range. I hope that's still ok?

"""


import argparse
import getpass
import os
import sys
import shutil
import subprocess
try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None


##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from util.pathing import bindir

from util.chron import dates_list

from util import mktdata

pk_bin = os.path.join(bindir(), "pktrade")


def manage_pk_data(pk_confs_lst, start, end, local_dir):
    syms_books_to_dates = {}
    dates = dates_list(start, end)

    # First get the syms
    # (This is a set bc diff pk confs can have redundant subscriptions.)
    sym_book_pairs = set()
    for one_pk_conf in pk_confs_lst:
        dryrun_cmd = "{PK_BIN} --date {FIRST_DATE} --conf {PK_CONF} --dry-run".format(
            PK_BIN=pk_bin,FIRST_DATE=dates[0], PK_CONF=one_pk_conf
        )

        # Run it...
        dryrun_lines = subprocess.run(
            dryrun_cmd, shell=True, capture_output=True, text=True
        )
        sub_lines = dryrun_lines.stdout.split("\n")
        print(dryrun_lines)
        for one_sub_line in sub_lines:
            if len(one_sub_line.strip()) == 0:
                continue
            if one_sub_line[0] != "(" and one_sub_line[-1] != ")":
                continue
            # Otherwise, parse the (book, symbol) pairs and turn it into (symbol, book)
            # for this subscription checker.
            one_sub_line = one_sub_line.replace("(", "").replace(")", "")
            bk_str = one_sub_line.split(",")[0].strip()
            sym_str = one_sub_line.split(",")[1].strip()

            # Do the appropriate sym substitution.
            # Note that we may need to do symbology substitution
            # (eg, symbols with a "/" in their name will have that
            # replaced with a "_" in the filename)
            sym_str = sym_str.replace("/", "_")
            if "USDT" in sym_str:
                continue

            sym_book_pairs.add((sym_str, bk_str))

    sym_book_pairs = list(sym_book_pairs)

    for one_pair in sym_book_pairs:
        syms_books_to_dates[one_pair] = dates
    # Make the call.
    check_for_data(syms_books_to_dates, local_dir)


def manage_custom_data(sym, market, start, end, local_dir):
    syms_books_to_dates = {}
    dates = dates_list(start, end)
    syms_books_to_dates = {
        (sym, market) : dates
    }
    print(dates)

    check_for_data(syms_books_to_dates, local_dir)


def manage_modelbuild_data(modelbuild_confs_lst, local_dir):
    import stratbuilder.TradeAcademy

    all_syms_books_to_dates = {}
    for one_ta_conf in modelbuild_confs_lst:
        syms_books_to_dates = stratbuilder.TradeAcademy.get_wanted_datas(one_ta_conf)
        for one_sb_pair, dates_lst in syms_books_to_dates.items():
            if one_sb_pair not in all_syms_books_to_dates:
                all_syms_books_to_dates[one_sb_pair] = []
            all_syms_books_to_dates[one_sb_pair] += dates_lst

    # Dedup
    for one_sb_pair, dates_lst in all_syms_books_to_dates.items():
        all_syms_books_to_dates[one_sb_pair] = list(set(dates_lst))

    # Ask for the datas.
    check_for_data(all_syms_books_to_dates, local_dir)


def missing_data_files(syms_books_to_dates, local_dir):
    # OK, start reading through the confs and check out what data we might want.
    # Now map these things to data files.
    wanted_files = []
    existing_files = []
    missing_files = []

    # key: the local path to the data file
    # value: a dict with mkt, sym, date, channel.
    localpath_to_props = {}
    for one_sb_pair, dates_lst in syms_books_to_dates.items():
        # Go through the channels for the market.
        mkt = one_sb_pair[1]
        mkt_channels = mktdata.books_to_channels[mkt]

        for one_channel in mkt_channels:
            for one_date in dates_lst:
                # The wanted file is there.

                # Note that the symbol name in these filenames is lowercased.
                # ugh.
                # TODO: undoing this.
                sym_name = one_sb_pair[0]
                wanted_path = os.path.join(
                    mkt,
                    "{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                        SYM=sym_name,
                        CHANNEL=one_channel,
                        DATE=one_date,
                    ),
                )
                wanted_files.append(wanted_path)
                localpath_to_props[wanted_path] = {
                    "mkt": mkt,
                    "sym": sym_name,
                    "date": one_date,
                    "channel": one_channel
                }

    # dedup wanted files.
    wanted_files = list(set(wanted_files))
    # Check for existence.
    for one_wanted_file in wanted_files:
        lcl_path = os.path.join(local_dir, one_wanted_file)
        if not os.path.exists(lcl_path):
            missing_files.append(one_wanted_file)

    still_missing = []


    files_to_copy_props = {}

    # Check which we have in the external location.
    for one_missing_file in missing_files:
        # The only thing I'm pretty sure is clean is the hyperliquid
        # stuff. Other datafiles I need to regenerate, pls be patient ser.
        if localpath_to_props[one_missing_file]["mkt"] in ["Hyperliquid", "BinanceFutures", "BinanceSPOT", "TopBookCme", "TopBookEquity"]:
            files_to_copy_props[one_missing_file] = localpath_to_props[one_missing_file]

    still_missing.sort()

    return {"missing": still_missing, "files_to_copy_props": files_to_copy_props}


def check_for_data(syms_books_to_dates, local_dir, dl_automatically=False):
    s3 = boto3.Session(profile_name='l1').client("s3")

    # Parse these, check for data wants, etc etc.
    # Set of all files we might want.
    wanted_data_files = missing_data_files(syms_books_to_dates, local_dir)

    still_missing = wanted_data_files["missing"]

    files_to_copy_props = wanted_data_files["files_to_copy_props"]


    # If we successfully copied over a file, we add it to the set so we
    # can skip it for the other cp actions.
    s3_copied_files = set()

    # Handle the files we need to handle.
    # If the file exists, great.
    if  len(files_to_copy_props) > 0:
        # Then we cp stuff over from the cloud.

        for dest_file_name, one_file_props in files_to_copy_props.items():
            print("Getting {} from aws ({} total). Proceed? [y/n]".format(dest_file_name, len(files_to_copy_props)))
            break

        x = ""
        if dl_automatically:
            print("Downloading automatically.")
            x = "y"
        while x not in ["y", "n", "Y", "N"]:
            x = input()
        if x in ["y", "Y"]:
            # Start downloading stuff from aws.

            for dest_file_name, one_file_props in files_to_copy_props.items():
                # Copy it to the approp thing area.

                # it is what it is.
                src_bucket = "l1-pktrade-capture"

                object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                    MKT=one_file_props["mkt"],
                    SYM=one_file_props["sym"],
                    CHANNEL=one_file_props["channel"],
                    DATE=one_file_props["date"],
                )

                dest_file_path = os.path.join(local_dir, dest_file_name)
                try:
                    with open(dest_file_path, "wb") as f:
                        s3.download_fileobj(src_bucket, object_name, f)
                    # Mark that we downloaded this file successfully.
                    s3_copied_files.add(dest_file_name)
                except Exception as e:
                    print("DL failed for {}".format(dest_file_name))
                    # Probably delete it if it doesn't owkr.
                    os.remove(dest_file_path)
                    continue
            pass

    # Let's actually take copy_over and remove the ones that were
    # handled already.

    # Also inform them that there are dditional missing files if they exist.
    if len(still_missing) > 0:
        print("*" * 100)
        print("\n".join(still_missing))
        print(
            "The above {} files also don't exist in the ext data repo: please download and convert separately".format(
                len(still_missing)
            )
        )
        print("*" * 100)

    # check which ones are missing.

    # Ideally, merge these things.
    # OK. Now we
    pass


def main():
    parser = argparse.ArgumentParser()
    # This returns a list. We can use a glob with it.
    parser.add_argument("--confs", nargs="*")
    parser.add_argument("--pk", nargs="*")

    # If given, we just use this instead.
    parser.add_argument("--sym", type=str)
    parser.add_argument("--market", type=str)
    # If pk confs given, need a start and end dates too.
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)

    parser.add_argument(
        "--localdir", default="{}/tardis_datasets/gzpbf".format(os.getenv("HOME"))
    )
    # Took away extraneous instructions (eg. --ours, --remote) for a simpler interface.

    args = parser.parse_args()

    # Can I do stdin input?

    if args.pk is not None:
        if args.start is None or args.end is None:
            raise Exception("Need start and end dates for pk data.")

        # Otherwise, let's do the pk checking.
        manage_pk_data(args.pk, args.start, args.end, args.localdir)
    elif args.sym and args.market:
        manage_custom_data(args.sym, args.market, args.start, args.end, args.localdir)
    else:
        manage_modelbuild_data(args.confs, args.localdir)

    # x = input()
    # print("Your input was")
    # print(x)


if __name__ == "__main__":
    main()
