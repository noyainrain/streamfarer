"""Journey logic."""

from __future__ import annotations

from datetime import datetime, timedelta
from sqlite3 import IntegrityError
from typing import Self

from pydantic import BaseModel, validate_call

from . import context
from .core import Event, Text, format_datetime
from .services import Channel
from .util import nested, randstr

class OngoingJourneyError(Exception):
    """Raised when an action cannot be performed due to an ongoing journey."""

class EndedJourneyError(Exception):
    """Raised when an action cannot be performed because the journey has ended."""

class PastJourneyError(Exception):
    """Raised when an action cannot be performed because the journey is not the latest one."""

class Stay(BaseModel): # type: ignore[misc]
    """Stay at a live stream on a journey.

    .. attribute:: id

       Unique stay ID.

    .. attribute:: journey_id

       Related journey ID.

    .. attribute:: channel

       Live stream channel.

    .. attribute:: start_time

       Time the stay started.

    .. attribute:: end_time

       Time the stay ended. ``None`` if the stay is ongoing.
    """

    id: str
    journey_id: str
    channel: Channel
    start_time: datetime
    end_time: datetime | None

    @property
    def duration(self) -> timedelta:
        """Duration of the stay."""
        return (self.end_time or context.bot.get().now()) - self.start_time

    def get_journey(self) -> Journey:
        """Get the related journey."""
        return context.bot.get().get_journey(self.journey_id)

class Journey(BaseModel): # type: ignore[misc]
    """Journey through live streams.

    .. attribute:: id

       Unique journey ID.

    .. attribute:: title

       Journey title.

    .. attribute:: start_time

       Time the journey started.

    .. attribute:: end_time

       Time the journey ended. `None` if the journey is ongoing.

    .. attribute:: deleted

       Whether the ended journey has been deleted.
    """

    id: str
    title: Text
    start_time: datetime
    end_time: datetime | None
    deleted: bool

    @property
    def duration(self) -> timedelta:
        """Duration of the journey."""
        return (self.end_time or context.bot.get().now()) - self.start_time

    def get_stays(self) -> list[Stay]:
        """Get all stays on the journey, latest first."""
        with context.bot.get().transaction() as db:
            rows = db.execute('SELECT * FROM stays WHERE journey_id = ? ORDER BY start_time DESC',
                              (self.id, ))
            return [Stay.model_validate(nested(dict(row), 'channel')) for row in rows]

    def get_latest_stay(self) -> Stay:
        """Get the latest stay on the journey."""
        with context.bot.get().transaction() as db:
            rows = db.execute(
                'SELECT * FROM stays WHERE journey_id = ? ORDER BY start_time DESC LIMIT 1',
                (self.id, ))
            return Stay.model_validate(nested(dict(next(rows)), 'channel'))

    @validate_call # type: ignore[misc]
    def edit(self, title: Text) -> Self:
        """Edit the journey *title*."""
        with context.bot.get().transaction() as db:
            rows = db.execute(
                'UPDATE journeys SET title = ? WHERE id = ? AND deleted = 0 RETURNING *',
                (title, self.id))
            try:
                return self.model_validate(dict(next(rows)))
            except StopIteration:
                raise KeyError(self.id) from None

    def end(self) -> Self:
        """End the ongoing journey."""
        bot = context.bot.get()
        with bot.transaction() as db:
            end_time = bot.now().isoformat()
            rows = db.execute(
                """
                UPDATE journeys SET end_time = coalesce(end_time, ?) WHERE id = ? AND deleted = 0
                RETURNING *
                """,
                (end_time, self.id))
            db.execute('UPDATE stays SET end_time = ? WHERE journey_id = ? AND end_time IS NULL',
                       (end_time, self.id))
            try:
                journey = self.model_validate(dict(next(rows)))
            except StopIteration:
                raise KeyError(self.id) from None
        bot.dispatch_event(Event(type='journey-end'))
        return journey

    async def resume(self) -> Journey:
        """Resume the ended journey.

        If authentication with the livestreaming service fails, an :exc:`AuthenticationError` is
        raised. If there is a problem communicating with the livestreaming service, an
        :exc:`OSError` is raised.
        """
        bot = context.bot.get()
        await bot.stream(self.get_latest_stay().channel.url)

        with bot.transaction() as db:
            try:
                rows = db.execute(
                    """
                    UPDATE journeys SET end_time = NULL WHERE id = ? AND deleted = 0
                    RETURNING
                        *,
                        (SELECT id = ? FROM journeys ORDER BY start_time DESC LIMIT 1) AS latest
                    """,
                    (self.id, self.id))
                row = dict(next(rows))
                if not row['latest']:
                    raise PastJourneyError(f'Past journey {self.id}')
                journey = Journey.model_validate(row)
            except IntegrityError as e:
                if 'journeys_end_time_index' in str(e):
                    raise PastJourneyError(f'Past journey {self.id}') from None
                raise
            except StopIteration:
                raise KeyError(self.id) from None
            db.execute(
                """
                UPDATE stays SET end_time = NULL WHERE journey_id = ? ORDER BY start_time DESC
                LIMIT 1
                """,
                (self.id, ))
        bot.dispatch_event(Event(type='journey-resume'))
        return journey

    def delete(self) -> None:
        """Delete the ended journey."""
        with context.bot.get().transaction() as db:
            try:
                db.execute('UPDATE journeys SET deleted = 1 WHERE id = ?', (self.id, ))
            except IntegrityError as e:
                if 'deleted_end_time_check' in str(e):
                    raise OngoingJourneyError(f'Ongoing journey {self.id}') from None
                raise

    async def travel_on(self, channel_url: str) -> Stay:
        """End the current stay and travel on to the given *channel_url*.

        If authentication with the livestreaming service fails, an :exc:`AuthenticationError` is
        raised. If there is a problem communicating with the livestreaming service, an
        :exc:`OSError` is raised.
        """
        bot = context.bot.get()
        stream = await bot.stream(channel_url)

        with bot.transaction() as db:
            now = bot.now().isoformat()
            # Simplify the query by handling deleted as ended journeys
            rows = db.execute(
                'UPDATE stays SET end_time = ? WHERE journey_id = ? AND end_time IS NULL',
                (now, self.id))
            if not rows.rowcount:
                raise EndedJourneyError(f'Ended journey {self.id}')
            rows = db.execute(
                """
                INSERT INTO stays(id, journey_id, channel_url, channel_name, start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING *
                """,
                (randstr(), self.id, stream.channel.url, stream.channel.name, now, None))
            stay = Stay.model_validate(nested(dict(next(rows)), 'channel'))
        bot.dispatch_event(Event(type='journey-travel-on'))
        return stay

    def __str__(self) -> str:
        period = (
            f'from {format_datetime(self.start_time)} until {format_datetime(self.end_time)}'
            if self.end_time else f'since {format_datetime(self.start_time)}')
        return f'🧭 {self.title}, {self.id}, {period}'
