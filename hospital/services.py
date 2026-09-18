import hashlib
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    AuditEvent,
    CatalogueItem,
    CashShift,
    ClinicianPayable,
    CreditNote,
    EyeCase,
    ExceptionRecord,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    PharmacyOrderItem,
    PriceVersion,
    PurchaseOrder,
    Role,
    StockBatch,
    StockMovement,
)
from .permissions import user_role


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


def active_price(item):
    now = timezone.now()
    return item.prices.filter(effective_from__lte=now).filter(
        models_q_effective(now)
    ).order_by("-effective_from").first()


def models_q_effective(now):
    from django.db.models import Q
    return Q(effective_to__isnull=True) | Q(effective_to__gt=now)


@transaction.atomic
def prepare_pharmacy_order(*, actor, customer_name, patient, items, encounter=None, prescription=None, request=None):
    if user_role(actor) != Role.PHARMACY:
        raise ValidationError("Only pharmacy staff may prepare a pharmacy basket.")
    if not items:
        raise ValidationError("Add at least one item.")

    invoice = Invoice.objects.create(patient=patient, encounter=encounter, customer_name=customer_name, created_by=actor)
    order = PharmacyOrder.objects.create(
        patient=patient,
        customer_name=customer_name,
        encounter=encounter,
        prescription=prescription,
        invoice=invoice,
        prepared_by=actor,
    )
    for item, quantity in items:
        if item.kind != CatalogueItem.Kind.PRODUCT or quantity <= 0:
            raise ValidationError("Every basket line must be an active product with a positive quantity.")
        price = active_price(item)
        if not price:
            raise ValidationError(f"{item.name} has no active price. It cannot silently be priced at zero.")
        PharmacyOrderItem.objects.create(order=order, product=item, quantity_base_units=quantity, unit_price=price.amount)
        InvoiceLine.objects.create(
            invoice=invoice,
            item=item,
            description=item.name,
            department=item.department,
            quantity=quantity,
            unit_price=price.amount,
            price_version=price,
        )
    invoice.status = Invoice.Status.POSTED
    invoice.posted_at = timezone.now()
    invoice.save(update_fields=["status", "posted_at", "updated_at"])
    audit(actor, "pharmacy_order.prepared", order, after={"invoice": invoice.invoice_number, "total": str(invoice.total)}, request=request)
    return order


@transaction.atomic
def record_payment(*, actor, invoice_id, amount, method, reference, idempotency_key, request=None):
    if user_role(actor) != Role.RECEPTION:
        raise ValidationError("Only reception/cashier staff may collect payments.")
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("Payment must be greater than zero.")
    if amount > invoice.balance:
        raise ValidationError(f"Payment exceeds the outstanding balance of KES {invoice.balance:,.2f}.")
    shift = CashShift.objects.select_for_update().filter(cashier=actor, status=CashShift.Status.OPEN).first()
    if method == Payment.Method.CASH and not shift:
        raise ValidationError("Open a cashier shift before recording cash.")
    verification = Payment.Verification.NOT_APPLICABLE if method == Payment.Method.CASH else Payment.Verification.UNVERIFIED
    try:
        payment = Payment.objects.create(
            amount=amount,
            method=method,
            reference=reference.strip().upper(),
            verification_status=verification,
            shift=shift,
            received_by=actor,
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing
        raise ValidationError("That payment reference has already been recorded.") from exc
    PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=amount, allocated_by=actor)
    invoice.refresh_status()
    order = getattr(invoice, "pharmacy_order", None)
    if order and invoice.balance <= 0 and (method == Payment.Method.CASH or verification in {Payment.Verification.MANUAL, Payment.Verification.PROVIDER}):
        order.status = PharmacyOrder.Status.CLEARED
        order.save(update_fields=["status", "updated_at"])
    audit(actor, "payment.recorded", payment, after={"amount": str(amount), "method": method, "invoice": invoice.invoice_number}, request=request)
    if method == Payment.Method.MPESA:
        ExceptionRecord.objects.get_or_create(
            category="unverified_mpesa",
            summary=f"Verify M-PESA {payment.reference} for {payment.receipt_number}",
            defaults={"evidence": f"Recorded amount KES {amount:,.2f}; not yet verified against hospital-controlled records."},
        )
    return payment


