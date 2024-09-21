"""Command-Line interface."""

from __future__ import annotations

import argparse
from argparse import ArgumentParser, RawDescriptionHelpFormatter
import asyncio
from asyncio import CancelledError, Event
from collections.abc import Awaitable, Callable
from configparser import ConfigParser, ParsingError
from importlib import resources
import logging
from logging import getLogger
import signal
from signal import getsignal, SIGINT, SIGTERM
from sqlite3 import OperationalError
import sys
from textwrap import dedent

from . import context
from .bot import Bot, VERSION
from .server import serve
from .services import AuthorizationError

class _Arguments:
    command: Callable[[_Arguments], Awaitable[int]]
    database_url: str
    client_id: str
    client_secret: str
    code: str
    host: str
    port: int

async def _run(args: _Arguments) -> int:
    logger = getLogger(__name__)

    try:
        server = serve(host=args.host, port=args.port)
    except OSError as e:
        print(f'⚠️ Failed to start the server ({e})', file=sys.stderr)
        return 1
    logger.info('Started the server at %s', server.url)

    try:
        await Event().wait()
    finally:
        server.close()
        logger.info('Stopped the server')
    return 0

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
        print('⚠️ Failed to get authorization with CLIENT_ID, CLIENT_SECRET and CODE',
              file=sys.stderr)
        return 1
    print('✅ Connected Twitch', file=sys.stderr)
    return 0

async def main(*args: str) -> int:
    """Execute a bot command with command-line arguments *args* and return the exit status."""
    signal.signal(SIGTERM, getsignal(SIGINT))

    parser = ArgumentParser(
        prog='python3 -m streamfarer',
        description='Live stream traveling bot and tiny art experiment. ⛵',
        epilog=f'Streamfarer {VERSION}', argument_default=argparse.SUPPRESS)
    parser.add_argument('--database-url', help='SQLite database URL')
    subparsers = parser.add_subparsers(required=True)

    run_help = 'Run the bot.'
    run_parser = subparsers.add_parser('run', description=run_help, help=run_help)
    run_parser.set_defaults(command=_run)

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

    config = ConfigParser(strict=False, interpolation=None)
    with (resources.files(f'{__package__}.res') / 'default.ini').open() as f:
        config.read_file(f)
    try:
        config.read('streamfarer.ini')
    except ParsingError as e:
        print(f'⚠️ Failed to load the config file ({e})', file=sys.stderr)
        return 1

    options = config['streamfarer']
    if not hasattr(parsed_args, 'database_url'):
        parsed_args.database_url = options['database_url']
    parsed_args.host = options['host']
    try:
        parsed_args.port = options.getint('port')
    except ValueError:
        print('⚠️ Failed to load the config file (Bad port type)', file=sys.stderr)
        return 1

    logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s %(message)s', level=logging.INFO)
    getLogger('asyncio').setLevel(logging.WARNING)

    Bot(database_url=parsed_args.database_url)
    try:
        return await parsed_args.command(parsed_args)
    except CancelledError:
        print('cancelled', file=sys.stderr)
        return 2
    except OSError as e:
        print(f'⚠️ Failed to communicate with the livestreaming service ({e})', file=sys.stderr)
        return 1
    except OperationalError as e:
        print(f'⚠️ Failed to access the database ({e})', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(asyncio.run(main(*sys.argv)))
