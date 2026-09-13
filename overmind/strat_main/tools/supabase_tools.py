#! /usr/bin/env python

"""
Access utils for our supabase instance.

If you want to insert a row, pass in a file like this:
[
 {
        "strat_id": "test1",
        "symbol": "unit:DCHEN",
        "global_pos": 10,
        "my_pos": 0,
        "my_max_pos": 50,
        "my_inherit_amount": 0,
    },
     {
        "strat_id": "test2",
        "symbol": "unit:DCHEN",
        "global_pos": 30,
        "my_pos": 10,
        "my_max_pos": 150,
        "my_inherit_amount": 0,
    }
]



"""

import argparse
import commentjson
import getpass
import json
import os
import requests
import sys
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import sbcreds


def get_tables():
    # OK, I guess the information_schema is not public?
    # SO for now I'm going to just print this out from a static list
    # and update later. However, if I'm logged in, can check the tables from here:
    # https://supabase.com/dashboard/project/lolgjbnpttwqdqtmgwux/editor/17271?schema=public

    tables = [
        "positions_1",
        "invigilator_1",
    ]
    print(tables)
    pass


def read_table(table, testnet, limit, credsfile, auth):
    # Read from the table.
    params = {
        "select": "*",
        "limit": str(limit),
        "order": "id.desc",  # optional, by some column
    }
    creds = sbcreds.SupabaseCredentials(testnet, credsfile, auth)
    r = creds.get(table, params)
    if r.status_code == 200:
        print("rows:", json.dumps(r.json(), indent=2))
    else:
        print("error:", r.status_code, r.text)


def write_table(table, testnet, rows_json_file, credsfile, auth):
    # Read the file.
    with open(rows_json_file) as f:
        rows_json = commentjson.loads(f.read())
    creds = sbcreds.SupabaseCredentials(testnet, credsfile, auth)
    for idx, one_row in enumerate(rows_json):
        # Write the row.
        print("Inserting row {} of {}".format(idx + 1, len(rows_json)))
        r = creds.post(table, one_row)
        print("status:", r.status_code)
        # Sleep a little.
        time.sleep(0.5)


def delete_row(table, testnet, id, credsfile, auth):
    print("Deleting row {}".format(id))
    creds = sbcreds.SupabaseCredentials(testnet, credsfile, auth)
    r = creds.delete(table, id)
    print("status:", r.status_code)


def main():
    parser = argparse.ArgumentParser()

    # Need to specify which table for these actions.
    parser.add_argument("--table", required=True)
    parser.add_argument("--testnet", action="store_true")

    # read, write, delete
    parser.add_argument("--mode", required=True)

    # Supabase Auth or just anon
    parser.add_argument("--auth", action="store_true")

    # If delete, we need the id.
    parser.add_argument("--id")

    # If we're writing new rows, define those in a json.
    parser.add_argument("--rows_json")

    # If we're reading, specify row limit
    parser.add_argument("--limit", default=100, type=int)

    sbcreds.add_sb_creds_arg(parser)

    args = parser.parse_args()

    if args.mode == "tables":
        get_tables()
    elif args.mode == "read":
        if args.table is None:
            sys.exit("Error: need to specify a table to read from")
        if args.limit is None:
            sys.exit("Error: need to specify a limit to read from")
        read_table(args.table, args.testnet, args.limit, args.sb_creds, args.auth)
    elif args.mode == "write":
        if args.table is None:
            sys.exit("Error: need to specify a table to write to")
        if args.rows_json is None:
            sys.exit("Error: need to specify a rows_json file to write to")
        write_table(args.table, args.testnet, args.rows_json, args.sb_creds, args.auth)
    elif args.mode == "delete":
        if args.id is None:
            sys.exit("Error: need to specify id to delete")
        delete_row(args.table, args.testnet, args.id, args.sb_creds, args.auth)


if __name__ == "__main__":
    main()
