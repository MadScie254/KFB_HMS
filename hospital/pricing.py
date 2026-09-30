"""The shared definition of a currently effective approved price."""

from django.db.models import Q
from django.utils import timezone

from .models import PriceVersion


def active_price_versions():
    now = timezone.now()
    return PriceVersion.objects.filter(effective_from__lte=now).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=now)
    ).order_by("item_id", "-effective_from", "-pk")
