# pylint: disable=missing-docstring

from datetime import UTC, datetime, timedelta
from unittest import TestCase

from streamfarer.core import format_datetime, format_duration

class FormatDatetimeTest(TestCase):
    def test(self) -> None:
        time = format_datetime(datetime(2024, 9, 13, 15, 22, tzinfo=UTC))
        self.assertEqual(time, '13 Sep 2024 15:22')

class FormatDurationTest(TestCase):
    def test(self) -> None:
        duration = format_duration(timedelta(minutes=23, hours=1))
        self.assertEqual(duration, '1 h 23 min')

    def test_short_duration(self) -> None:
        duration = format_duration(timedelta(minutes=12))
        self.assertEqual(duration, '12 min')
