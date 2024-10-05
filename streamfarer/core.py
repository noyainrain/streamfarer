"""Core concepts.

.. data:: Text

   String with visible characters.
"""

from datetime import datetime, timedelta
from typing import Annotated

from pydantic import StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

def format_datetime(time: datetime) -> str:
    """Format a *time*."""
    return time.strftime('%d %b %Y %H:%M')

def format_duration(duration: timedelta) -> str:
    """Format a *duration*."""
    hours, duration = divmod(duration, timedelta(hours=1))
    minutes = duration // timedelta(minutes=1)
    return ' '.join(filter(None, (f'{hours} h' if hours else None, f'{minutes} min')))
