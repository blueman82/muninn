"""pctx command line entry point; later units add subcommands."""

import argparse
import sys

from pctx import __version__


def main(argv=None) -> int:
    # add_help/allow_abbrev off: "--version" is the only accepted input.
    parser = argparse.ArgumentParser(
        prog="pctx", add_help=False, allow_abbrev=False
    )
    parser.add_argument("--version", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2  # argparse already printed the usage error to stderr
    if not args.version:
        parser.print_usage(sys.stderr)
        return 2
    print(f"pctx {__version__}")
    return 0