@transaction.atomic
def verify_mpesa(*, actor, payment_id, provider_confirmed=False, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("An authorised independent reviewer must verify M-PESA.")
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.method != Payment.Method.MPESA:
        raise ValidationError("This payment is not M-PESA.")
    if payment.received_by_id == actor.id:
        raise ValidationError("The person who recorded a payment cannot verify it.")
    payment.verification_status = Payment.Verification.PROVIDER if provider_confirmed else Payment.Verification.MANUAL
    payment.save(update_fields=["verification_status", "updated_at"])
    for allocation in payment.allocations.select_related("invoice"):
        invoice = allocation.invoice
        invoice.refresh_status()
        if hasattr(invoice, "pharmacy_order") and invoice.balance <= 0:
            invoice.pharmacy_order.status = PharmacyOrder.Status.CLEARED
            invoice.pharmacy_order.save(update_fields=["status", "updated_at"])
    audit(actor, "payment.verified", payment, after={"verification": payment.verification_status}, request=request)
    return payment


@transaction.atomic
def dispense_order(*, actor, order_id, idempotency_key, request=None):
    if user_role(actor) != Role.PHARMACY:
        raise ValidationError("Only pharmacy staff may dispense stock.")
    order = PharmacyOrder.objects.select_for_update().select_related("invoice").get(pk=order_id)
    existing = StockMovement.objects.filter(idempotency_key__startswith=f"{idempotency_key}:").exists()
    if order.status == PharmacyOrder.Status.DISPENSED and existing:
        return order
    if order.status != PharmacyOrder.Status.CLEARED:
        raise ValidationError("Payment clearance is required before dispensing.")

    allocations = []
    for line in order.items.select_related("product"):
        remaining = line.quantity_base_units
        batches = list(
            StockBatch.objects.select_for_update()
            .filter(item=line.product, status=StockBatch.Status.ACTIVE)
            .order_by("expiry_date", "created_at")
        )
        for batch in batches:
            if not batch.can_dispense:
                continue
            available = batch.movements.aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")
            if available <= 0:
                continue
            take = min(available, remaining)
            allocations.append((batch, take, line))
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValidationError(f"Insufficient valid stock for {line.product.name}; short by {remaining} {line.product.base_unit}.")

    for index, (batch, quantity, line) in enumerate(allocations):
        StockMovement.objects.create(
            batch=batch,
            movement_type=StockMovement.MovementType.DISPENSE,
            quantity_delta=-quantity,
            from_location="Pharmacy",
            to_location=f"Patient:{order.patient_id or order.customer_name}",
            reference_type="PharmacyOrder",
            reference_id=str(order.pk),
            idempotency_key=f"{idempotency_key}:{index}",
            entered_by=actor,
        )
        if line.prescription_item_id:
            pi = line.prescription_item
            pi.dispensed_quantity += quantity
            pi.save(update_fields=["dispensed_quantity"])
    order.status = PharmacyOrder.Status.DISPENSED
    order.dispensed_by = actor
    order.dispensed_at = timezone.now()
    order.save(update_fields=["status", "dispensed_by", "dispensed_at", "updated_at"])
    audit(actor, "pharmacy_order.dispensed", order, after={"movements": len(allocations)}, request=request)
    return order


@transaction.atomic
def approve_credit_note(*, actor, credit_note_id, approve, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only a delegated reviewer may review a credit note.")
    note = CreditNote.objects.select_for_update().select_related("invoice").get(pk=credit_note_id)
    if note.requested_by_id == actor.id:
        raise ValidationError("You cannot approve your own request.")
    if note.status != CreditNote.Status.PENDING:
        raise ValidationError("This request has already been reviewed.")
    if note.amount > note.invoice.balance:
        raise ValidationError("Credit exceeds the current invoice balance.")
    note.status = CreditNote.Status.APPROVED if approve else CreditNote.Status.REJECTED
    note.reviewed_by = actor
    note.reviewed_at = timezone.now()
    note.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    note.invoice.refresh_status()
    audit(actor, f"credit_note.{note.status}", note, reason=note.reason, request=request)
    return note


@transaction.atomic
def approve_purchase_order(*, actor, order_id, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only an independent reviewer may approve a purchase order.")
    order = PurchaseOrder.objects.select_for_update().get(pk=order_id)
    if order.requested_by_id == actor.id:
        raise ValidationError("The requester cannot approve their own purchase order.")
    order.approved_by = actor
    order.status = "approved"
    order.save(update_fields=["approved_by", "status", "updated_at"])
    audit(actor, "purchase_order.approved", order, request=request)
    return order


@transaction.atomic
def complete_eye_case(*, actor, case_id, request=None):
    if user_role(actor) not in {Role.EYE, Role.CLINICIAN}:
        raise ValidationError("Only authorised clinical eye staff may complete a case.")
    case = EyeCase.objects.select_for_update().get(pk=case_id)
    if case.readiness != "ready":
        raise ValidationError("Clinical readiness must be confirmed before completion.")
    case.status = "completed"
    case.completed_at = timezone.now()
    case.save(update_fields=["status", "completed_at", "updated_at"])
    payable, _ = ClinicianPayable.objects.get_or_create(eye_case=case, defaults={"amount": Decimal("2000.00")})
    audit(actor, "eye_case.completed", case, after={"payable": str(payable.amount)}, request=request)
    return case


def deterministic_key(*parts):
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()

