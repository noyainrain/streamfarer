"""Command-Line interface.

.. data:: VERSION

   Current version.
"""

from __future__ import annotations

from argparse import ArgumentParser, RawDescriptionHelpFormatter
import asyncio
from collections.abc import Awaitable, Callable
from sqlite3 import OperationalError
import sys
from textwrap import dedent

from . import context
from .bot import Bot
from .services import AuthorizationError

VERSION = '0.1.1'

class _Arguments:
    command: Callable[[_Arguments], Awaitable[int]]
    database_url: str
    client_id: str
    client_secret: str
    code: str

async def _service(args: _Arguments) -> int:
    # pylint: disable=unused-argument
    for service in context.bot.get().get_services():
        print(service)
    return 0

async def _connect_twitch(args: _Arguments) -> int:
    try:
        await context.bot.get().twitch.connect(args.client_id, args.client_secret, args.code,
                                               'http://localhost:8080/', None)
    except AuthorizationError:
        print('⚠️ Failed to get authorization with CLIENT_ID, CLIENT_SECRET and CODE')
        return 1
    print('✅ Connected Twitch')
    return 0

async def main(*args: str) -> int:
    """Run the bot with command-line arguments *args* and return the exit status."""
    parser = ArgumentParser(
        prog='python3 -m streamfarer',
        description='Live stream traveling bot and tiny art experiment. ⛵',
        epilog=f'Streamfarer {VERSION}')
    parser.add_argument('--database-url', default='streamfarer.db', help='SQLite database URL')
    subparsers = parser.add_subparsers(required=True)

    service_help = 'Show connected livestreaming services.'
    service_parser = subparsers.add_parser('service', description=service_help, help=service_help)
    service_parser.set_defaults(command=_service)

    connect_help = 'Connect a livestreaming service.'
    connect_parser = subparsers.add_parser('connect', description=connect_help, help=connect_help)
    connect_subparsers = connect_parser.add_subparsers(required=True)

    twitch_help = 'Connect Twitch.'
    twitch_description = dedent(
        f"""\
        {twitch_help}

        Prerequisites:

        1. Create a bot account at https://www.twitch.tv/signup.
        2. Register an application at https://dev.twitch.tv/console/apps/create and set OAuth \
Redirect URLs to URL. Obtain CLIENT_ID and CLIENT_SECRET.
        3. Authorize the application with the bot account at \
https://id.twitch.tv/oauth2/authorize?client_id=CLIENT_ID&redirect_uri=URL&response_type=code. \
Obtain CODE from the address bar.

        URL is http://localhost:8080/.
        """
    )
    twitch_parser = connect_subparsers.add_parser(
        'twitch', description=twitch_description, formatter_class=RawDescriptionHelpFormatter,
        help=twitch_help)
    twitch_parser.add_argument('client_id', help='application client ID', metavar='CLIENT_ID')
    twitch_parser.add_argument('client_secret', help='application client secret',
                               metavar='CLIENT_SECRET')
    twitch_parser.add_argument('code', help='authorization code', metavar='CODE')
    twitch_parser.set_defaults(command=_connect_twitch)

    try:
        parsed_args = parser.parse_args(args[1:], namespace=_Arguments())
    except SystemExit as e:
        assert isinstance(e.code, int)
        return e.code

    Bot(database_url=parsed_args.database_url)
    try:
        return await parsed_args.command(parsed_args)
    except OSError as e:
        print(f'⚠️ Failed to communicate with the livestreaming service ({e})')
        return 1
    except OperationalError as e:
        print(f'⚠️ Failed to access the database ({e})')
        return 1

if __name__ == '__main__':
    sys.exit(asyncio.run(main(*sys.argv)))
