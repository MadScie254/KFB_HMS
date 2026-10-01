from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from . import billing_services as _billing_services
from . import stock_count_services as _stock_count_services
from . import stock_custody_services as _stock_custody_services
from .models import (
    Admission,
    CatalogueItem,
    ClinicalNote,
    ClinicianPayable,
    Encounter,
    EyeCase,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceLine,
    PharmacyOrder,
    PharmacyOrderItem,
    PurchaseOrder,
    Role,
    ServiceOrder,
    StockBatch,
    StockMovement,
    Supplier,
    SupplierChangeRequest,
)
from .permissions import user_role
from .pricing import active_price_versions
from .service_common import audit, deterministic_key, raise_exception, setting_decimal

# Defaults for thresholds an implementer is expected to review. They are read
# through Setting so a site can change them without a code change, and they are
# deliberately conservative rather than silent.
NEAR_EXPIRY_DAYS = 90
COST_VARIANCE_FRACTION = Decimal("0.10")
INVOICE_TOLERANCE = Decimal("1.00")
STOCK_COST_PRECISION = Decimal("0.000001")


# Nulls sort first on SQLite and last on PostgreSQL. Left to the database, a
# batch with no recorded expiry would be dispensed first in the demo and last
# in production — the same order, the same stock, a different batch off the
# shelf. FEFO has to mean one thing, so the ordering is stated explicitly.
FEFO_ORDER = (F("expiry_date").asc(nulls_last=True), "created_at", "pk")

# Preserve the established import surface used by views, admin, and tests.
record_payment = _billing_services.record_payment
_matching_payment_retry = _billing_services._matching_payment_retry
open_cash_shift = _billing_services.open_cash_shift
close_cash_shift = _billing_services.close_cash_shift
review_cash_shift = _billing_services.review_cash_shift
request_refund = _billing_services.request_refund
review_refund = _billing_services.review_refund
pay_refund = _billing_services.pay_refund
review_mpesa = _billing_services.review_mpesa
approve_credit_note = _billing_services.approve_credit_note

open_stock_count = _stock_count_services.open_stock_count
submit_stock_count = _stock_count_services.submit_stock_count
review_stock_count = _stock_count_services.review_stock_count

issue_to_department = _stock_custody_services.issue_to_department
account_for_issue = _stock_custody_services.account_for_issue
request_write_off = _stock_custody_services.request_write_off
review_write_off = _stock_custody_services.review_write_off
set_batch_disposition = _stock_custody_services.set_batch_disposition


def active_price(item):
    return active_price_versions().filter(item=item).first()


@transaction.atomic
def close_encounter(*, actor, encounter_id, reason, request=None):
    if user_role(actor) not in {Role.CLINICIAN, Role.OWNER}:
        raise ValidationError("Only a clinician or owner may close an encounter.")
    encounter = Encounter.objects.select_for_update().get(pk=encounter_id)
    if encounter.status == Encounter.Status.CLOSED:
        return encounter
    if Admission.objects.filter(encounter=encounter, discharged_at__isnull=True).exists():
        raise ValidationError("Discharge the active admission before closing this encounter.")
    reason = reason.strip()
    if not reason:
        raise ValidationError("Record why the clinical encounter is being closed.")
    encounter.status = Encounter.Status.CLOSED
    encounter.closed_at = timezone.now()
    encounter.closed_by = actor
    encounter.closure_reason = reason
    encounter.save(update_fields=["status", "closed_at", "closed_by", "closure_reason", "updated_at"])
    audit(actor, "encounter.closed", encounter, reason=reason, request=request)
    return encounter


