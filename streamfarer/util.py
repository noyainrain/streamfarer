"""Various utilities."""

from asyncio import CancelledError, FIRST_COMPLETED, Task, create_task, gather, wait
from argparse import ArgumentTypeError
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Mapping
import errno
import json
from json import JSONDecodeError
import random
from string import ascii_lowercase
import sqlite3
from sqlite3 import OperationalError
from typing import Generic, Protocol, TypeVar
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import tornado
from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest, HTTPResponse
from tornado.simple_httpclient import HTTPStreamClosedError, HTTPTimeoutError

T_co = TypeVar("T_co", covariant=True)
M_co = TypeVar('M_co', bound=Mapping[str, object], covariant=True)

RowFactory = Callable[[sqlite3.Cursor, tuple[object, ...]], T_co]

class SupportsLenAndGetItem(Protocol):
    # pylint: disable=missing-class-docstring
    def __len__(self) -> int: ...
    def __getitem__(self, key: int) -> object: ...

def randstr(length: int = 16, *, characters: str = ascii_lowercase) -> str:
    """Generate a random string with the given *length*.

    The result is comprised of the given set of *characters*.
    """
    # To be suitable for IDs, the default length l is chosen such that
    # 1 - exp(-n * (n - 1) / (2 * c ** l)) <= p, where the probability of collision p = 1‰, the
    # presumed number of entities n = 1000000 and the size of the character set c = 26. (see
    # https://en.wikipedia.org/wiki/Birthday_problem)
    return ''.join(random.choice(characters) for _ in range(length))

def urlorigin(url: str) -> str:
    """Get the origin of a *url*."""
    components = urlsplit(url)
    return urlunsplit((components.scheme, components.netloc, '', '', ''))

async def cancel(task: Task[object]) -> None:
    """Cancel a *task*."""
    task.cancel()
    try:
        await task
    except CancelledError:
        pass

async def amerge(*iterables: AsyncIterable[T_co]) -> AsyncGenerator[T_co]:
    """Merge multiple asynchronous *iterables* into a single generator.

    The *iterables* are taken over by the generator, once it starts: When it stops, any input
    iterators may be cancelled and any input generators are closed.
    """
    # While AsyncGenerator.__anext__() is a coroutine, in general Awaitable is returned by
    # AsyncIterator.__anext__()
    async def anext_coroutine(it: AsyncIterator[T_co]) -> T_co:
        return await anext(it)
    iterators = {aiter(iterable) for iterable in iterables}
    tasks: dict[Task[T_co], AsyncIterator[T_co]] = {}

    try:
        while iterators:
            for it in iterators - set(tasks.values()):
                tasks[create_task(anext_coroutine(it))] = it
            done, _ = await wait(tasks, return_when=FIRST_COMPLETED)
            for task in done:
                it = tasks.pop(task)
                try:
                    yield task.result()
                except StopAsyncIteration:
                    iterators.remove(it)
    finally:
        await gather(*(cancel(task) for task in tasks))
        await gather(
            *(it.aclose() # type: ignore[misc]
              for it in iterators if isinstance(it, AsyncGenerator)))

def nested(mapping: Mapping[str, object], name: str, *, separator: str = '_') -> dict[str, object]:
    """Return a dictionary with prefixed keys from a *mapping* merged into a nested dictionary.

    The relevant prefix is a *name* along with a *separator*. The nested dictionary has the given
    *name* and unprefixed items.
    """
    sub: dict[str, object] = {}
    result: dict[str, object] = {name: sub}
    prefix = f'{name}{separator}'
    for key, value in mapping.items():
        sub_key = key.removeprefix(prefix)
        if sub_key != key:
            sub[sub_key] = value
        else:
            result[key] = value
    return result

def text(arg: str) -> str:
    """Convert a command-line argument *arg* to a text with visible characters."""
    arg = arg.strip()
    if not arg:
        raise ArgumentTypeError()
    return arg

def add_column(db: sqlite3.Connection, table: str, column: str, value: object = None) -> None:
    """Add a *column* to a *table* in a database *db* if it does not exist yet.

    Existing rows are initialized with a *value*.
    """
    table = table.replace('"', '""')
    column = column.replace('"', '""')
    try:
        db.execute(f'ALTER TABLE "{table}" ADD "{column}"')
    except OperationalError as e:
        if column not in str(e):
            raise
    else:
        if value is not None:
            db.execute(f'UPDATE "{table}" SET "{column}" = ?', (value, ))

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

    @staticmethod
    def _with_response(error: Exception, response: HTTPResponse) -> Exception:
        error.add_note(response.body.decode(errors='replace'))
        return error

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
        headers = dict(self.headers)
        url = urljoin(self.url, f'{endpoint}?{urlencode(query)}')
        body = None
        if data is not None:
            body = json.dumps(data)
            headers['Content-Type'] = 'application/json'

        try:
            response = await AsyncHTTPClient().fetch(
                HTTPRequest(url, method=method, headers=headers, body=body,
                            allow_nonstandard_methods=True))
        except HTTPStreamClosedError as e:
            raise ConnectionResetError(errno.ECONNRESET, f'{e} for {method} {url}') from e
        except HTTPTimeoutError as e:
            raise TimeoutError(errno.ETIMEDOUT, f'{e} for {method} {url}') from e
        except HTTPClientError as e:
            assert e.response
            response = e.response

        if response.code >= 500:
            raise self._with_response(
                OSError(errno.EPROTO, f'Error {response.code} for {method} {url}'),
                response)

        try:
            result: object = json.loads(response.body)
            if not isinstance(result, dict):
                raise TypeError()
        except (UnicodeDecodeError, JSONDecodeError, TypeError) as e:
            raise self._with_response(
                OSError(errno.EPROTO, f'Bad response format for {method} {url}'), response
            ) from e

        if not 200 <= response.code < 300:
            raise self._with_response(
                WebAPI.Error(f'Error {response.code} for {method} {url}', result, response.code),
                response)

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

class Application(tornado.web.Application, Generic[M_co]):
    """Web application with type annotations for settings."""

    # __init__() cannot be annotated adequately, because Unpack does not support type variables yet
    # (see https://github.com/python/typing/issues/1399)

    settings: M_co # type: ignore[assignment]

class RequestHandler(tornado.web.RequestHandler, Generic[M_co]):
    """HTTP request handler with type annotations for settings."""

    settings: M_co # type: ignore[assignment]

class UIModule(tornado.web.UIModule, Generic[M_co]):
    """UI module with type annotations for settings."""

    handler: RequestHandler[M_co]
