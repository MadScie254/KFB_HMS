import hashlib
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    AuditEvent,
    CashShift,
    CatalogueItem,
    ClinicianPayable,
    CreditNote,
    DepartmentIssue,
    DepartmentIssueLine,
    ExceptionRecord,
    EyeCase,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    PharmacyOrderItem,
    PurchaseOrder,
    Role,
    Setting,
    StockBatch,
    StockCount,
    StockCountLine,
    StockMovement,
    StockWriteOff,
)
from .permissions import user_role

# Defaults for thresholds an implementer is expected to review. They are read
# through Setting so a site can change them without a code change, and they are
# deliberately conservative rather than silent.
NEAR_EXPIRY_DAYS = 90
COST_VARIANCE_FRACTION = Decimal("0.10")
INVOICE_TOLERANCE = Decimal("1.00")


def setting_decimal(key, default):
    """Read a numeric Setting, falling back to the documented default.

    A site that has not configured a threshold gets the default rather than a
    crash or a silently disabled control.
    """
    row = Setting.objects.filter(key=key).first()
    if not row or not row.value.strip():
        return default
    try:
        return Decimal(row.value.strip())
    except (ArithmeticError, ValueError):
        return default


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
        # A nested savepoint keeps the workflow queryable after a uniqueness
        # constraint rejects a repeated button click or provider reference.
        with transaction.atomic():
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
    # Quantity already earmarked inside THIS call, keyed by batch id. Without it,
    # two lines for the same product each read the untouched batch balance and
    # the order dispenses more than the hospital physically holds.
    reserved = {}
    for line in order.items.select_related("product"):
        remaining = Decimal(str(line.quantity_base_units))
        batches = list(
            StockBatch.objects.select_for_update()
            .filter(item=line.product, status=StockBatch.Status.ACTIVE)
            .order_by("expiry_date", "created_at")
        )
        for batch in batches:
            if not batch.can_dispense:
                continue
            on_hand = batch.movements.aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")
            available = on_hand - reserved.get(batch.pk, Decimal("0.000"))
            if available <= 0:
                continue
            take = min(available, remaining)
            allocations.append((batch, take, line))
            reserved[batch.pk] = reserved.get(batch.pk, Decimal("0.000")) + take
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


