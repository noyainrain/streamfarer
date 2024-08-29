"""Command-Line interface.

.. data:: VERSION

   Current version.
"""

from argparse import ArgumentParser
import asyncio
import sys

VERSION = '0.1.0'

async def main(*args: str) -> int:
    """Run the bot with command-line arguments *args* and return the exit status."""
    parser = ArgumentParser(
        prog='python3 -m streamfarer',
        description='Live stream traveling bot and tiny art experiment. ⛵',
        epilog=f'Streamfarer {VERSION}')
    # Work around argparse error if no subcommand is defined (see
    # https://github.com/python/cpython/issues/73484)
    parser.add_subparsers(required=True, metavar='COMMAND')
    try:
        parser.parse_args(args[1:])
    except SystemExit as e:
        assert isinstance(e.code, int)
        return e.code
    return 0

if __name__ == '__main__':
    sys.exit(asyncio.run(main(*sys.argv)))
