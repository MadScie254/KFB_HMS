"""Invoice payments, cashier shifts, M-PESA review, refunds, and credits."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    CashShift,
    CreditNote,
    ExceptionRecord,
    Invoice,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    Refund,
    Role,
)
from .permissions import user_role
from .service_common import _exception_key, audit, raise_exception


@transaction.atomic
def record_payment(*, actor, invoice_id, amount, method, reference, idempotency_key, request=None):
    if user_role(actor) != Role.RECEPTION:
        raise ValidationError("Only reception/cashier staff may collect payments.")
    amount = Decimal(str(amount))
    reference = (reference or "").strip().upper()
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key:
        raise ValidationError("A payment request key is required.")
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return _matching_payment_retry(
            existing, actor=actor, invoice_id=invoice_id, amount=amount,
            method=method, reference=reference,
        )
    if amount <= 0:
        raise ValidationError("Payment must be greater than zero.")
    if amount > invoice.balance:
        raise ValidationError(f"Payment exceeds the outstanding balance of KES {invoice.balance:,.2f}.")
    shift = CashShift.objects.select_for_update().filter(cashier=actor, status=CashShift.Status.OPEN).first()
    if method == Payment.Method.CASH and not shift:
        raise ValidationError("Open a cashier shift before recording cash.")
    verification = Payment.Verification.NOT_APPLICABLE if method == Payment.Method.CASH else Payment.Verification.UNVERIFIED
    try:
        # A nested savepoint keeps the workflow queryable after a uniqueness
        # constraint rejects a repeated button click or provider reference.
        with transaction.atomic():
            payment = Payment.objects.create(
                amount=amount,
                method=method,
                reference=reference,
                verification_status=verification,
                shift=shift,
                received_by=actor,
                idempotency_key=idempotency_key,
            )
    except IntegrityError as exc:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return _matching_payment_retry(
                existing, actor=actor, invoice_id=invoice_id, amount=amount,
                method=method, reference=reference,
            )
        raise ValidationError("That payment reference has already been recorded.") from exc
    # An unverified M-PESA allocation reserves its invoice association for
    # review, but Invoice.paid_amount does not treat it as settled money.
    PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=amount, allocated_by=actor)
    invoice.refresh_status()
    order = getattr(invoice, "pharmacy_order", None)
    if order and invoice.balance <= 0 and (method == Payment.Method.CASH or verification in {Payment.Verification.MANUAL, Payment.Verification.PROVIDER}):
        order.status = PharmacyOrder.Status.CLEARED
        order.save(update_fields=["status", "updated_at"])
    audit(actor, "payment.recorded", payment, after={"amount": str(amount), "method": method, "invoice": invoice.invoice_number}, request=request)
    if method == Payment.Method.MPESA:
        raise_exception(
            "unverified_mpesa",
            f"Verify M-PESA {payment.reference} for {payment.receipt_number}",
            f"Recorded amount KES {amount:,.2f}; not yet verified against hospital-controlled records.",
            dedupe_on=("unverified_mpesa", payment.pk),
        )
    return payment


def _matching_payment_retry(existing, *, actor, invoice_id, amount, method, reference):
    allocations = list(existing.allocations.values_list("invoice_id", "amount"))
    if (
        existing.received_by_id != actor.pk
        or existing.amount != amount
        or existing.method != method
        or existing.reference != reference
        or allocations != [(invoice_id, amount)]
    ):
        raise ValidationError("This payment request key was already used for a different payment.")
    return existing


@transaction.atomic
def open_cash_shift(*, actor, label, opening_float, request=None):
    if user_role(actor) not in {Role.RECEPTION, Role.OWNER}:
        raise ValidationError("Only a cashier may open a shift.")
    if not label.strip() or opening_float < 0:
        raise ValidationError("Enter a shift label and a nonnegative opening float.")
    try:
        with transaction.atomic():
            shift = CashShift.objects.create(cashier=actor, label=label.strip(), opening_float=opening_float)
    except IntegrityError as exc:
        raise ValidationError("You already have an open shift.") from exc
    audit(actor, "shift.opened", shift, after={"float": str(shift.opening_float)}, request=request)
    return shift


@transaction.atomic
def close_cash_shift(*, actor, actual_cash, transfers_in, transfers_out, variance_reason, request=None):
    shift = CashShift.objects.select_for_update().filter(cashier=actor, status=CashShift.Status.OPEN).first()
    if not shift:
        raise ValidationError("There is no open shift to close.")
    if min(actual_cash, transfers_in, transfers_out) < 0:
        raise ValidationError("Cash counts and transfers cannot be negative.")
    shift.actual_cash = actual_cash
    shift.transfers_in = transfers_in
    shift.transfers_out = transfers_out
    shift.variance_reason = variance_reason.strip()
    shift.closed_at = timezone.now()
    shift.status = CashShift.Status.CLOSED
    if shift.variance and not shift.variance_reason:
        raise ValidationError("Explain the cash variance before submitting the shift.")
    shift.save(update_fields=[
        "actual_cash", "transfers_in", "transfers_out", "variance_reason", "closed_at", "status", "updated_at",
    ])
    if shift.variance:
        raise_exception("cash_variance", f"{shift.label}: KES {shift.variance:,.2f} variance", shift.variance_reason)
    audit(actor, "shift.closed", shift, after={
        "expected": str(shift.expected_cash), "actual": str(shift.actual_cash), "variance": str(shift.variance),
    }, request=request)
    return shift


@transaction.atomic
def review_cash_shift(*, actor, shift_id, request=None):
    if user_role(actor) not in {Role.OWNER, Role.REVIEWER}:
        raise ValidationError("Only an authorised reviewer may review a shift.")
    shift = CashShift.objects.select_for_update().get(pk=shift_id)
    if shift.cashier_id == actor.pk:
        raise ValidationError("You cannot review your own shift.")
    if shift.status != CashShift.Status.CLOSED:
        raise ValidationError("Only a closed shift can be reviewed.")
    shift.status = CashShift.Status.REVIEWED
    shift.reviewer = actor
    shift.reviewed_at = timezone.now()
    shift.save(update_fields=["status", "reviewer", "reviewed_at", "updated_at"])
    audit(actor, "shift.reviewed", shift, after={
        "expected": str(shift.expected_cash), "actual": str(shift.actual_cash), "variance": str(shift.variance),
    }, request=request)
    return shift


@transaction.atomic
def request_refund(*, actor, payment_id, amount, reason, request=None):
    if user_role(actor) not in {Role.RECEPTION, Role.OWNER}:
        raise ValidationError("Only authorised cashier staff may request a refund.")
    allocation = PaymentAllocation.objects.filter(payment_id=payment_id).first()
    if not allocation or PaymentAllocation.objects.filter(payment_id=payment_id).count() != 1:
        raise ValidationError("This payment needs a single linked invoice before a refund can be requested.")
    invoice = Invoice.objects.select_for_update().get(pk=allocation.invoice_id)
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.method != Payment.Method.CASH or payment.status != Payment.Status.VALID:
        raise ValidationError("Only valid cash payments can be refunded through a cashier shift.")
    if getattr(invoice, "pharmacy_order", None) and invoice.pharmacy_order.status == PharmacyOrder.Status.DISPENSED:
        raise ValidationError("A dispensed order requires a documented return before refunding payment.")
    if amount <= 0 or not reason.strip():
        raise ValidationError("Enter a positive refund amount and a reason.")
    reserved = payment.refunds.exclude(status=Refund.Status.REJECTED).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if amount > allocation.amount - reserved:
        raise ValidationError("Refund exceeds the unrefunded amount of this payment.")
    refund = Refund.objects.create(
        payment=payment, invoice=invoice, amount=amount, reason=reason.strip(), requested_by=actor,
    )
    audit(actor, "refund.requested", refund, reason=refund.reason, request=request)
    return refund


@transaction.atomic
def review_refund(*, actor, refund_id, approve, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only an independent reviewer may review a refund.")
    refund = Refund.objects.select_related("payment").get(pk=refund_id)
    if refund.invoice_id is None:
        raise ValidationError("This legacy refund has no linked invoice and needs manual reconciliation.")
    invoice = Invoice.objects.select_for_update().get(pk=refund.invoice_id)
    payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
    refund = Refund.objects.select_for_update().get(pk=refund_id)
    if actor.pk in {refund.requested_by_id, payment.received_by_id}:
        raise ValidationError("The requester or original cashier cannot review this refund.")
    if refund.status != Refund.Status.PENDING:
        raise ValidationError("Only a pending refund can be reviewed.")
    if approve and (payment.status != Payment.Status.VALID or invoice.pk != refund.invoice_id):
        raise ValidationError("The linked payment or invoice changed before review.")
    refund.status = Refund.Status.APPROVED if approve else Refund.Status.REJECTED
    refund.reviewed_by = actor
    refund.reviewed_at = timezone.now()
    refund.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    audit(actor, f"refund.{refund.status}", refund, reason=refund.reason, request=request)
    return refund


@transaction.atomic
def pay_refund(*, actor, refund_id, request=None):
    if user_role(actor) not in {Role.RECEPTION, Role.OWNER}:
        raise ValidationError("Only authorised cashier staff may pay a refund.")
    refund = Refund.objects.get(pk=refund_id)
    if refund.invoice_id is None:
        raise ValidationError("This legacy refund has no linked invoice and needs manual reconciliation.")
    invoice = Invoice.objects.select_for_update().get(pk=refund.invoice_id)
    payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
    refund = Refund.objects.select_for_update().get(pk=refund_id)
    if refund.status != Refund.Status.APPROVED:
        raise ValidationError("Only an approved refund can be paid.")
    if actor.pk == refund.reviewed_by_id:
        raise ValidationError("The reviewer cannot pay their own approved refund.")
    if payment.status != Payment.Status.VALID:
        raise ValidationError("The original payment is no longer valid.")
    shift = CashShift.objects.select_for_update().filter(cashier=actor, status=CashShift.Status.OPEN).first()
    if not shift:
        raise ValidationError("Open a cashier shift before paying a cash refund.")
    refund.status = Refund.Status.PAID
    refund.paid_by = actor
    refund.paid_at = timezone.now()
    refund.paid_shift = shift
    refund.save(update_fields=["status", "paid_by", "paid_at", "paid_shift", "updated_at"])
    invoice.refresh_status()
    order = getattr(invoice, "pharmacy_order", None)
    if order and order.status == PharmacyOrder.Status.CLEARED and invoice.balance > 0:
        order.status = PharmacyOrder.Status.PREPARED
        order.save(update_fields=["status", "updated_at"])
    audit(actor, "refund.paid", refund, after={"shift": shift.pk, "amount": str(refund.amount)}, request=request)
    return refund


@transaction.atomic
def review_mpesa(*, actor, payment_id, approve, review_notes="", request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("An authorised independent reviewer must verify M-PESA.")
    invoice_ids = list(
        PaymentAllocation.objects.filter(payment_id=payment_id)
        .order_by("invoice_id").values_list("invoice_id", flat=True)
    )
    invoices = {
        invoice.pk: invoice for invoice in
        Invoice.objects.select_for_update().filter(pk__in=invoice_ids).order_by("pk")
    }
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.method != Payment.Method.MPESA:
        raise ValidationError("This payment is not M-PESA.")
    if payment.received_by_id == actor.id:
        raise ValidationError("The person who recorded a payment cannot verify it.")
    if payment.status != Payment.Status.VALID or payment.verification_status != Payment.Verification.UNVERIFIED:
        raise ValidationError("This M-PESA claim has already been reviewed.")
    allocations = list(payment.allocations.all())
    if not allocations or {row.invoice_id for row in allocations} != set(invoices):
        raise ValidationError("The payment's invoice allocation changed during review. Please retry.")
    review_notes = review_notes.strip()
    if not review_notes:
        raise ValidationError(
            "Record the evidence checked for this M-PESA claim." if approve
            else "Record why this M-PESA claim was rejected."
        )
    if approve:
        for allocation in allocations:
            if allocation.amount > invoices[allocation.invoice_id].balance:
                raise ValidationError("Verification would exceed the current invoice balance. Reject the claim or resolve the other settlement first.")
        payment.verification_status = Payment.Verification.MANUAL
    else:
        payment.status = Payment.Status.REJECTED
    payment.reviewed_by = actor
    payment.reviewed_at = timezone.now()
    payment.review_notes = review_notes
    payment.save(update_fields=["status", "verification_status", "reviewed_by", "reviewed_at", "review_notes", "updated_at"])
    for invoice in invoices.values():
        invoice.refresh_status()
        if approve and hasattr(invoice, "pharmacy_order") and invoice.balance <= 0:
            invoice.pharmacy_order.status = PharmacyOrder.Status.CLEARED
            invoice.pharmacy_order.save(update_fields=["status", "updated_at"])
    ExceptionRecord.objects.filter(
        dedupe_key=_exception_key(("unverified_mpesa", payment.pk)),
    ).update(
        status=ExceptionRecord.Status.RESOLVED,
        resolved_at=timezone.now(),
        resolution="M-PESA claim verified." if approve else f"M-PESA claim rejected: {review_notes}"[:255],
    )
    audit(
        actor, "payment.verified" if approve else "payment.rejected", payment,
        reason=review_notes, after={"verification": payment.verification_status, "status": payment.status}, request=request,
    )
    return payment


@transaction.atomic
def approve_credit_note(*, actor, credit_note_id, approve, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only a delegated reviewer may review a credit note.")
    # Payment takes the invoice lock first. Credit reviews must use the same
    # lock order so every balance check sees the preceding committed change.
    invoice_id = CreditNote.objects.values_list("invoice_id", flat=True).get(pk=credit_note_id)
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    note = CreditNote.objects.select_for_update().get(pk=credit_note_id)
    if note.invoice_id != invoice.pk:
        raise ValidationError("The credit note's invoice changed during review. Please retry.")
    if note.requested_by_id == actor.id:
        raise ValidationError("You cannot approve your own request.")
    if note.status != CreditNote.Status.PENDING:
        raise ValidationError("This request has already been reviewed.")
    if approve and note.amount > invoice.balance:
        raise ValidationError("Credit exceeds the current invoice balance.")
    note.status = CreditNote.Status.APPROVED if approve else CreditNote.Status.REJECTED
    note.reviewed_by = actor
    note.reviewed_at = timezone.now()
    note.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    invoice.refresh_status()
    audit(actor, f"credit_note.{note.status}", note, reason=note.reason, request=request)
    return note
