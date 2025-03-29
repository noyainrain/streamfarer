"""Web server."""

from contextlib import AbstractContextManager
from importlib import resources
import logging
from logging import getLogger
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from pydantic import TypeAdapter
from tornado.httpserver import HTTPServer

from . import context
from .bot import VERSION
from .core import format_datetime, format_duration
from .services import Channel, Twitch
from .util import Application, RequestHandler, UIModule, urlorigin

_FontsModel = TypeAdapter(dict[str, str])

class _Settings(TypedDict):
    url: str
    fonts: dict[str, str]

class TwitchPlayer(UIModule[_Settings]):
    """Twitch video player."""

    def render(self, channel: Channel, service: Twitch) -> str:
        html = self.render_string("twitchplayer.html", login=service.get_login(channel.url),
                                  host=urlsplit(self.handler.settings['url']).hostname)
        return html.decode()

class _Index(RequestHandler[_Settings]):
    def get(self) -> None:
        # pylint: disable=missing-function-docstring
        bot = context.bot.get()
        description='Live stream traveling bot and tiny art experiment.'
        try:
            journey = bot.get_journeys()[0]
            stays = journey.get_stays()
            service = bot.get_service_at(urlorigin(stays[0].channel.url))
            title = journey.title
            if journey.description:
                description = journey.description
        except IndexError:
            journey = None
            stays = None
            service = None
            title = None

        self.set_header('Cache-Control', 'no-cache')
        self.render(
            'index.html', journey=journey, stays=stays, service=service, url=self.settings['url'],
            title=title, description=description, fonts=self.settings['fonts'], version=VERSION,
            format_datetime=format_datetime, format_duration=format_duration)

def _log(handler: RequestHandler[_Settings]) -> None:
    request = handler.request
    client: str | None = request.remote_ip
    status = handler.get_status()
    getLogger(__name__).log(
        logging.WARNING if status >= 400 else logging.INFO, '%s %s %s %d %.2fms', client,
        request.method, request.uri, status, request.request_time() * 1000)

class Server:
    """Bot web server."""

    def __init__(self, _http: HTTPServer, _res_directory: AbstractContextManager[Path]) -> None:
        self._http = _http
        self._res_directory = _res_directory

    @property
    def url(self) -> str:
        """Server URL."""
        assert isinstance(self._http.request_callback, Application) # type: ignore[misc]
        app: Application[_Settings] = self._http.request_callback
        return app.settings['url']

    def close(self) -> None:
        """Stop the server."""
        self._http.stop()
        self._res_directory.__exit__(None, None, None)

def server_url(host: str, port: int) -> str:
    """Construct a web server URL from its *host* and *port*."""
    host = host or 'localhost'
    return f'http://{host}:{port}/'

def serve(*, host: str = '', port: int = 8080, url: str | None = None) -> Server:
    """Serve a web UI for the active bot.

    Incoming connections are listened for on the given *host* and *port*. *url* is the public URL.

    If there is a problem starting the server, an :exc:`OSError` is raised.
    """
    res = resources.files(f'{__package__}.res')
    fonts = _FontsModel.validate_json((res / 'static/vendor/unicode.json').read_text())

    res_directory = resources.as_file(res)
    res_path = res_directory.__enter__()
    try:
        ui_modules = {TwitchPlayer.__name__: TwitchPlayer}
        app: Application[_Settings] = Application(
            [('/', _Index)], compress_response=True, log_function=_log, # type: ignore[misc]
            ui_modules=ui_modules, template_path=res_path, static_path=res_path / 'static',
            url=server_url(host, port) if url is None else url, fonts=fonts)

        http = app.listen(port, address=host, xheaders=True)
        return Server(http, res_directory)
    except:
        res_directory.__exit__(None, None, None)
        raise
