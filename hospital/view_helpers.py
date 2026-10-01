"""Small helpers shared by HTTP view modules."""

from django.core.paginator import Paginator
from django.utils.http import urlencode


def _validation_message(exc):
    if hasattr(exc, "messages"):
        return " ".join(exc.messages)
    return str(exc)


def _worklist_page(request, queryset, per_page, parameter, anchor):
    """Page a worklist while preserving the other lists' current pages."""
    page = Paginator(queryset, per_page).get_page(request.GET.get(parameter))

    def link(number):
        query = request.GET.copy()
        query[parameter] = number
        return f"?{query.urlencode()}#{anchor}"

    return page, {
        "previous": link(page.previous_page_number()) if page.has_previous() else None,
        "next": link(page.next_page_number()) if page.has_next() else None,
    }


def _period_days(value, default=7):
    """A safe reporting window from a query string, clamped to one year."""
    days = int(value) if str(value).isdigit() else default
    return max(1, min(days, 365))


def _filter_query(**params):
    """Query-string tail that keeps active filters on pagination links."""
    active = {key: value for key, value in params.items() if value not in (None, "")}
    return f"&{urlencode(active)}" if active else ""
