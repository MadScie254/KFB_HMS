"""Shared audit, setting, and idempotency helpers for transactional workflows."""

import hashlib

from django.db import IntegrityError, transaction
from django.db.models import Case, F, Value, When
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import AuditEvent, ExceptionRecord, Setting
from .permissions import user_role
from .setting_validation import validated_setting_decimal


def raise_exception(category, summary, evidence, severity="warning", dedupe_on=None):
    """Raise an operational exception once, no matter how often it recurs.

    Matching on summary text is not safe: nothing stops two rows sharing a
    summary, and once two exist every later attempt to raise the same exception
    dies with MultipleObjectsReturned. A hashed dedupe key with a unique
    constraint makes the raise idempotent under concurrency, and a recurrence
    of something already marked resolved reopens it rather than vanishing.
    """
    summary = str(summary)[:255]
    key = _exception_key(dedupe_on or (category, summary))
    now = timezone.now()
    try:
        with transaction.atomic():
            record, created = ExceptionRecord.objects.get_or_create(
                dedupe_key=key,
                defaults={
                    "category": category, "summary": summary,
                    "evidence": evidence, "severity": severity, "last_seen_at": now,
                },
            )
    except IntegrityError:
        created = False
        record = ExceptionRecord.objects.get(dedupe_key=key)
    if created:
        return record
    ExceptionRecord.objects.filter(pk=record.pk).update(
        occurrence_count=F("occurrence_count") + 1,
        last_seen_at=Greatest(F("last_seen_at"), Value(now)),
        evidence=evidence,
        updated_at=now,
        status=Case(
            When(status=ExceptionRecord.Status.RESOLVED, then=Value(ExceptionRecord.Status.OPEN)),
            default=F("status"),
        ),
        resolved_at=Case(
            When(status=ExceptionRecord.Status.RESOLVED, then=Value(None)),
            default=F("resolved_at"),
        ),
    )
    record.refresh_from_db()
    return record


def _exception_key(parts):
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()[:64]


def setting_decimal(key, default):
    """Use a default only when the key is absent; reject invalid saved values."""
    row = Setting.objects.filter(key=key).first()
    if not row:
        return default
    return validated_setting_decimal(key, row.value)


def audit(actor, action, entity, *, reason="", before=None, after=None, request=None):
    return AuditEvent.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        effective_role=user_role(actor) if actor else "",
        action=action,
        entity_type=entity.__class__.__name__,
        entity_id=str(getattr(entity, "pk", "")),
        reason=reason,
        before=before,
        after=after,
        session_key=(request.session.session_key or "") if request else "",
        device_hint=(request.headers.get("User-Agent", "")[:255]) if request else "",
        ip_address=(request.META.get("REMOTE_ADDR") or None) if request else None,
    )


def deterministic_key(*parts):
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
