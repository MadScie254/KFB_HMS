"""Departmental stock custody, write-offs, and batch disposition."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import DepartmentIssue, DepartmentIssueLine, Role, StockBatch, StockMovement, StockWriteOff
from .permissions import user_role
from .service_common import audit, deterministic_key, raise_exception


@transaction.atomic
def issue_to_department(*, actor, department, received_by_name, lines, kind=None, patient=None, notes="", request=None):
    """Move stock from pharmacy into a named department's custody.

    This is a custody transfer, not a sale and not consumption. Stock leaves the
    pharmacy location and stays visible as outstanding departmental custody, so
    it can never be deducted a second time when it is administered.
    """
    if user_role(actor) != Role.PHARMACY:
        raise ValidationError("Only pharmacy staff may issue stock to a department.")
    if not lines:
        raise ValidationError("Record at least one item to issue.")
    department = department.strip()
    received_by_name = received_by_name.strip()
    if not department or not received_by_name:
        raise ValidationError("Name the department and the person receiving the stock.")

    kind = kind or DepartmentIssue.Kind.GENERAL
    if kind == DepartmentIssue.Kind.PATIENT and patient is None:
        raise ValidationError("A patient-specific issue must name the patient it is for.")

    prepared = []
    # Quantity already claimed by an earlier line of THIS issue, keyed by batch.
    # Without it two lines naming the same batch each read the untouched balance,
    # both pass, and the ledger goes negative — stock issued that never existed.
    reserved = {}
    for row in lines:
        batch = StockBatch.objects.select_for_update().get(pk=row["batch"].pk)
        quantity = Decimal(str(row["quantity"]))
        if quantity <= 0:
            raise ValidationError("Each issued line needs a positive quantity.")
        if not batch.can_dispense:
            raise ValidationError(
                f"{batch.item.name} batch {batch.batch_number} is {batch.get_status_display().lower()} and cannot be issued."
            )
        on_hand = batch.movements.aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")
        claimed = reserved.get(batch.pk, Decimal("0.000"))
        available = on_hand - claimed
        if quantity > available:
            already = f" ({claimed} already claimed by another line of this issue)" if claimed else ""
            raise ValidationError(
                f"Only {available} {batch.item.base_unit or 'units'} of {batch.item.name} "
                f"batch {batch.batch_number} are available{already}."
            )
        reserved[batch.pk] = claimed + quantity
        prepared.append((batch, quantity))

    issue = DepartmentIssue.objects.create(
        department=department,
        received_by_name=received_by_name,
        kind=kind,
        patient=patient,
        notes=notes.strip(),
        issued_by=actor,
    )
    for index, (batch, quantity) in enumerate(prepared):
        StockMovement.objects.create(
            batch=batch,
            movement_type=StockMovement.MovementType.TRANSFER,
            quantity_delta=-quantity,
            from_location="Pharmacy",
            to_location=department[:80],
            reference_type="DepartmentIssue",
            reference_id=str(issue.pk),
            reason=f"{issue.reference} to {received_by_name}"[:255],
            idempotency_key=deterministic_key("department_issue", issue.pk, batch.pk, index),
            entered_by=actor,
        )
        DepartmentIssueLine.objects.create(issue=issue, batch=batch, quantity_issued=quantity)

    audit(
        actor,
        "department_issue.created",
        issue,
        after={"department": department, "lines": len(prepared), "kind": kind},
        request=request,
    )
    return issue


@transaction.atomic
def account_for_issue(*, actor, issue_id, outcomes, request=None):
    """Close out departmental custody: administered, returned, or wasted.

    Consumption records that stock was used; it posts no further deduction
    because the units already left the pharmacy when custody moved. A return
    brings the units back into pharmacy stock, quarantined, because medicine
    that has been off the shelf needs an authorised disposition before reuse.
    """
    if user_role(actor) not in {Role.NURSE, Role.CLINICIAN, Role.PHARMACY}:
        raise ValidationError("Your role cannot account for departmental stock.")
    issue = DepartmentIssue.objects.select_for_update().get(pk=issue_id)
    if issue.status == DepartmentIssue.Status.SETTLED:
        raise ValidationError("This issue is already fully accounted for.")

    posted = 0
    for line in issue.lines.select_related("batch__item").select_for_update():
        outcome = outcomes.get(line.pk)
        if not outcome:
            continue
        consumed = Decimal(str(outcome.get("consumed", 0) or 0))
        returned = Decimal(str(outcome.get("returned", 0) or 0))
        wasted = Decimal(str(outcome.get("wasted", 0) or 0))
        if min(consumed, returned, wasted) < 0:
            raise ValidationError("Quantities cannot be negative.")
        total = consumed + returned + wasted
        if total <= 0:
            continue
        if total > line.outstanding:
            raise ValidationError(
                f"{line.batch.item.name}: only {line.outstanding} {line.batch.item.base_unit or 'units'} remain outstanding on this issue."
            )

        if returned > 0:
            # Returned stock re-enters the ledger in quarantine, never straight
            # back into sellable stock.
            StockMovement.objects.create(
                batch=line.batch,
                movement_type=StockMovement.MovementType.RETURN,
                quantity_delta=returned,
                from_location=issue.department[:80],
                to_location="Pharmacy quarantine",
                reference_type="DepartmentIssue",
                reference_id=str(issue.pk),
                reason=f"{issue.reference} returned unused"[:255],
                idempotency_key=deterministic_key("issue_return", issue.pk, line.pk, line.quantity_returned, returned),
                entered_by=actor,
            )
            if line.batch.status == StockBatch.Status.ACTIVE:
                line.batch.status = StockBatch.Status.QUARANTINE
                line.batch.save(update_fields=["status", "updated_at"])
            posted += 1

        if wasted > 0:
            # No ledger movement: these units left the pharmacy balance when
            # custody moved, and the specification is explicit that stock is
            # never deducted a second time at the point of use. The waste is
            # recorded against the custody line and raised for review.
            raise_exception(
                "departmental_waste",
                f"Waste recorded in {issue.department}: {line.batch.item.name}",
                f"{issue.reference}: {wasted} {line.batch.item.base_unit or 'units'} of batch "
                f"{line.batch.batch_number} recorded as wasted by {actor.username}.",
                dedupe_on=("departmental_waste", issue.pk, line.pk, line.quantity_wasted, wasted),
            )

        line.quantity_consumed += consumed
        line.quantity_returned += returned
        line.quantity_wasted += wasted
        line.save(update_fields=["quantity_consumed", "quantity_returned", "quantity_wasted"])

    issue.refresh_status()
    audit(
        actor,
        "department_issue.accounted",
        issue,
        after={"outstanding": str(issue.outstanding_quantity), "movements": posted},
        request=request,
    )
    return issue


@transaction.atomic
def request_write_off(*, actor, batch_id, quantity, reason, narrative, request=None):
    """Propose removing stock that can no longer be sold or used."""
    if user_role(actor) not in {Role.PHARMACY, Role.PROCUREMENT}:
        raise ValidationError("Only pharmacy or procurement staff may propose a write-off.")
    batch = StockBatch.objects.select_for_update().get(pk=batch_id)
    quantity = Decimal(str(quantity))
    if quantity <= 0:
        raise ValidationError("A write-off needs a positive quantity.")
    on_hand = batch.movements.aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")
    pending = StockWriteOff.objects.filter(
        batch=batch, status=StockWriteOff.Status.PENDING
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0.000")
    if quantity + pending > on_hand:
        raise ValidationError(
            f"Only {on_hand - pending} {batch.item.base_unit or 'units'} can still be written off from this batch."
        )
    if not narrative.strip():
        raise ValidationError("Describe what happened; a write-off without an explanation cannot be reviewed.")

    write_off = StockWriteOff.objects.create(
        batch=batch, quantity=quantity, reason=reason, narrative=narrative.strip(), requested_by=actor
    )
    audit(
        actor,
        "stock_write_off.requested",
        write_off,
        reason=write_off.narrative,
        after={"batch": batch.batch_number, "quantity": str(quantity), "value": str(write_off.value_at_cost)},
        request=request,
    )
    return write_off


@transaction.atomic
def review_write_off(*, actor, write_off_id, approve, review_notes="", request=None):
    """Approval is what removes the stock; rejection changes no balance."""
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only a delegated reviewer may approve a write-off.")
    write_off = StockWriteOff.objects.select_for_update().select_related("batch__item").get(pk=write_off_id)
    if write_off.requested_by_id == actor.id:
        raise ValidationError("You cannot approve your own write-off request.")
    if write_off.status != StockWriteOff.Status.PENDING:
        raise ValidationError("This write-off has already been reviewed.")

    if approve:
        # Time passes between proposal and approval, and stock keeps moving.
        # Posting an unchecked write-off drives the balance negative and
        # removes stock the hospital no longer has.
        on_hand = write_off.batch.movements.aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")
        if write_off.quantity > on_hand:
            raise ValidationError(
                f"Only {on_hand} {write_off.batch.item.base_unit or 'units'} of batch "
                f"{write_off.batch.batch_number} remain, but {write_off.quantity} were proposed for write-off. "
                "The stock has moved since this was raised; reject it and raise a new request for what is there."
            )
        StockMovement.objects.create(
            batch=write_off.batch,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_delta=-write_off.quantity,
            from_location="Pharmacy",
            to_location="Written off",
            reference_type="StockWriteOff",
            reference_id=str(write_off.pk),
            reason=f"{write_off.reference} {write_off.get_reason_display()} · {write_off.narrative}"[:255],
            idempotency_key=deterministic_key("write_off", write_off.pk),
            entered_by=actor,
        )
        raise_exception(
            "stock_write_off",
            f"Stock written off: {write_off.batch.item.name} batch {write_off.batch.batch_number}",
            f"{write_off.reference}: {write_off.quantity} {write_off.batch.item.base_unit or 'units'} "
            f"worth KES {write_off.value_at_cost:,.2f}, reason {write_off.get_reason_display().lower()}, "
            f"approved by {actor.username}.",
            dedupe_on=("stock_write_off", write_off.pk),
        )

    write_off.status = StockWriteOff.Status.APPROVED if approve else StockWriteOff.Status.REJECTED
    write_off.reviewed_by = actor
    write_off.reviewed_at = timezone.now()
    write_off.review_notes = review_notes.strip()
    write_off.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes", "updated_at"])
    audit(actor, f"stock_write_off.{write_off.status}", write_off, reason=write_off.review_notes, request=request)
    return write_off


@transaction.atomic
def set_batch_disposition(*, actor, batch_id, status, reason, request=None):
    """Release a quarantined batch back to active, or quarantine an active one."""
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only a delegated reviewer may change a batch disposition.")
    if status not in {StockBatch.Status.ACTIVE, StockBatch.Status.QUARANTINE}:
        raise ValidationError("A batch can only be released to active or held in quarantine here.")
    batch = StockBatch.objects.select_for_update().get(pk=batch_id)
    if batch.is_expired and status == StockBatch.Status.ACTIVE:
        raise ValidationError("Expired stock cannot be released back into sellable inventory.")
    if not reason.strip():
        raise ValidationError("Record why this disposition was authorised.")
    before = batch.status
    batch.status = status
    batch.save(update_fields=["status", "updated_at"])
    audit(actor, "stock_batch.disposition", batch, reason=reason.strip(), before={"status": before}, after={"status": status}, request=request)
    return batch
