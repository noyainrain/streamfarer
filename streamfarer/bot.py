"""Bot logic.

.. data:: VERSION

   Current version.
"""

import sqlite3
from sqlite3 import Row
from typing import Annotated

from pydantic import Field, TypeAdapter

from . import context
from .services import LocalService, LocalServiceAdapter, Service, Twitch, TwitchAdapter
from .util import Connection

VERSION = '0.1.2'

class Bot:
    """Live stream traveling bot.

    .. attribute:: twitch

       Twitch adapter.

    .. attribute:: local

       Local livestreaming service adapter.

    .. attribute:: database_url

       SQLite database URL.
    """

    _AnyService = Annotated[Twitch | LocalService, Field(discriminator='type')]
    _ServiceModel: TypeAdapter[_AnyService] = TypeAdapter(_AnyService)

    def __init__(self, *, database_url: str = 'streamfarer.db') -> None:
        self.twitch = TwitchAdapter()
        self.local = LocalServiceAdapter()
        self.database_url = database_url
        self._db: Connection[Row] | None = None

        if context.bot.get(None):
            raise RuntimeError('Duplicate bot in task')
        context.bot.set(self)

    def get_services(self) -> list[Service]:
        """Get connected livestreaming services."""
        with self.transaction() as db:
            return [self._ServiceModel.validate_python(dict(row)) # type: ignore[misc]
                    for row in db.execute('SELECT * FROM services ORDER BY type')]

    def transaction(self) -> Connection[Row]:
        """Plumbing: Context manager to perform a transaction."""
        if not self._db:
            self._db = sqlite3.connect(self.database_url, factory=Connection)
            self._db.row_factory = Row
            self._update(self._db)
        return self._db

    def _update(self, db: Connection[Row]) -> None:
        with db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS services
                (type PRIMARY KEY, oauth_url, client_id, client_secret, token)
                """
            )
