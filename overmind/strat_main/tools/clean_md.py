#! /usr/bin/env python3

"""
Performs hd cleanup.

Given a set of TradeAcademy conffiles and dates, give the user the option
of rm-ing all the files that aren't needed.

Basically... just a way to do housekeeping on the hard drive.

Example usage:

./clean_md.py --src /home/dchen/tardis_datasets/gzpbf  --confs ~/modeltrain/20230602/*/modelbuild.conf

"""

import os
import sys
import argparse
import shutil
import getpass


##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from util.chron import dates_list
import stratbuilder.TradeAcademy
#from tools import dl_tardis_lib

from util import mktdata


def check_data(confs_lst, local_dir):
    # These paths... are all relative to the pbf dirs.
    wanted_files = []
    existing_files = []
    extra_files = []

    wanted_books = []

    # OK, start reading through the confs and check out what data we might want.
    for one_ta_conf in confs_lst:
        syms_books_to_dates = stratbuilder.TradeAcademy.get_wanted_datas(one_ta_conf)
        # Now map these things to data files.

        for one_sb_pair, dates_lst in syms_books_to_dates.items():
            # Go through the channels for the market.
            mkt = one_sb_pair[1]
            wanted_books.append(mkt)
            mkt_channels = mktdata.books_to_channels[mkt]
            for one_channel in mkt_channels:
                for one_date in dates_lst:
                    # The wanted file is there.

                    # Note that the symbol name in these filenames is lowercased.
                    # ugh.

                    format_sym = one_sb_pair[0]
                    wanted_path = os.path.join(
                        mkt,
                        "{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                            SYM=format_sym,
                            CHANNEL=one_channel,
                            DATE=one_date,
                        ),
                    )
                    wanted_files.append(wanted_path)

    # dedup wanteds
    wanted_books = list(set(wanted_books))
    wanted_files = set(wanted_files)

    # OK, now let's enumerate all the files we do have.
    for one_book in wanted_books:
        book_dir = os.path.join(local_dir, one_book)
        book_files = os.listdir(book_dir)
        for one_book_file in book_files:
            book_file_path = os.path.join(one_book, one_book_file)
            existing_files.append(book_file_path)

    # OK, now get the difference I guess.
    existing_files = set(existing_files)

    extra_files = existing_files.difference(wanted_files)
    extra_files = list(extra_files)
    extra_files.sort()
    print("*" * 100)
    print("\n".join(extra_files))
    print(
        "The above {} files are not needed by the modelbuilds. Delete? [y/n]".format(
            len(extra_files)
        )
    )

    x = ""
    while x not in ["y", "n", "Y", "N"]:
        x = input()
    if x in ["y", "Y"]:
        # Copy them over.
        for one_extra_file in extra_files:
            extra_path = os.path.join(local_dir, one_extra_file)
            os.remove(extra_path)
        print("Done!")

    # Potentially delete.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confs", nargs="*")
    parser.add_argument(
        "--localdir", default="{}/tardis_datasets/gzpbf".format(os.getenv("HOME"))
    )
    args = parser.parse_args()

    check_data(args.confs, args.localdir)


if __name__ == "__main__":
    main()
