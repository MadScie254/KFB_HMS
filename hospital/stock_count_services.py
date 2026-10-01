"""Freeze, submit, and independently review pharmacy stock counts."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Role, StockBatch, StockCount, StockCountLine, StockMovement
from .permissions import user_role
from .service_common import audit, deterministic_key, raise_exception, setting_decimal


@transaction.atomic
def open_stock_count(*, actor, location="Pharmacy", blind_count=True, notes="", request=None):
    """Freeze a count sheet: every batch with its ledger balance at the cutoff.

    Expected quantities are captured once, at the cutoff. Submission rejects
    the sheet if stock moves while the shelf is being counted.
    """
    if user_role(actor) not in {Role.PHARMACY, Role.PROCUREMENT}:
        raise ValidationError("Only pharmacy or procurement staff may open a stock count.")
    if location != "Pharmacy":
        raise ValidationError("Only Pharmacy stock can be counted against this ledger.")
    cutoff = timezone.now()
    count = StockCount.objects.create(
        location=location,
        cutoff_at=cutoff,
        blind_count=blind_count,
        notes=notes.strip(),
        counted_by=actor,
    )

    # One aggregate for every balance, not one query per batch. A pharmacy that
    # has been trading for a few years holds thousands of batches; reading each
    # balance separately turns opening a count into thousands of round trips.
    balances = dict(
        StockMovement.objects.filter(event_at__lte=cutoff)
        .values_list("batch_id")
        .annotate(total=Sum("quantity_delta"))
        .values_list("batch_id", "total")
    )

    # A sheet nobody can finish is a control nobody uses. Count what is on the
    # shelf (any non-zero balance) plus the live products that should be there,
    # so "the ledger says zero but here are twenty" is still recordable. Batches
    # that are both empty and retired are left off.
    today = timezone.localdate()
    lines = []
    for batch in StockBatch.objects.select_related("item").order_by("item__name", "batch_number"):
        balance = balances.get(batch.pk) or Decimal("0.000")
        live = batch.status == StockBatch.Status.ACTIVE and (not batch.expiry_date or batch.expiry_date >= today)
        if balance == 0 and not live:
            continue
        lines.append(StockCountLine(
            count=count, batch=batch,
            expected_quantity=balance, counted_quantity=Decimal("0.000"),
        ))
    if not lines:
        raise ValidationError("There is no stock to count: no batch holds a balance and no product is active.")
    StockCountLine.objects.bulk_create(lines, batch_size=500)

    audit(actor, "stock_count.opened", count, after={"lines": len(lines), "blind": blind_count}, request=request)
    return count


def _stock_moved_during_count(count, submitted_at):
    """Catch ordinary and backdated ledger entries that invalidate the snapshot."""
    return StockMovement.objects.filter(
        entered_at__gt=count.cutoff_at,
        event_at__lte=submitted_at,
    ).exists()


@transaction.atomic
def submit_stock_count(*, actor, count_id, counted, reasons=None, request=None):
    """Record the counted quantities and send the sheet for independent review."""
    reasons = reasons or {}
    count = StockCount.objects.select_for_update().get(pk=count_id)
    if count.counted_by_id != actor.id:
        raise ValidationError("Only the person who opened this count may submit it.")
    if count.status != StockCount.Status.FROZEN:
        raise ValidationError("This count has already been submitted.")
    if count.location != "Pharmacy":
        raise ValidationError("Only Pharmacy stock can be counted against this ledger.")
    submitted_at = timezone.now()
    if _stock_moved_during_count(count, submitted_at):
        raise ValidationError("Stock moved after this sheet was frozen. Start a new count against a fresh ledger snapshot.")
    for line in count.lines.select_for_update():
        if line.pk not in counted:
            raise ValidationError("Enter a counted quantity for every line on the frozen sheet.")
        quantity = Decimal(str(counted[line.pk]))
        if quantity < 0:
            raise ValidationError("A counted quantity cannot be negative.")
        line.counted_quantity = quantity
        line.reason = str(reasons.get(line.pk, ""))[:255]
        line.save(update_fields=["counted_quantity", "reason"])
    count.status = StockCount.Status.SUBMITTED
    count.submitted_at = submitted_at
    count.save(update_fields=["status", "submitted_at", "updated_at"])
    audit(actor, "stock_count.submitted", count, after={"net_variance": str(count.net_variance)}, request=request)
    return count


@transaction.atomic
def review_stock_count(*, actor, count_id, approve, review_notes="", request=None):
    """Approve or reject a count; approval is what posts the correcting movements.

    Nothing in the application edits a quantity directly. A difference between
    the shelf and the ledger becomes an approved adjustment movement with a
    named reviewer, or it does not happen at all.
    """
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only a delegated reviewer may approve a stock count.")
    count = StockCount.objects.select_for_update().get(pk=count_id)
    if count.counted_by_id == actor.id or count.witnessed_by_id == actor.id:
        raise ValidationError("A stock count cannot be reviewed by the person who counted it.")
    if count.status != StockCount.Status.SUBMITTED:
        raise ValidationError("Only a submitted count can be reviewed.")
    if approve and count.location != "Pharmacy":
        raise ValidationError("This location has no separate ledger balance. Reject the sheet without posting adjustments.")
    if approve and _stock_moved_during_count(count, count.submitted_at or count.updated_at):
        raise ValidationError("Stock moved during this count. Reject the stale sheet and start a new count.")

    posted = 0
    variance_threshold = setting_decimal("stock_variance_review_value", Decimal("500.00")) if approve else None
    if approve:
        for line in count.lines.select_related("batch__item").select_for_update():
            variance = line.variance
            if not variance:
                continue
            StockMovement.objects.create(
                batch=line.batch,
                movement_type=StockMovement.MovementType.ADJUSTMENT,
                quantity_delta=variance,
                from_location=count.location if variance < 0 else "",
                to_location=count.location if variance > 0 else "",
                reference_type="StockCount",
                reference_id=str(count.pk),
                reason=f"{count.reference} approved variance · cutoff {timezone.localtime(count.cutoff_at):%d %b %Y %H:%M} · {line.reason}"[:255],
                idempotency_key=deterministic_key("stock_count", count.pk, line.pk),
                entered_by=actor,
            )
            posted += 1
            if abs(line.variance_value) > variance_threshold:
                raise_exception(
                    "stock_discrepancy",
                    f"Approved stock adjustment for {line.batch.item.name} batch {line.batch.batch_number}",
                    f"{count.reference}: counted {line.counted_quantity}, expected {line.expected_quantity}, "
                    f"value KES {line.variance_value:,.2f}. Reason recorded: {line.reason or 'none given'}.",
                    dedupe_on=("stock_discrepancy", count.pk, line.pk),
                )

    count.status = StockCount.Status.APPROVED if approve else StockCount.Status.REJECTED
    count.reviewed_by = actor
    count.reviewed_at = timezone.now()
    count.review_notes = review_notes.strip()
    count.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes", "updated_at"])
    audit(
        actor,
        f"stock_count.{count.status}",
        count,
        reason=count.review_notes,
        after={"adjustments_posted": posted, "net_variance": str(count.net_variance)},
        request=request,
    )
    return count
