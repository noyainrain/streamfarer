"""Livestreaming services."""

from asyncio import create_subprocess_exec
from asyncio.subprocess import Process, PIPE
from http import HTTPStatus
from typing import Generic, Literal, ParamSpec, TypeVar

from pydantic import BaseModel

from . import context
from .util import WebAPI

P = ParamSpec('P')
S = TypeVar('S', bound='Service')

class AuthorizationError(Exception):
    """Raised when getting authorization fails."""

class Service(BaseModel): # type: ignore[misc]
    """Connected livestreaming service.

    .. attribute:: type

       Service type.
    """

    type: str

class ServiceAdapter(Generic[P, S]):
    """Livestreaming service adapter."""

    async def connect(self, *args: P.args, **kwargs: P.kwargs) -> S:
        """Connect a livestreaming service.

        If getting authorization fails, an :exc:`AuthorizationError` is raised. If there is a
        problem communicating with the livestreaming service, an :exc:`OSError` is raised.
        """
        service = await self.authorize(*args, **kwargs)
        with context.bot.get().transaction() as db:
            data: dict[str, object] = service.model_dump()
            names = ', '.join(data)
            values = list(data.values())
            placeholders = ', '.join(['?'] * len(data))
            db.execute(f'INSERT OR REPLACE INTO services ({names}) VALUES ({placeholders})', values)
        return service

    async def authorize(self, *args: P.args, **kwargs: P.kwargs) -> S:
        """Get authorization and construct the service."""
        raise NotImplementedError()

class LocalService(Service): # type: ignore[misc]
    """Local livestreaming service."""

    type: Literal['local']

    def __str__(self) -> str:
        return '📺 Local livestreaming service'

class LocalServiceAdapter(ServiceAdapter[[], LocalService]):
    """Local livestreaming service adapter."""

    async def authorize(self) -> LocalService:
        return LocalService(type='local')

class Twitch(Service): # type: ignore[misc]
    """Twitch connection."""

    type: Literal['twitch']
    """OAuth URL."""
    oauth_url: str
    """Application client ID."""
    client_id: str
    """Application client secret."""
    client_secret: str
    """Access token."""
    token: str

    @staticmethod
    async def start_cli(*args: str, signal: str | None = None) -> Process:
        """Plumbing: Start Twitch CLI with command-line arguments *args*.

        Optionally, a start *signal* is awaited on stderr.

        If there is a problem starting Twitch CLI, an :exc:`OSError` is raised.
        """
        process = await create_subprocess_exec('twitch', *args, stderr=PIPE)
        if signal:
            assert process.stderr
            async for line in process.stderr:
                if signal in line.decode():
                    break
        return process

    def __str__(self) -> str:
        return f'📺 Twitch via application {self.client_id}'

class TwitchAdapter(ServiceAdapter[[str, str, str, str, str | None], Twitch]):
    """Twitch adapter.

    *code* is an authorization code obtained via the *redirect_uri*. For a mock server, it is the ID
    of the authorizing user.
    """

    _PRODUCTION_OAUTH_URL = 'https://id.twitch.tv/oauth2/'

    class _Token(BaseModel): # type: ignore[misc]
        access_token: str

    async def authorize(self, client_id: str, client_secret: str, code: str, redirect_uri: str,
                        oauth_url: str | None) -> Twitch:
        oauth = WebAPI(oauth_url or self._PRODUCTION_OAUTH_URL)
        if oauth.url == self._PRODUCTION_OAUTH_URL:
            endpoint = 'token'
            query = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri}
        else:
            endpoint = 'authorize'
            query={'grant_type': 'user_token', 'user_id': code}

        try:
            token = TwitchAdapter._Token.model_validate(
                await oauth.call(
                    'POST', endpoint,
                    query={**query, 'client_id': client_id, 'client_secret': client_secret}))
        except WebAPI.Error as e:
            if e.status == HTTPStatus.BAD_REQUEST:
                raise AuthorizationError(f'Failed authorization of {client_id}') from e
            raise

        return Twitch(type='twitch', oauth_url=oauth.url, client_id=client_id,
                      client_secret=client_secret, token=token.access_token)