@transaction.atomic
def receive_delivery(
    *,
    actor,
    purchase_order_id,
    supplier_invoice_reference,
    invoice_amount,
    invoice_date,
    invoice_photo,
    lines,
    delivered_at=None,
    request=None,
):
    """Post a physical delivery into the stock ledger against its invoice.

    This is the only way stock enters the hospital through the application.
    The supplier's invoice photograph is mandatory: a receipt keyed in without
    the document it claims to match is exactly the entry this control exists to
    prevent. Quantity and price differences are recorded and flagged rather
    than blocked, because the balance must follow what physically arrived.
    """
    if user_role(actor) not in {Role.PROCUREMENT, Role.PHARMACY}:
        raise ValidationError("Only procurement or pharmacy staff may receive a delivery.")
    if invoice_photo is None:
        raise ValidationError("Attach a photograph or scan of the supplier invoice before posting stock.")
    if not lines:
        raise ValidationError("Record at least one delivered line.")

    order = PurchaseOrder.objects.select_for_update().select_related("supplier").get(pk=purchase_order_id)
    if order.status not in {"approved", "part_received"}:
        raise ValidationError("Only an independently approved order can receive stock.")

    reference = supplier_invoice_reference.strip().upper()
    if not reference:
        raise ValidationError("Enter the supplier invoice or delivery note reference.")
    invoice_amount = Decimal(invoice_amount)
    if invoice_amount < 0:
        raise ValidationError("The supplier invoice amount cannot be negative.")

    today = timezone.localdate()
    near_expiry_days = int(setting_decimal("near_expiry_days", Decimal(NEAR_EXPIRY_DAYS)))
    prepared = []
    for row in lines:
        order_line = row["order_line"]
        if order_line.order_id != order.pk:
            raise ValidationError("A delivered line must belong to the order being received.")
        quantity = Decimal(str(row["quantity_received"]))
        if quantity <= 0:
            raise ValidationError("Each delivered line needs a positive quantity.")
        batch_number = str(row["batch_number"]).strip()
        if not batch_number:
            raise ValidationError(f"Record the batch number printed on {order_line.item.name}.")
        expiry = row.get("expiry_date")
        if expiry and expiry < today:
            raise ValidationError(
                f"{order_line.item.name} batch {batch_number} expired on {expiry:%d %b %Y}. Expired stock cannot be received into sellable inventory."
            )
        prepared.append((order_line, quantity, batch_number, expiry, Decimal(str(row["actual_unit_cost"]))))

    try:
        # A savepoint keeps the outer transaction usable when the per-order
        # invoice uniqueness constraint rejects a repeated submission.
        with transaction.atomic():
            receipt = GoodsReceipt.objects.create(
                purchase_order=order,
                supplier_invoice_reference=reference,
                invoice_photo=invoice_photo,
                invoice_photo_name=getattr(invoice_photo, "name", "")[:255],
                invoice_amount=invoice_amount,
                invoice_date=invoice_date,
                delivered_at=delivered_at or timezone.now(),
                received_by=actor,
            )
    except IntegrityError as exc:
        raise ValidationError(
            f"Supplier invoice {reference} has already been received against {order.order_number}."
        ) from exc

    flags = []
    duplicate = GoodsReceipt.objects.filter(
        purchase_order__supplier=order.supplier, supplier_invoice_reference=reference
    ).exclude(pk=receipt.pk).first()
    if duplicate:
        flags.append((
            "urgent",
            f"Supplier invoice {reference} recorded twice for {order.supplier.name}",
            f"Also received on {duplicate.receipt_number} against {duplicate.purchase_order.order_number}. Confirm this is a genuinely separate delivery before paying.",
        ))

    cost_threshold = setting_decimal("purchase_cost_variance_fraction", COST_VARIANCE_FRACTION)
    for index, (order_line, quantity, batch_number, expiry, unit_cost) in enumerate(prepared):
        batch, created = StockBatch.objects.get_or_create(
            item=order_line.item,
            batch_number=batch_number,
            defaults={"expiry_date": expiry, "purchase_cost_per_base_unit": unit_cost},
        )
        if not created and batch.expiry_date != expiry:
            raise ValidationError(
                f"Batch {batch_number} of {order_line.item.name} is already recorded with expiry "
                f"{batch.expiry_date or 'not recorded'}. Two different expiry dates cannot share one batch number."
            )
        StockMovement.objects.create(
            batch=batch,
            movement_type=StockMovement.MovementType.RECEIPT,
            quantity_delta=quantity,
            from_location=order.supplier.name[:80],
            to_location="Pharmacy",
            reference_type="GoodsReceipt",
            reference_id=str(receipt.pk),
            reason=f"{receipt.receipt_number} · invoice {reference}",
            idempotency_key=deterministic_key("goods_receipt", receipt.pk, order_line.pk, index),
            entered_by=actor,
        )
        GoodsReceiptLine.objects.create(
            receipt=receipt,
            order_line=order_line,
            batch=batch,
            quantity_received=quantity,
            batch_number=batch_number,
            expiry_date=expiry,
            actual_unit_cost=unit_cost,
        )

        quoted = Decimal(str(order_line.quoted_unit_cost))
        if quoted > 0 and abs(unit_cost - quoted) > (quoted * cost_threshold):
            flags.append((
                "warning",
                f"Delivered cost differs from the approved order for {order_line.item.name}",
                f"{receipt.receipt_number}: quoted KES {quoted:,.2f}, invoiced KES {unit_cost:,.2f} per {order_line.item.base_unit or 'unit'}.",
            ))
        if expiry and (expiry - today).days <= near_expiry_days:
            flags.append((
                "warning",
                f"Short-dated stock received: {order_line.item.name} batch {batch_number}",
                f"{receipt.receipt_number}: expires {expiry:%d %b %Y}, within the {near_expiry_days}-day review window.",
            ))

        received_total = GoodsReceiptLine.objects.filter(order_line=order_line).aggregate(
            total=Sum("quantity_received")
        )["total"] or Decimal("0.000")
        if received_total > Decimal(str(order_line.quantity_base_units)):
            flags.append((
                "warning",
                f"Delivered quantity exceeds the approved order for {order_line.item.name}",
                f"{receipt.receipt_number}: ordered {order_line.quantity_base_units}, received {received_total} {order_line.item.base_unit or 'units'} in total.",
            ))

    receipt.posted_at = timezone.now()
    receipt.save(update_fields=["posted_at", "updated_at"])

    order_lines = list(order.lines.select_related("item"))
    fully_received = all(
        (GoodsReceiptLine.objects.filter(order_line=line).aggregate(total=Sum("quantity_received"))["total"] or Decimal("0.000"))
        >= Decimal(str(line.quantity_base_units))
        for line in order_lines
    )
    order.status = "received" if fully_received else "part_received"
    order.save(update_fields=["status", "updated_at"])

    tolerance = setting_decimal("supplier_invoice_tolerance", INVOICE_TOLERANCE)
    variance = receipt.invoice_variance
    if abs(variance) > tolerance:
        flags.append((
            "warning",
            f"Supplier invoice {reference} does not match the goods counted in",
            f"{receipt.receipt_number}: invoice KES {invoice_amount:,.2f}, delivered value KES {receipt.received_value:,.2f}, difference KES {variance:,.2f}.",
        ))

    for severity, summary, evidence in flags:
        ExceptionRecord.objects.get_or_create(
            category="purchase_discrepancy",
            summary=summary[:255],
            defaults={"severity": severity, "evidence": evidence},
        )

    audit(
        actor,
        "goods_receipt.posted",
        receipt,
        after={
            "order": order.order_number,
            "invoice": reference,
            "lines": len(prepared),
            "delivered_value": str(receipt.received_value),
            "invoice_amount": str(invoice_amount),
            "order_status": order.status,
            "flags": len(flags),
        },
        request=request,
    )
    return receipt


