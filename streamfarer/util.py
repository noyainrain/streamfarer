"""Various utilities."""

from collections.abc import Callable, Mapping
import errno
import json
from json import JSONDecodeError
import sqlite3
from typing import Generic, Protocol, TypeVar
from urllib.parse import urlencode, urljoin, urlsplit

from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest

T_co = TypeVar("T_co", covariant=True)

RowFactory = Callable[[sqlite3.Cursor, tuple[object, ...]], T_co]

class SupportsLenAndGetItem(Protocol):
    # pylint: disable=missing-class-docstring
    def __len__(self) -> int: ...
    def __getitem__(self, key: int) -> object: ...

class WebAPI:
    """Simple JSON REST API client.

    .. attribute:: url

       Base URL of the web API. It is an HTTP/S URL with no components after path.

    .. attribute:: query

       Default query parameters for web API calls.

    .. attribute:: headers

       Supplied HTTP headers for web API calls.
    """

    class Error(Exception):
        """Raised when there is a web API error."""

        args: tuple[str, dict[str, object], int]

        def __init__(self, message: str, error: dict[str, object], status: int) -> None:
            super().__init__(message, error, status)

        @property
        def error(self) -> dict[str, object]:
            """Error object."""
            return self.args[1]

        @property
        def status(self) -> int:
            """Associated HTTP status code."""
            return self.args[2]

        def __str__(self) -> str:
            return self.args[0]

    def __init__(self, url: str, *, query: Mapping[str, str] = {},
                 headers: Mapping[str, str] = {}) -> None:
        # pylint: disable=dangerous-default-value
        urlparts = urlsplit(url)
        if not (urlparts.scheme in {'http', 'https'} and urlparts.netloc and
                not any([urlparts.query, urlparts.fragment])):
            raise ValueError(f'Bad url {url}')
        self.url = url
        self.query = dict(query)
        self.headers = dict(headers)
        self._urlparts = urlparts

    async def call(self, method: str, endpoint: str, *, data: Mapping[str, object] | None = None,
                   query: Mapping[str, str] = {}) -> dict[str, object]:
        """Call a *method* on an *endpoint* and return the result.

        *method* is an HTTP method (e.g. ``GET`` or ``POST``). *endpoint* is a URL relative to
        :attr:`url` with no components after path. *data* is the supplied JSON payload. *query* are
        the supplied query parameters.

        If there is an error, a :exc:`WebAPI.Error` is raised. If there is a problem communicating
        with the web API, an :exc:`OSError` is raised.
        """
        # pylint: disable=dangerous-default-value
        urlparts = urlsplit(endpoint)
        if not (urlparts.scheme in {'', 'http', 'https'} and
                not any([urlparts.query, urlparts.fragment])):
            raise ValueError(f'Bad endpoint {endpoint}')

        query = {**self.query, **query}
        url = urljoin(self.url, f'{endpoint}?{urlencode(query)}')
        body = None if data is None else json.dumps(data)

        try:
            response = await AsyncHTTPClient().fetch(
                HTTPRequest(url, method=method, headers=self.headers, body=body,
                            allow_nonstandard_methods=True))
        except HTTPClientError as e:
            if e.code >= 500:
                # Also takes care of HTTPStreamClosedError and HTTPTimeoutError
                raise OSError(errno.EPROTO, f'{e} for {method} {url}') from e
            assert e.response
            response = e.response

        try:
            assert response.buffer
            result: object = json.load(response.buffer)
            if not isinstance(result, dict):
                raise TypeError()
        except (UnicodeDecodeError, JSONDecodeError, TypeError) as e:
            raise OSError(errno.EPROTO, f'Bad response format for {method} {url}') from e

        if not 200 <= response.code < 300:
            raise self.Error(f'Error {response.code} for {method} {url}', result, response.code)
        return result

class Cursor(sqlite3.Cursor, Generic[T_co]):
    """Database cursor with row type annotations."""

    row_factory: RowFactory[T_co] # type: ignore[assignment]

    def __next__(self) -> T_co:
        row: T_co = super().__next__()
        return row

class Connection(sqlite3.Connection, Generic[T_co]):
    """Database connection with row type annotations."""

    row_factory: RowFactory[T_co]

    def execute(self, sql: str,
                parameters: SupportsLenAndGetItem | Mapping[str, object] = ()) -> Cursor[T_co]:
        # pylint: disable=missing-function-docstring
        return super().cursor(factory=Cursor).execute(sql, parameters)
