#! /usr/bin/env python

import argparse
import json
import os
import requests
import sys
import threading
import time

DEBUG = False

# TODO: This could go into a library for use elsewhere.
class SingletonMeta(type):
    """
    Singleton metaclass. Thread-safe instantiation.
    """
    _instances = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]

class SupabaseCredentials(metaclass=SingletonMeta):

    _tables = {"position": None, "snapshot": None, "usermsg": None, "closing_print": None}
    _base_url = None
    _urls = {}
    _headers = {}
    _api_key = None
    _jwt = None
    _refresh_token = None
    _expires_at = None # expiration of JWT

    def __init__(self, testnet=False, credsfile="~/.creds/.Supabase.creds.json", auth=False):
        try:
            with open(os.path.expanduser(credsfile)) as f:
                supabase_secrets = json.load(f)
        except FileNotFoundError:
            sys.exit(f"Credentials file not found at {credsfile}. Please create it.")
        except Exception as e:
            sys.exit(f"Error loading credentials file ({credsfile}): {e}")

        project_id = supabase_secrets["project_id"]
        self._api_key = supabase_secrets["api_key"]
        prefix = "testnet" if testnet else "mainnet"
        self._base_url = f"https://{project_id}.supabase.co"
        for table in self._tables:
            table_key = f"{table}_table"
            if table not in ["snapshot", "closing_print"]:
                table_key = f"{prefix}_{table_key}"
            table_name = supabase_secrets[table_key]
            self._tables[table] = table_name
            self._urls[table] = f"{self._base_url}/rest/v1/{table_name}"
        self._headers["apikey"] = self._api_key

        # If using Supabase Auth, sign in with email/password to get JWT
        if auth:
            self._authenticate(supabase_secrets["email"], supabase_secrets["password"])
            self._headers["Authorization"] = f"Bearer {self._jwt}"
        # Otherwise, just use anon API key
        else:
            self._jwt = self._api_key

        if DEBUG:
            print(f"(DEBUG) using credsfile: {credsfile}, tables: {self._tables}")

    def _authenticate(self, email, password):
        """Authenticate with Supabase and save tokens"""
        auth_url = f"{self._base_url}/auth/v1/token?grant_type=password"
        payload = {
            "email": email,
            "password": password
        }
        r = requests.post(auth_url, headers=self._headers, json=payload)
        if r.status_code == 200:
            r_json = r.json()
            self._jwt = r_json["access_token"]
            self._refresh_token = r_json["refresh_token"]
            self._expires_at = int(time.time()) + int(r_json["expires_in"])
            if DEBUG:
                print("Successfully authenticated.")
                print("Expires in:", r_json["expires_in"])
        else:
            sys.exit(f"Authentication failed ({r.status_code}): {r.text}")

    def _refresh_auth(self):
        """Checks expiration of JWT and refreshes if necessary"""
        if self._expires_at is None or time.time() < self._expires_at - 10:
            # Nothing to refresh, or not expired yet
            return
        refresh_url = f"{self._base_url}/auth/v1/token?grant_type=refresh_token"
        payload = {
            "refresh_token": self._refresh_token
        }
        r = requests.post(refresh_url, headers=self._headers, json=payload)
        if r.status_code == 200:
            r_json = r.json()
            self._jwt = r_json["access_token"]
            self._refresh_token = r_json["refresh_token"]
            self._expires_at = int(time.time()) + int(r_json["expires_in"])
            self._headers["Authorization"] = f"Bearer {self._jwt}"
            if DEBUG:
                print("Successfully refreshed authentication.")
                print("Expires in:", r_json["expires_in"])
        else:
            sys.exit(f"Refresh failed ({r.status_code}): {r.text}")

    def get(self, table, params):
        self._refresh_auth()
        return requests.get(self._urls[table], headers=self._headers, params=params)

    def post(self, table, json_row):
        self._refresh_auth()
        return requests.post(self._urls[table], headers=self._headers, json=json_row)

    def delete(self, table, id):
        self._refresh_auth()
        params = { "id" : f"eq.{id}" }
        return requests.delete(self._urls[table], headers=self._headers, params=params)

    def get_table_name(self, table):
        return self._tables[table]

    async def get_client(self, auth=False):
        """Returns an AsyncClient. Only supports anon for now."""
        import supabase
        client: supabase.AsyncClient = await supabase.acreate_client(self._base_url, self._api_key)
        return client

# This lives here so we have the default credsfile in just 1 place.
def add_sb_creds_arg(parser):
    parser.add_argument(
        "--sb-creds", default="~/.creds/.Supabase.creds.json",
        help="Supabase creds file override"
    )
    parser.add_argument(
        "--vault", action="store_true",
        help="If given, sends things to the vault instead"
    )

def main():
    """Test loading credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--auth", action="store_true")
    add_sb_creds_arg(parser)
    args = parser.parse_args()
    global DEBUG
    DEBUG = True
    creds = SupabaseCredentials(args.testnet, args.sb_creds, args.auth)

if __name__ == "__main__":
    main()
