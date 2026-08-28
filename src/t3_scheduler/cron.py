from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class CronError(ValueError):
    pass


def _parse_field(text: str, minimum: int, maximum: int, *, sunday: bool = False) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            raise CronError(f"empty cron item in {text!r}")
        base, slash, step_text = item.partition("/")
        if slash:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"invalid step {step_text!r}") from exc
            if step <= 0:
                raise CronError("cron step must be positive")
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise CronError(f"invalid range {base!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise CronError(f"invalid value {base!r}") from exc

        allowed_maximum = 7 if sunday else maximum
        if start < minimum or end > allowed_maximum or start > end:
            raise CronError(f"value {base!r} outside {minimum}-{allowed_maximum}")
        for value in range(start, end + 1, step):
            values.add(0 if sunday and value == 7 else value)
    return frozenset(values)


@dataclass(frozen=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        fields = expression.split()
        if len(fields) != 5:
            raise CronError("cron expression must contain exactly five fields")
        minute, hour, day, month, weekday = fields
        return cls(
            expression=expression,
            minutes=_parse_field(minute, 0, 59),
            hours=_parse_field(hour, 0, 23),
            days=_parse_field(day, 1, 31),
            months=_parse_field(month, 1, 12),
            weekdays=_parse_field(weekday, 0, 6, sunday=True),
            day_wildcard=day == "*",
            weekday_wildcard=weekday == "*",
        )

    def matches(self, value: datetime) -> bool:
        if value.minute not in self.minutes or value.hour not in self.hours or value.month not in self.months:
            return False
        day_matches = value.day in self.days
        cron_weekday = (value.weekday() + 1) % 7
        weekday_matches = cron_weekday in self.weekdays
        if self.day_wildcard and self.weekday_wildcard:
            calendar_matches = True
        elif self.day_wildcard:
            calendar_matches = weekday_matches
        elif self.weekday_wildcard:
            calendar_matches = day_matches
        else:
            calendar_matches = day_matches or weekday_matches
        return calendar_matches
