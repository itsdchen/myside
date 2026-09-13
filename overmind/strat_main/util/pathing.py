"""
Some tools for working with pathing, relative to this dir or elsewhere.

"""

import os
from pathlib import Path
import getpass

# Find the TEMPLATES dir, relative to this path.
def templatedir():
    return os.path.join(
        Path(os.path.abspath(__file__)).parent.parent.parent, "TEMPLATES"
    )


def bindir(use_debug=False):
    parent_path = Path(os.path.abspath(__file__)).parent.parent.parent.parent
    if use_debug:
        return os.path.join(parent_path, "bin.debug")
    else:
        return os.path.join(parent_path, "bin")

def inheritdir():
    parent_path = os.path.join(
        Path(os.path.abspath(__file__)).parent.parent.parent, "sig_inheritance"
    )

    return parent_path

def tickerdir():
    parent_path = os.path.join(
        Path(os.path.abspath(__file__)).parent.parent.parent, "booktickers"
    )

    return parent_path


# Get the basic data dir.
def datadir():
    return "{}/tardis_datasets/gzpbf".format(os.getenv("HOME"))
