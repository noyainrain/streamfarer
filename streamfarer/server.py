"""Web server."""

from contextlib import AbstractContextManager
from importlib import resources
import logging
from logging import getLogger
from pathlib import Path

from tornado.httpserver import HTTPServer
from tornado.web import Application, RequestHandler

from . import context
from .bot import VERSION
from .core import format_datetime, format_duration

class _Index(RequestHandler):
    def get(self) -> None:
        # pylint: disable=missing-function-docstring
        try:
            journey = context.bot.get().get_journeys()[0]
            stays = journey.get_stays()
        except IndexError:
            journey = None
            stays = None
        self.render('index.html', journey=journey, stays=stays, version=VERSION,
                    format_datetime=format_datetime, format_duration=format_duration)

def _log(handler: RequestHandler) -> None:
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
        settings: dict[str, object] = self._http.request_callback.settings
        url = settings['url']
        assert isinstance(url, str)
        return url

    def close(self) -> None:
        """Stop the server."""
        self._http.stop()
        self._res_directory.__exit__(None, None, None)

def serve(*, host: str = '', port: int = 8080) -> Server:
    """Serve a web UI for the active bot.

    Incoming connections are listened for on *host* and *port*.

    If there is a problem starting the server, an :exc:`OSError` is raised.
    """
    res_directory = resources.as_file(resources.files(f'{__package__}.res'))
    res_path = res_directory.__enter__()

    try:
        url_host = host or 'localhost'
        url = f'http://{url_host}:{port}/'
        app = Application([('/', _Index)], compress_response=True, # type: ignore[misc]
                          log_function=_log, template_path=res_path, url=url)
        http = app.listen(port, address=host, xheaders=True)
        return Server(http, res_directory)
    except:
        res_directory.__exit__(None, None, None)
        raise