@transaction.atomic
def discharge_admission(*, actor, admission_id, summary, request=None):
    if user_role(actor) not in {Role.CLINICIAN, Role.OWNER}:
        raise ValidationError("Only a clinician or owner may discharge an admission.")
    encounter_id = Admission.objects.values_list("encounter_id", flat=True).get(pk=admission_id)
    Encounter.objects.select_for_update().get(pk=encounter_id)
    admission = Admission.objects.select_for_update().get(pk=admission_id)
    if admission.discharged_at is not None:
        return admission
    summary = summary.strip()
    if not summary:
        raise ValidationError("Record a clinical discharge summary.")
    admission.discharge_summary = summary
    admission.discharged_at = timezone.now()
    admission.discharged_by = actor
    admission.clinical_status = "discharged"
    admission.save(update_fields=["discharge_summary", "discharged_at", "discharged_by", "clinical_status", "updated_at"])
    audit(actor, "admission.discharged", admission, reason=summary, request=request)
    close_encounter(actor=actor, encounter_id=encounter_id, reason=f"Discharged: {summary}", request=request)
    return admission


@transaction.atomic
def update_service_order(*, actor, order_id, status, result, request=None):
    if user_role(actor) not in {Role.LAB, Role.CLINICIAN, Role.OWNER}:
        raise ValidationError("Only authorised clinical or laboratory staff may update a service order.")
    order = ServiceOrder.objects.select_for_update().select_related("encounter__patient", "service").get(pk=order_id)
    if order.status == ServiceOrder.Status.RELEASED:
        if (
            status == ServiceOrder.Status.RELEASED
            and actor.pk == order.reviewed_by_id
            and result.strip() == order.result
            and InvoiceLine.objects.filter(service_order=order).exists()
        ):
            return order
        raise ValidationError("This result is already released and cannot be changed.")
    next_status = {
        ServiceOrder.Status.REQUESTED: ServiceOrder.Status.IN_PROGRESS,
        ServiceOrder.Status.IN_PROGRESS: ServiceOrder.Status.REVIEW,
        ServiceOrder.Status.REVIEW: ServiceOrder.Status.RELEASED,
    }.get(order.status)
    if status != next_status:
        raise ValidationError(f"Move this service order from {order.get_status_display()} to the next review step before release.")
    if status == ServiceOrder.Status.RELEASED:
        if order.requested_by_id == actor.id or order.performer_id == actor.id:
            raise ValidationError("The requester or performer cannot release their own result. Independent review is required.")
        if not order.result.strip() or result.strip() != order.result:
            raise ValidationError("Release the reviewed result without editing it. Ask the performer to correct it first.")
        price = active_price(order.service)
        if price is None:
            raise ValidationError(f"{order.service.name} has no active approved price. Release cannot post a zero charge.")
        order.status = ServiceOrder.Status.RELEASED
        order.reviewed_by = actor
        order.reviewed_at = timezone.now()
        order.released_at = timezone.now()
        order.save(update_fields=["status", "reviewed_by", "reviewed_at", "released_at", "updated_at"])
        invoice = Invoice.objects.create(
            patient=order.encounter.patient, encounter=order.encounter,
            customer_name=order.encounter.patient.full_name,
            status=Invoice.Status.POSTED, posted_at=order.released_at,
            created_by=actor,
        )
        InvoiceLine.objects.create(
            invoice=invoice, service_order=order, item=order.service,
            description=order.service.name, department=order.service.department,
            quantity=Decimal("1"), unit_price=price.amount, price_version=price,
        )
        encounter = order.encounter
        if encounter.status == Encounter.Status.TESTS and not ServiceOrder.objects.filter(encounter=encounter).exclude(
            status=ServiceOrder.Status.RELEASED
        ).exists():
            has_signed_note = ClinicalNote.objects.filter(encounter=encounter, status=ClinicalNote.Status.SIGNED).exists()
            encounter.status = Encounter.Status.PHARMACY if has_signed_note else Encounter.Status.CLINICIAN
            encounter.save(update_fields=["status", "updated_at"])
        audit(actor, "service_order.released", order, after={"invoice": invoice.invoice_number}, request=request)
        return order
    if status == ServiceOrder.Status.REVIEW and order.performer_id != actor.id:
        raise ValidationError("Only the recorded performer may submit this result for review.")
    if status == ServiceOrder.Status.REVIEW and not result.strip():
        raise ValidationError("Enter a result before submitting it for review.")
    order.status = status
    order.result = result.strip()
    if status == ServiceOrder.Status.IN_PROGRESS:
        order.performer = actor
    order.save(update_fields=["status", "result", "performer", "updated_at"])
    audit(actor, f"service_order.{status}", order, request=request)
    return order


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
    current_prices = {}
    for price in active_price_versions().filter(item_id__in={item.pk for item, _ in items}):
        current_prices.setdefault(price.item_id, price)
    for item, quantity in items:
        if item.kind != CatalogueItem.Kind.PRODUCT or quantity <= 0:
            raise ValidationError("Every basket line must be an active product with a positive quantity.")
        price = current_prices.get(item.pk)
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
            .order_by(*FEFO_ORDER)
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
def approve_purchase_order(*, actor, order_id, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only an independent reviewer may approve a purchase order.")
    order = PurchaseOrder.objects.select_for_update().get(pk=order_id)
    if order.requested_by_id == actor.id:
        raise ValidationError("The requester cannot approve their own purchase order.")
    if order.status != "requested":
        raise ValidationError("Only a requested purchase order can be approved.")
    order.approved_by = actor
    order.status = "approved"
    order.save(update_fields=["approved_by", "status", "updated_at"])
    audit(actor, "purchase_order.approved", order, request=request)
    return order


@transaction.atomic
def request_supplier_change(*, actor, supplier_id, proposed_name, proposed_phone, proposed_payment_details,
                            proposed_active, reason, request=None):
    if user_role(actor) not in {Role.PROCUREMENT, Role.OWNER}:
        raise ValidationError("Only procurement or the owner may request a supplier change.")
    if not proposed_name.strip() or not reason.strip():
        raise ValidationError("Enter a supplier name and a reason for the change.")
    supplier = Supplier.objects.select_for_update().get(pk=supplier_id)
    change = SupplierChangeRequest.objects.create(
        supplier=supplier, proposed_name=proposed_name.strip(), proposed_phone=proposed_phone.strip(),
        proposed_payment_details=proposed_payment_details.strip(), proposed_active=proposed_active,
        reason=reason.strip(), supplier_updated_at=supplier.updated_at, requested_by=actor,
    )
    audit(actor, "supplier_change.requested", change, reason=change.reason, request=request)
    return change


@transaction.atomic
def review_supplier_change(*, actor, change_id, approve, request=None):
    if user_role(actor) not in {Role.REVIEWER, Role.OWNER}:
        raise ValidationError("Only an independent reviewer may review supplier changes.")
    supplier_id = SupplierChangeRequest.objects.values_list("supplier_id", flat=True).get(pk=change_id)
    supplier = Supplier.objects.select_for_update().get(pk=supplier_id)
    change = SupplierChangeRequest.objects.select_for_update().get(pk=change_id)
    if actor.pk == change.requested_by_id:
        raise ValidationError("You cannot approve your own supplier change.")
    if change.status != SupplierChangeRequest.Status.PENDING:
        raise ValidationError("This supplier change was already reviewed.")
    if approve and supplier.updated_at != change.supplier_updated_at:
        raise ValidationError("Supplier details changed after this request. Submit a new change for review.")
    if approve:
        before = {field: getattr(supplier, field) for field in (
            "name", "phone", "payment_details", "active",
        )}
        supplier.name = change.proposed_name
        supplier.phone = change.proposed_phone
        supplier.payment_details = change.proposed_payment_details
        supplier.active = change.proposed_active
        try:
            with transaction.atomic():
                supplier.save(update_fields=["name", "phone", "payment_details", "active", "updated_at"])
        except IntegrityError as exc:
            raise ValidationError("A supplier with that name already exists.") from exc
        audit(actor, "supplier.changed", supplier, before=before, after={
            field: getattr(supplier, field) for field in before
        }, reason=change.reason, request=request)
    change.status = SupplierChangeRequest.Status.APPROVED if approve else SupplierChangeRequest.Status.REJECTED
    change.reviewed_by = actor
    change.reviewed_at = timezone.now()
    change.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    audit(actor, f"supplier_change.{change.status}", change, reason=change.reason, request=request)
    return change


@transaction.atomic
def complete_eye_case(*, actor, case_id, request=None):
    if user_role(actor) not in {Role.EYE, Role.CLINICIAN}:
        raise ValidationError("Only authorised clinical eye staff may complete a case.")
    case = EyeCase.objects.select_for_update().select_related("session").get(pk=case_id)
    if case.status == "completed":
        return case
    if case.status not in {"waiting", "confirmed"}:
        raise ValidationError("A cancelled eye case cannot be completed.")
    if case.readiness != "ready":
        raise ValidationError("Clinical readiness must be confirmed before completion.")
    if case.payment_status != "paid":
        raise ValidationError("Payment clearance is required before completing an eye case.")
    if case.session_id and case.session.status == "cancelled":
        raise ValidationError("A case in a cancelled eye session cannot be completed.")
    case.status = "completed"
    case.completed_at = timezone.now()
    case.save(update_fields=["status", "completed_at", "updated_at"])
    payable, _ = ClinicianPayable.objects.get_or_create(eye_case=case, defaults={"amount": Decimal("2000.00")})
    audit(actor, "eye_case.completed", case, after={"payable": str(payable.amount)}, request=request)
    return case


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

    order_lines = list(order.lines.select_related("item"))
    received_totals = {
        row["order_line_id"]: row["total"]
        for row in GoodsReceiptLine.objects.filter(order_line__order=order)
        .values("order_line_id").annotate(total=Sum("quantity_received"))
    }
    delivered_value = sum(
        ((quantity * unit_cost).quantize(Decimal("0.01")) for _, quantity, _, _, unit_cost in prepared),
        Decimal("0.00"),
    )

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
        batch = StockBatch.objects.select_for_update().get(pk=batch.pk)
        on_hand = batch.quantity_on_hand
        previous_cost = batch.purchase_cost_per_base_unit
        batch.purchase_cost_per_base_unit = (
            ((on_hand * previous_cost + quantity * unit_cost) / (on_hand + quantity))
            if on_hand > 0 else unit_cost
        ).quantize(STOCK_COST_PRECISION)
        batch.save(update_fields=["purchase_cost_per_base_unit", "updated_at"])
        StockMovement.objects.create(
            batch=batch,
            movement_type=StockMovement.MovementType.RECEIPT,
            quantity_delta=quantity,
            unit_cost_at_event=unit_cost,
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

        received_total = received_totals.get(order_line.pk, Decimal("0.000")) + quantity
        received_totals[order_line.pk] = received_total
        if received_total > Decimal(str(order_line.quantity_base_units)):
            flags.append((
                "warning",
                f"Delivered quantity exceeds the approved order for {order_line.item.name}",
                f"{receipt.receipt_number}: ordered {order_line.quantity_base_units}, received {received_total} {order_line.item.base_unit or 'units'} in total.",
            ))

    receipt.posted_at = timezone.now()
    receipt.save(update_fields=["posted_at", "updated_at"])

    fully_received = all(
        received_totals.get(line.pk, Decimal("0.000")) >= Decimal(str(line.quantity_base_units))
        for line in order_lines
    )
    order.status = "received" if fully_received else "part_received"
    order.save(update_fields=["status", "updated_at"])

    tolerance = setting_decimal("supplier_invoice_tolerance", INVOICE_TOLERANCE)
    variance = (invoice_amount - delivered_value).quantize(Decimal("0.01"))
    if abs(variance) > tolerance:
        flags.append((
            "warning",
            f"Supplier invoice {reference} does not match the goods counted in",
            f"{receipt.receipt_number}: invoice KES {invoice_amount:,.2f}, delivered value KES {delivered_value:,.2f}, difference KES {variance:,.2f}.",
        ))

    for index, (severity, summary, evidence) in enumerate(flags):
        raise_exception(
            "purchase_discrepancy", summary, evidence, severity=severity,
            dedupe_on=("purchase_discrepancy", receipt.pk, index, summary),
        )

    audit(
        actor,
        "goods_receipt.posted",
        receipt,
        after={
            "order": order.order_number,
            "invoice": reference,
            "lines": len(prepared),
            "delivered_value": str(delivered_value),
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
