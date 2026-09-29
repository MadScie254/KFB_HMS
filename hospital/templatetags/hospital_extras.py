"""Presentation helpers. No business rule lives here — only how it is shown."""

from django import template
from django.utils import timezone

register = template.Library()

# How long someone may wait before the screen should say so without being read.
# A queue where a twelve-hour wait looks identical to a five-minute one is a
# queue nobody triages.
WAIT_WARNING_MINUTES = 60
WAIT_URGENT_MINUTES = 180


def _minutes_since(value):
    if not value:
        return 0
    return max(0, int((timezone.now() - value).total_seconds() // 60))


@register.filter
def wait_class(value):
    """CSS class for how long a wait has run."""
    minutes = _minutes_since(value)
    if minutes >= WAIT_URGENT_MINUTES:
        return "wait wait-urgent"
    if minutes >= WAIT_WARNING_MINUTES:
        return "wait wait-warn"
    return "wait"


@register.filter
def wait_row_class(value):
    """Row tint for a wait that has gone past a threshold."""
    minutes = _minutes_since(value)
    if minutes >= WAIT_URGENT_MINUTES:
        return "row-urgent"
    if minutes >= WAIT_WARNING_MINUTES:
        return "row-warn"
    return ""


@register.filter
def wait_note(value):
    """Words for the same fact, for anyone not reading the colour."""
    minutes = _minutes_since(value)
    if minutes >= WAIT_URGENT_MINUTES:
        return "Waiting a long time"
    if minutes >= WAIT_WARNING_MINUTES:
        return "Waiting over an hour"
    return ""


@register.filter
def sort_value(value):
    """A stable value a table sort can compare, independent of formatting."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
