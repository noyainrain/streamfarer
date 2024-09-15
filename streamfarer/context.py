"""Context-Local state.

.. data:: bot

   Active bot.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bot import Bot

bot: ContextVar[Bot] = ContextVar('bot')