@transaction.atomic
def check_delivery(*, actor, receipt_id, discrepancy_notes="", request=None):
    """Second-person confirmation that the delivery matches its invoice."""
    if user_role(actor) not in {Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Your role cannot check a delivery.")
    receipt = GoodsReceipt.objects.select_for_update().select_related("purchase_order").get(pk=receipt_id)
    if receipt.received_by_id == actor.id:
        raise ValidationError("The person who received a delivery cannot also check it.")
    if receipt.checked_by_id:
        raise ValidationError("This delivery has already been checked.")
    receipt.checked_by = actor
    receipt.checked_at = timezone.now()
    receipt.discrepancy_notes = discrepancy_notes.strip()
    receipt.save(update_fields=["checked_by", "checked_at", "discrepancy_notes", "updated_at"])
    audit(actor, "goods_receipt.checked", receipt, reason=receipt.discrepancy_notes, request=request)
    return receipt


@transaction.atomic
def open_stock_count(*, actor, location="Pharmacy", blind_count=True, notes="", request=None):
    """Freeze a count sheet: every batch with its ledger balance at the cutoff.

    Expected quantities are captured once, at the cutoff, so a movement posted
    while the shelf is being counted cannot quietly change what the count is
    later judged against.
    """
    if user_role(actor) not in {Role.PHARMACY, Role.PROCUREMENT}:
        raise ValidationError("Only pharmacy or procurement staff may open a stock count.")
    cutoff = timezone.now()
    count = StockCount.objects.create(
        location=location,
        cutoff_at=cutoff,
        blind_count=blind_count,
        notes=notes.strip(),
        counted_by=actor,
    )
    batches = StockBatch.objects.select_related("item").order_by("item__name", "expiry_date")
    for batch in batches:
        StockCountLine.objects.create(
            count=count,
            batch=batch,
            expected_quantity=batch.balance_at(cutoff),
            counted_quantity=Decimal("0.000"),
        )
    audit(actor, "stock_count.opened", count, after={"lines": count.lines.count(), "blind": blind_count}, request=request)
    return count


@transaction.atomic
def submit_stock_count(*, actor, count_id, counted, reasons=None, request=None):
    """Record the counted quantities and send the sheet for independent review."""
    reasons = reasons or {}
    count = StockCount.objects.select_for_update().get(pk=count_id)
    if count.counted_by_id != actor.id:
        raise ValidationError("Only the person who opened this count may submit it.")
    if count.status != StockCount.Status.FROZEN:
        raise ValidationError("This count has already been submitted.")
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
    count.save(update_fields=["status", "updated_at"])
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

    posted = 0
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
            if abs(line.variance_value) > setting_decimal("stock_variance_review_value", Decimal("500.00")):
                ExceptionRecord.objects.get_or_create(
                    category="stock_discrepancy",
                    summary=f"Approved stock adjustment for {line.batch.item.name} batch {line.batch.batch_number}"[:255],
                    defaults={
                        "severity": "warning",
                        "evidence": f"{count.reference}: counted {line.counted_quantity}, expected {line.expected_quantity}, value KES {line.variance_value:,.2f}. Reason recorded: {line.reason or 'none given'}.",
                    },
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
        if quantity > on_hand:
            raise ValidationError(
                f"Only {on_hand} {batch.item.base_unit or 'units'} of {batch.item.name} batch {batch.batch_number} are on hand."
            )
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
            ExceptionRecord.objects.get_or_create(
                category="departmental_waste",
                summary=f"Waste recorded in {issue.department}: {line.batch.item.name}"[:255],
                defaults={
                    "severity": "warning",
                    "evidence": f"{issue.reference}: {wasted} {line.batch.item.base_unit or 'units'} of batch {line.batch.batch_number} recorded as wasted by {actor.username}.",
                },
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
        ExceptionRecord.objects.get_or_create(
            category="stock_write_off",
            summary=f"Stock written off: {write_off.batch.item.name} batch {write_off.batch.batch_number}"[:255],
            defaults={
                "severity": "warning",
                "evidence": f"{write_off.reference}: {write_off.quantity} {write_off.batch.item.base_unit or 'units'} "
                            f"worth KES {write_off.value_at_cost:,.2f}, reason {write_off.get_reason_display().lower()}, "
                            f"approved by {actor.username}.",
            },
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
