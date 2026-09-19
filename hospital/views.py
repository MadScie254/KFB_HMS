import csv
import hashlib
import io
import mimetypes
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Case, Count, DecimalField, IntegerField, Max, Q, Sum, Value, When
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header, urlencode

from .analytics import (
    control_adoption,
    departmental_custody,
    receiving_summary,
    shrinkage,
    stock_activity,
    stock_position,
    supplier_price_history,
)
from .forms import (
    AdmissionForm,
    BatchDispositionForm,
    ClinicalAttachmentForm,
    ClinicalNoteForm,
    CreditNoteForm,
    CsvImportForm,
    DeliveryCheckForm,
    DepartmentIssueForm,
    DepartmentIssueLineFormSet,
    EncounterForm,
    GoodsReceiptForm,
    GoodsReceiptLineFormSet,
    PatientForm,
    PaymentForm,
    PharmacyBasketForm,
    PrescriptionForm,
    PrescriptionFormSet,
    PurchaseOrderForm,
    PurchaseOrderLineFormSet,
    ServiceOrderForm,
    ServiceResultForm,
    ShiftCloseForm,
    ShiftOpenForm,
    StockCountOpenForm,
    StockCountReviewForm,
    WriteOffRequestForm,
    WriteOffReviewForm,
)
from .models import (
    Admission,
    AuditEvent,
    Bed,
    CashShift,
    CatalogueItem,
    ClinicalAttachment,
    ClinicalNote,
    ClinicianPayable,
    CreditNote,
    DepartmentIssue,
    Encounter,
    ExceptionRecord,
    EyeCase,
    EyeSession,
    GoodsReceipt,
    ImportJob,
    Invoice,
    InvoiceLine,
    LoginAttempt,
    Patient,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    Prescription,
    PrescriptionItem,
    PriceVersion,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    ServiceOrder,
    Setting,
    StockCount,
    StockMovement,
    StockWriteOff,
    Ward,
)
from .pdf_reports import build_financial_report_pdf, build_patient_access_pdf
from .permissions import role_required, user_role
from .services import (
    account_for_issue,
    approve_credit_note,
    approve_purchase_order,
    audit,
    check_delivery,
    complete_eye_case,
    dispense_order,
    issue_to_department,
    open_stock_count,
    prepare_pharmacy_order,
    receive_delivery,
    record_payment,
    request_write_off,
    review_stock_count,
    review_write_off,
    set_batch_disposition,
    submit_stock_count,
    verify_mpesa,
)

ZERO_MONEY = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))


def _validation_message(exc):
    if hasattr(exc, "messages"):
        return " ".join(exc.messages)
    return str(exc)


def _period_days(value, default=7):
    """A safe reporting window from a query string, clamped to one year."""
    days = int(value) if str(value).isdigit() else default
    return max(1, min(days, 365))


def _filter_query(**params):
    """Query-string tail that keeps active filters on pagination links."""
    active = {key: value for key, value in params.items() if value not in (None, "")}
    return f"&{urlencode(active)}" if active else ""


def invoiced_total(invoices):
    """Billed value of an invoice queryset in one query."""
    return InvoiceLine.objects.filter(invoice__in=invoices).aggregate(v=Sum("line_total"))["v"] or Decimal("0.00")


def outstanding_receivables():
    """Posted-but-unsettled value: billed − approved credits − valid allocations.

    Three aggregates regardless of ledger size, in place of two queries per
    open invoice.
    """
    open_invoices = Invoice.objects.exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.PAID])
    billed = invoiced_total(open_invoices)
    credited = CreditNote.objects.filter(
        invoice__in=open_invoices, status=CreditNote.Status.APPROVED
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    paid = PaymentAllocation.objects.filter(
        invoice__in=open_invoices, payment__status=Payment.Status.VALID
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    return billed - credited - paid


def health(request):
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database_ok = False
    status = 200 if database_ok else 503
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"status": "ok" if database_ok else "unavailable", "database": database_ok, "demo": settings.DEMO_MODE}, status=status)
    return render(request, "hospital/health.html", {"database_ok": database_ok}, status=status)


@login_required
def dashboard(request):
    role = user_role(request.user)
    today = timezone.localdate()
    start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    context = {
        "role": role,
        "open_encounters": Encounter.objects.exclude(status=Encounter.Status.CLOSED).count(),
        "today_patients": Patient.objects.filter(created_at__gte=start).count(),
        "open_orders": PharmacyOrder.objects.exclude(status__in=[PharmacyOrder.Status.DISPENSED, PharmacyOrder.Status.CANCELLED]).count(),
        "exceptions": ExceptionRecord.objects.exclude(status=ExceptionRecord.Status.RESOLVED).order_by("-created_at")[:6],
        "queue": Encounter.objects.exclude(status=Encounter.Status.CLOSED).select_related("patient").order_by("created_at")[:8],
        "my_shift": CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN).first(),
    }
    if role in {Role.PHARMACY, Role.PROCUREMENT, Role.OWNER}:
        # The people who can act on a shortage are the only ones shown one.
        position = stock_position()
        context.update({
            "stock": position,
            "low_stock": position["below_reorder"][:6],
            "expiring_stock": position["expiring_soon"][:6],
            "unchecked_deliveries": GoodsReceipt.objects.filter(checked_by__isnull=True).count(),
        })
    if role == Role.OWNER:
        valid_payments = Payment.objects.filter(status=Payment.Status.VALID, received_at__gte=start)
        posted = Invoice.objects.filter(posted_at__gte=start).exclude(status=Invoice.Status.DRAFT)
        context.update({
            "net_billed": invoiced_total(posted),
            "verified_collections": valid_payments.filter(Q(method=Payment.Method.CASH) | Q(verification_status__in=[Payment.Verification.MANUAL, Payment.Verification.PROVIDER])).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
            "unverified_mpesa": valid_payments.filter(method=Payment.Method.MPESA, verification_status=Payment.Verification.UNVERIFIED).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
            "receivables": outstanding_receivables(),
            "occupied_beds": Admission.objects.filter(discharged_at__isnull=True).count(),
            "active_beds": Bed.objects.filter(active=True, ward__active=True).count(),
            "eye_waiting": EyeCase.objects.filter(status="waiting").count(),
        })
    return render(request, "hospital/dashboard.html", context)


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.OWNER, Role.EYE)
def patient_list(request):
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(patient_number__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone__icontains=query)
            | Q(id_number__iexact=query)
            | Q(guardian_phone__icontains=query)
        )
    page = Paginator(patients, 50).get_page(request.GET.get("page"))
    return render(request, "hospital/patient_list.html", {"patients": page, "page": page, "query": query})


@role_required(Role.RECEPTION)
def patient_create(request):
    form = PatientForm(request.POST or None)
    duplicate_candidates = []
    if request.method == "POST" and form.is_valid():
        duplicate_candidates = Patient.objects.filter(
            first_name__iexact=form.cleaned_data["first_name"],
            last_name__iexact=form.cleaned_data["last_name"],
        )
        if form.cleaned_data.get("phone"):
            duplicate_candidates = duplicate_candidates.filter(phone=form.cleaned_data["phone"])
        if duplicate_candidates.exists() and request.POST.get("confirm_duplicate") != "yes":
            messages.warning(request, "Possible matching patient found. Review before creating a separate record.")
        else:
            patient = form.save(commit=False)
            patient.registered_by = request.user
            patient.is_demo = settings.DEMO_MODE
            patient.save()
            audit(request.user, "patient.registered", patient, request=request)
            messages.success(request, f"Patient {patient.patient_number} registered.")
            return redirect("patient_detail", pk=patient.pk)
    return render(request, "hospital/patient_form.html", {"form": form, "duplicates": duplicate_candidates})


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.OWNER, Role.EYE)
def patient_detail(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    role = user_role(request.user)
    audit(request.user, "patient.viewed", patient, request=request)
    invoices = patient.invoices.all() if role in {Role.RECEPTION, Role.OWNER} else []
    notes = ClinicalNote.objects.filter(encounter__patient=patient) if role in {Role.CLINICIAN, Role.NURSE, Role.OWNER} else []
    attachments = patient.attachments.select_related("uploaded_by", "encounter") if role in {Role.CLINICIAN, Role.NURSE, Role.OWNER} else []
    return render(request, "hospital/patient_detail.html", {
        "patient": patient,
        "invoices": invoices,
        "notes": notes,
        "encounters": patient.encounters.all(),
        "attachments": attachments,
        "attachment_form": ClinicalAttachmentForm() if role in {Role.CLINICIAN, Role.NURSE} else None,
    })


@role_required(Role.CLINICIAN, Role.NURSE)
def patient_attachment_upload(request, pk):
    if request.method != "POST":
        raise Http404
    patient = get_object_or_404(Patient, pk=pk)
    form = ClinicalAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        attachment = form.save(commit=False)
        attachment.patient = patient
        attachment.original_name = Path(attachment.file.name).name[:255]
        attachment.uploaded_by = request.user
        attachment.save()
        audit(request.user, "clinical_attachment.uploaded", attachment, after={"name": attachment.original_name}, request=request)
        messages.success(request, "Clinical attachment uploaded securely.")
    else:
        messages.error(request, " ".join(
            error for errors in form.errors.values() for error in errors
        ))
    return redirect("patient_detail", pk=patient.pk)


@role_required(Role.CLINICIAN, Role.NURSE, Role.OWNER)
def patient_attachment_download(request, pk):
    attachment = get_object_or_404(ClinicalAttachment.objects.select_related("patient"), pk=pk)
    content_type = mimetypes.guess_type(attachment.original_name)[0] or "application/octet-stream"
    attachment.file.open("rb")
    response = FileResponse(attachment.file, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(True, attachment.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "clinical_attachment.downloaded", attachment, request=request)
    return response


@role_required(Role.RECEPTION, Role.OWNER)
def patient_access_pdf(request, pk):
    patient = get_object_or_404(Patient.objects.prefetch_related("encounters__clinical_notes__author"), pk=pk)
    pdf = build_patient_access_pdf(
        patient=patient,
        hospital_name=settings.HOSPITAL_NAME,
        generated_by=str(request.user.staff_profile),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(
        True, f"KFBH-patient-record-{patient.patient_number}.pdf"
    )
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "patient.exported_pdf", patient, request=request)
    return response


@role_required(Role.RECEPTION, Role.OWNER)
def credit_note_create(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("patient"), pk=pk)
    form = CreditNoteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        note = form.save(commit=False)
        if note.amount > invoice.balance:
            form.add_error("amount", "Credit cannot exceed the current invoice balance.")
        else:
            note.invoice = invoice
            note.requested_by = request.user
            note.save()
            audit(request.user, "credit_note.requested", note, reason=note.reason, request=request)
            messages.success(request, "Credit note sent for independent review.")
            return redirect("patient_detail", pk=invoice.patient_id) if invoice.patient_id else redirect("reports")
    return render(request, "hospital/credit_note_form.html", {"form": form, "invoice": invoice})


@role_required(Role.RECEPTION, Role.CLINICIAN)
def encounter_create(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    form = EncounterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        encounter = form.save(commit=False)
        encounter.patient = patient
        encounter.started_by = request.user
        encounter.status = Encounter.Status.TRIAGE
        encounter.save()
        if encounter.urgency == "emergency":
            ExceptionRecord.objects.create(category="emergency_override", summary=f"Emergency override for {encounter.encounter_number}", evidence=encounter.emergency_override_reason)
        audit(request.user, "encounter.started", encounter, request=request)
        messages.success(request, f"Visit {encounter.encounter_number} started.")
        return redirect("queue")
    return render(request, "hospital/encounter_form.html", {"form": form, "patient": patient})


#: Clinical priority. Never sort the queue on the raw ``urgency`` string —
#: alphabetically "routine" precedes "urgent", which pushes urgent patients
#: below routine ones.
TRIAGE_RANK = Case(
    When(urgency="emergency", then=Value(0)),
    When(urgency="urgent", then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.LAB)
def queue(request):
    encounters = (
        Encounter.objects.exclude(status=Encounter.Status.CLOSED)
        .select_related("patient", "assigned_clinician")
        .annotate(triage_rank=TRIAGE_RANK)
        .order_by("triage_rank", "created_at")
    )
    return render(request, "hospital/queue.html", {"encounters": encounters})


@role_required(Role.CLINICIAN)
def clinical_note(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    draft = ClinicalNote.objects.filter(encounter=encounter, author=request.user, status=ClinicalNote.Status.DRAFT).first()
    form = ClinicalNoteForm(request.POST or None, instance=draft, initial={"expected_version": draft.version if draft else 0})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Serialise note numbering on the encounter. Two tabs can no longer
            # calculate the same next version and collide on the unique key.
            locked_encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
            current_draft = ClinicalNote.objects.select_for_update().filter(
                encounter=locked_encounter,
                author=request.user,
                status=ClinicalNote.Status.DRAFT,
            ).first()
            expected_version = form.cleaned_data.get("expected_version") or 0
            if current_draft and expected_version not in {0, current_draft.version}:
                form.add_error(None, "This note changed in another tab. Reload before saving again.")
            else:
                locked_form = ClinicalNoteForm(request.POST, instance=current_draft)
                if locked_form.is_valid():
                    note = locked_form.save(commit=False)
                    if not note.pk:
                        note.encounter = locked_encounter
                        note.author = request.user
                        note.version = (
                            ClinicalNote.objects.filter(
                                encounter=locked_encounter, author=request.user
                            ).aggregate(v=Max("version"))["v"]
                            or 0
                        ) + 1
                    note.save()
                    if request.POST.get("action") == "sign":
                        note.sign()
                        locked_encounter.status = Encounter.Status.PHARMACY
                        locked_encounter.save(update_fields=["status", "updated_at"])
                        audit(request.user, "clinical_note.signed", note, request=request)
                        messages.success(request, "Clinical note signed. Future changes require an attributed amendment.")
                    else:
                        audit(request.user, "clinical_note.saved", note, request=request)
                        messages.success(request, "Draft saved on the server.")
                    return redirect("patient_detail", pk=encounter.patient_id)
    return render(request, "hospital/clinical_note_form.html", {"form": form, "encounter": encounter, "draft": draft})


@role_required(Role.CLINICIAN)
def prescription_create(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    # Accept the original single-line payload as well as the new formset so
    # bookmarked clients and downtime back-entry tools keep working.
    legacy_payload = request.method == "POST" and "items-TOTAL_FORMS" not in request.POST
    form = PrescriptionForm(request.POST or None) if legacy_payload else None
    formset = PrescriptionFormSet(request.POST or None, prefix="items")
    is_valid = form.is_valid() if legacy_payload else formset.is_valid()
    if request.method == "POST" and is_valid:
        lines = [form.cleaned_data] if legacy_payload else [
            row.cleaned_data for row in formset
            if row.cleaned_data and not row.cleaned_data.get("DELETE")
        ]
        with transaction.atomic():
            prescription = Prescription.objects.create(encounter=encounter, prescriber=request.user, signed_at=timezone.now())
            for line in lines:
                PrescriptionItem.objects.create(
                    prescription=prescription,
                    product=line["product"], strength=line["strength"],
                    dose=line["dose"], route=line["route"], frequency=line["frequency"],
                    duration=line["duration"], quantity_base_units=line["quantity_base_units"],
                    instructions=line["instructions"],
                )
            audit(request.user, "prescription.signed", prescription, request=request)
        messages.success(request, f"Prescription with {len(lines)} item(s) signed and sent to pharmacy for pricing.")
        return redirect("patient_detail", pk=encounter.patient_id)
    return render(request, "hospital/prescription_form.html", {"form": form, "formset": formset, "encounter": encounter})


@role_required(Role.CLINICIAN)
def service_order_create(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    form = ServiceOrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        order = form.save(commit=False)
        order.encounter = encounter
        order.requested_by = request.user
        order.save()
        encounter.status = Encounter.Status.TESTS
        encounter.save(update_fields=["status", "updated_at"])
        audit(request.user, "service_order.requested", order, request=request)
        messages.success(request, f"{order.service.name} sent to {order.service.department}.")
        return redirect("departments")
    return render(request, "hospital/service_order_form.html", {"form": form, "encounter": encounter})


@role_required(Role.PHARMACY, Role.RECEPTION)
def pharmacy_orders(request):
    orders = PharmacyOrder.objects.select_related("patient", "invoice", "prepared_by").order_by("-created_at")[:100]
    pending_prescriptions = Prescription.objects.filter(status="active", pharmacyorder__isnull=True).select_related("encounter__patient", "prescriber").prefetch_related("items__product") if user_role(request.user) == Role.PHARMACY else []
    return render(request, "hospital/pharmacy_orders.html", {"orders": orders, "pending_prescriptions": pending_prescriptions})


@role_required(Role.PHARMACY)
def pharmacy_prepare_prescription(request, prescription_id):
    if request.method != "POST":
        raise Http404
    prescription = get_object_or_404(Prescription.objects.select_related("encounter__patient").prefetch_related("items__product"), pk=prescription_id, status="active")
    if PharmacyOrder.objects.filter(prescription=prescription).exists():
        messages.info(request, "This prescription already has a pharmacy order.")
        return redirect("pharmacy_orders")
    try:
        order = prepare_pharmacy_order(
            actor=request.user,
            customer_name=prescription.encounter.patient.full_name,
            patient=prescription.encounter.patient,
            encounter=prescription.encounter,
            prescription=prescription,
            items=[(item.product, item.quantity_base_units) for item in prescription.items.all()],
            request=request,
        )
        for order_item, prescription_item in zip(order.items.order_by("pk"), prescription.items.order_by("pk")):
            order_item.prescription_item = prescription_item
            order_item.save(update_fields=["prescription_item"])
        messages.success(request, f"Prescription priced as {order.order_number}; reception can now collect payment.")
        return redirect("pharmacy_order_detail", pk=order.pk)
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
        return redirect("pharmacy_orders")


@role_required(Role.PHARMACY)
def pharmacy_order_create(request):
    form = PharmacyBasketForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        patient = None
        if form.cleaned_data["patient_number"]:
            patient = Patient.objects.filter(patient_number__iexact=form.cleaned_data["patient_number"]).first()
            if not patient:
                form.add_error("patient_number", "No patient has that number.")
        if not form.errors:
            try:
                order = prepare_pharmacy_order(
                    actor=request.user,
                    customer_name=form.cleaned_data["customer_name"],
                    patient=patient,
                    items=[(form.cleaned_data["product"], form.cleaned_data["quantity"])],
                    request=request,
                )
                messages.success(request, f"Basket {order.order_number} sent to reception for payment.")
                return redirect("pharmacy_order_detail", pk=order.pk)
            except ValidationError as exc:
                form.add_error(None, _validation_message(exc))
    return render(request, "hospital/pharmacy_order_form.html", {"form": form})


@role_required(Role.PHARMACY, Role.RECEPTION)
def pharmacy_order_detail(request, pk):
    order = get_object_or_404(PharmacyOrder.objects.select_related("patient", "invoice", "prepared_by", "dispensed_by"), pk=pk)
    return render(request, "hospital/pharmacy_order_detail.html", {"order": order})


@role_required(Role.RECEPTION)
def invoice_payment(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = PaymentForm(request.POST or None, initial={"amount": invoice.balance})
    if request.method == "POST" and form.is_valid():
        key = request.POST.get("idempotency_key") or uuid.uuid4().hex
        try:
            payment = record_payment(
                actor=request.user,
                invoice_id=invoice.pk,
                amount=form.cleaned_data["amount"],
                method=form.cleaned_data["method"],
                reference=form.cleaned_data["reference"],
                idempotency_key=key,
                request=request,
            )
            messages.success(request, f"Payment recorded. Receipt {payment.receipt_number}.")
            return redirect("receipt", pk=payment.pk)
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
    return render(request, "hospital/payment_form.html", {"form": form, "invoice": invoice, "idempotency_key": uuid.uuid4().hex})


@role_required(Role.RECEPTION, Role.OWNER)
def receipt(request, pk):
    payment = get_object_or_404(Payment.objects.prefetch_related("allocations__invoice"), pk=pk)
    audit(request.user, "receipt.viewed", payment, reason="Original or duplicate print view", request=request)
    return render(request, "hospital/receipt.html", {"payment": payment, "duplicate": request.GET.get("reprint") == "1"})


@role_required(Role.PHARMACY)
def pharmacy_dispense(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        order = dispense_order(actor=request.user, order_id=pk, idempotency_key=request.POST.get("idempotency_key") or str(pk), request=request)
        messages.success(request, f"{order.order_number} dispensed and stock posted once.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("pharmacy_order_detail", pk=pk)


@role_required(Role.RECEPTION)
def shift_manage(request):
    shift = CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN).first()
    form = ShiftCloseForm(request.POST or None, instance=shift) if shift else ShiftOpenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if shift:
            closing = form.save(commit=False)
            closing.closed_at = timezone.now()
            closing.status = CashShift.Status.CLOSED
            closing.save()
            if closing.variance != 0:
                ExceptionRecord.objects.create(category="cash_variance", summary=f"{closing.label}: KES {closing.variance:,.2f} variance", evidence=closing.variance_reason)
            audit(request.user, "shift.closed", closing, after={"expected": str(closing.expected_cash), "actual": str(closing.actual_cash), "variance": str(closing.variance)}, request=request)
            messages.success(request, "Shift closed and submitted for independent review.")
        else:
            opening = form.save(commit=False)
            opening.cashier = request.user
            opening.save()
            audit(request.user, "shift.opened", opening, after={"float": str(opening.opening_float)}, request=request)
            messages.success(request, "Shift opened.")
        return redirect("dashboard")
    return render(request, "hospital/shift_form.html", {"form": form, "shift": shift})


STOCK_VIEWS = {
    "all": "All stock",
    "low": "At or below reorder level",
    "expiring": "Expiring soon",
    "expired": "Expired",
    "quarantine": "Quarantined",
}


@role_required(Role.PHARMACY, Role.PROCUREMENT, Role.OWNER, Role.REVIEWER)
def stock_view(request):
    """Stock position, valuation and the movements that produced them."""
    query = request.GET.get("q", "").strip()
    selected = request.GET.get("view", "all")
    if selected not in STOCK_VIEWS:
        selected = "all"
    days = _period_days(request.GET.get("days", "7"))

    position = stock_position()
    activity = stock_activity(days)

    products = position["products"]
    if selected == "low":
        products = [row for row in products if row["below_reorder"]]
    elif selected == "expiring":
        products = [row for row in products if row["has_near_expiry"]]
    elif selected == "expired":
        products = [row for row in products if row["has_expired"]]
    elif selected == "quarantine":
        quarantined_items = {row["item"].pk for row in position["quarantined"]}
        products = [row for row in products if row["item"].pk in quarantined_items]
    if query:
        needle = query.lower()
        products = [
            row for row in products
            if needle in row["item"].name.lower() or needle in row["item"].code.lower()
        ]

    batches_by_item = {}
    for row in position["rows"]:
        batches_by_item.setdefault(row["item"].pk, []).append(row)
    for row in products:
        row["batches"] = batches_by_item.get(row["item"].pk, [])

    movements = StockMovement.objects.select_related(
        "batch__item", "entered_by__staff_profile"
    ).order_by("-event_at", "-entered_at")
    if query:
        movements = movements.filter(
            Q(batch__item__name__icontains=query)
            | Q(batch__item__code__icontains=query)
            | Q(batch__batch_number__icontains=query)
        )
    page = Paginator(movements, 40).get_page(request.GET.get("page"))

    return render(request, "hospital/stock.html", {
        "position": position,
        "activity": activity,
        "products": products,
        "movements": page,
        "page": page,
        "filter_query": _filter_query(q=query, view=selected, days=days),
        "query": query,
        "selected_view": selected,
        "selected_view_label": STOCK_VIEWS[selected],
        "stock_views": STOCK_VIEWS,
        "days": days,
        "can_receive": user_role(request.user) in {Role.PROCUREMENT, Role.PHARMACY},
        "can_count": user_role(request.user) in {Role.PHARMACY, Role.PROCUREMENT},
    })


@role_required(Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER)
def deliveries(request):
    """Every delivery received, with its invoice evidence and check status."""
    receipts = GoodsReceipt.objects.select_related(
        "purchase_order__supplier", "received_by__staff_profile", "checked_by__staff_profile"
    ).prefetch_related("lines__order_line__item")
    awaiting = receipts.filter(checked_by__isnull=True)[:20]
    open_orders = PurchaseOrder.objects.filter(
        status__in=["approved", "part_received"]
    ).select_related("supplier").prefetch_related("lines__item").order_by("-created_at")
    return render(request, "hospital/deliveries.html", {
        "receipts": receipts[:50],
        "awaiting_check": awaiting,
        "open_orders": open_orders,
        "summary": receiving_summary(30),
        "can_receive": user_role(request.user) in {Role.PROCUREMENT, Role.PHARMACY},
    })


@role_required(Role.PROCUREMENT, Role.PHARMACY)
def goods_receipt_create(request, pk):
    """Record what physically arrived and photograph the invoice that came with it."""
    order = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier").prefetch_related("lines__item"), pk=pk
    )
    if order.status not in {"approved", "part_received"}:
        messages.error(request, "Only an independently approved order can receive stock.")
        return redirect("deliveries")

    form = GoodsReceiptForm(request.POST or None, request.FILES or None)
    lines = GoodsReceiptLineFormSet(request.POST or None, prefix="lines", order=order)
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        payload = [
            {
                "order_line": row.cleaned_data["order_line"],
                "quantity_received": row.cleaned_data["quantity_received"],
                "batch_number": row.cleaned_data["batch_number"],
                "expiry_date": row.cleaned_data.get("expiry_date"),
                "actual_unit_cost": row.cleaned_data["actual_unit_cost"],
            }
            for row in lines
            if row.cleaned_data and not row.cleaned_data.get("DELETE")
        ]
        delivered_on = form.cleaned_data["delivered_on"]
        try:
            receipt = receive_delivery(
                actor=request.user,
                purchase_order_id=order.pk,
                supplier_invoice_reference=form.cleaned_data["supplier_invoice_reference"],
                invoice_amount=form.cleaned_data["invoice_amount"],
                invoice_date=form.cleaned_data.get("invoice_date"),
                invoice_photo=form.cleaned_data["invoice_photo"],
                lines=payload,
                delivered_at=timezone.make_aware(
                    timezone.datetime.combine(delivered_on, timezone.localtime().time())
                ),
                request=request,
            )
        except ValidationError as exc:
            messages.error(request, _validation_message(exc))
        else:
            messages.success(
                request,
                f"{receipt.receipt_number} posted. Stock increased against invoice {receipt.supplier_invoice_reference}; a different member of staff must now check the delivery.",
            )
            return redirect("goods_receipt_detail", pk=receipt.pk)

    received_so_far = {
        line.pk: line.receipt_lines.aggregate(total=Sum("quantity_received"))["total"] or Decimal("0.000")
        for line in order.lines.all()
    }
    return render(request, "hospital/goods_receipt_form.html", {
        "form": form,
        "formset": lines,
        "order": order,
        "received_so_far": [
            (line, received_so_far.get(line.pk, Decimal("0.000")), Decimal(str(line.quantity_base_units)) - received_so_far.get(line.pk, Decimal("0.000")))
            for line in order.lines.all()
        ],
    })


@role_required(Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER)
def goods_receipt_detail(request, pk):
    receipt = get_object_or_404(
        GoodsReceipt.objects.select_related(
            "purchase_order__supplier", "received_by__staff_profile", "checked_by__staff_profile"
        ).prefetch_related("lines__order_line__item", "lines__batch"),
        pk=pk,
    )
    movements = StockMovement.objects.filter(
        reference_type="GoodsReceipt", reference_id=str(receipt.pk)
    ).select_related("batch__item")
    evidence_name = receipt.invoice_photo_name or (receipt.invoice_photo.name if receipt.invoice_photo else "")
    return render(request, "hospital/goods_receipt_detail.html", {
        "receipt": receipt,
        "movements": movements,
        "check_form": DeliveryCheckForm(),
        # A PDF scan cannot be shown inline, so the checker is offered the file.
        "is_pdf_evidence": evidence_name.lower().endswith(".pdf"),
        "can_check": (
            receipt.checked_by_id is None
            and receipt.received_by_id != request.user.id
            and user_role(request.user) in {Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER}
        ),
    })


@role_required(Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER)
def goods_receipt_invoice(request, pk):
    """Serve the stored invoice photograph to authorised staff only."""
    receipt = get_object_or_404(GoodsReceipt, pk=pk)
    if not receipt.invoice_photo:
        raise Http404
    name = receipt.invoice_photo_name or Path(receipt.invoice_photo.name).name
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    receipt.invoice_photo.open("rb")
    response = FileResponse(receipt.invoice_photo, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(False, name)
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "goods_receipt.invoice_viewed", receipt, request=request)
    return response


@role_required(Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER)
def goods_receipt_check(request, pk):
    if request.method != "POST":
        raise Http404
    form = DeliveryCheckForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check notes could not be recorded.")
        return redirect("goods_receipt_detail", pk=pk)
    try:
        receipt = check_delivery(
            actor=request.user,
            receipt_id=pk,
            discrepancy_notes=form.cleaned_data["discrepancy_notes"],
            request=request,
        )
        messages.success(request, f"{receipt.receipt_number} independently checked.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("goods_receipt_detail", pk=pk)


@role_required(Role.PHARMACY, Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def stock_counts(request):
    counts = StockCount.objects.select_related(
        "counted_by__staff_profile", "reviewed_by__staff_profile"
    ).prefetch_related("lines__batch__item")
    return render(request, "hospital/stock_counts.html", {
        "counts": counts[:40],
        "open_form": StockCountOpenForm(),
        "can_open": user_role(request.user) in {Role.PHARMACY, Role.PROCUREMENT},
        "can_review": user_role(request.user) in {Role.REVIEWER, Role.OWNER},
    })


@role_required(Role.PHARMACY, Role.PROCUREMENT)
def stock_count_open(request):
    if request.method != "POST":
        raise Http404
    form = StockCountOpenForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a counting location before freezing a sheet.")
        return redirect("stock_counts")
    try:
        count = open_stock_count(
            actor=request.user,
            location=form.cleaned_data["location"],
            blind_count=form.cleaned_data["blind_count"],
            notes=form.cleaned_data["notes"],
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
        return redirect("stock_counts")
    messages.success(request, f"{count.reference} frozen at {timezone.localtime(count.cutoff_at):%H:%M}. Count the shelf and enter what you find.")
    return redirect("stock_count_detail", pk=count.pk)


@role_required(Role.PHARMACY, Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def stock_count_detail(request, pk):
    count = get_object_or_404(
        StockCount.objects.select_related("counted_by__staff_profile", "reviewed_by__staff_profile"), pk=pk
    )
    lines = count.lines.select_related("batch__item").order_by("batch__item__name", "batch__expiry_date")
    if request.method == "POST":
        counted = {}
        reasons = {}
        for line in lines:
            raw = request.POST.get(f"counted-{line.pk}", "").strip()
            if raw == "":
                messages.error(request, "Enter a counted quantity for every line, including the ones that are zero.")
                break
            try:
                counted[line.pk] = Decimal(raw)
            except (ArithmeticError, ValueError):
                messages.error(request, f"{line.batch.item.name} batch {line.batch.batch_number}: enter a number.")
                break
            reasons[line.pk] = request.POST.get(f"reason-{line.pk}", "").strip()
        else:
            try:
                submit_stock_count(actor=request.user, count_id=count.pk, counted=counted, reasons=reasons, request=request)
            except ValidationError as exc:
                messages.error(request, _validation_message(exc))
            else:
                messages.success(request, f"{count.reference} submitted. A delegated reviewer must approve before any adjustment is posted.")
                return redirect("stock_count_detail", pk=count.pk)

    role = user_role(request.user)
    return render(request, "hospital/stock_count_detail.html", {
        "count": count,
        "lines": lines,
        "review_form": StockCountReviewForm(),
        "can_enter": count.status == StockCount.Status.FROZEN and count.counted_by_id == request.user.id,
        "can_review": (
            count.status == StockCount.Status.SUBMITTED
            and role in {Role.REVIEWER, Role.OWNER}
            and count.counted_by_id != request.user.id
        ),
        "show_expected": not count.blind_count or count.status != StockCount.Status.FROZEN,
    })


@role_required(Role.REVIEWER, Role.OWNER)
def stock_count_review(request, pk):
    if request.method != "POST":
        raise Http404
    form = StockCountReviewForm(request.POST)
    notes = form.cleaned_data["review_notes"] if form.is_valid() else ""
    try:
        count = review_stock_count(
            actor=request.user,
            count_id=pk,
            approve=request.POST.get("decision") == "approve",
            review_notes=notes,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        if count.status == StockCount.Status.APPROVED:
            messages.success(request, f"{count.reference} approved. Adjustment movements posted for every variance.")
        else:
            messages.success(request, f"{count.reference} rejected. No stock balance was changed.")
    return redirect("stock_count_detail", pk=pk)


@role_required(Role.OWNER, Role.REVIEWER)
def reports(request):
    context = _report_context(request.GET.get("days", "7"))
    context.update({
        "unverified_payments": Payment.objects.filter(
            method=Payment.Method.MPESA,
            status=Payment.Status.VALID,
            verification_status=Payment.Verification.UNVERIFIED,
        ).select_related("received_by").order_by("-received_at")[:50],
        "pending_credits": CreditNote.objects.filter(
            status=CreditNote.Status.PENDING
        ).select_related("invoice", "requested_by").order_by("-created_at")[:50],
    })
    return render(request, "hospital/reports.html", context)


def _report_context(days_value):
    days = _period_days(days_value)
    start = timezone.now() - timedelta(days=days)
    invoices = Invoice.objects.filter(posted_at__gte=start).exclude(status=Invoice.Status.DRAFT)
    payments = Payment.objects.filter(received_at__gte=start, status=Payment.Status.VALID)
    return {
        "days": days,
        "net_billed": invoiced_total(invoices),
        "verified_collections": payments.filter(Q(method=Payment.Method.CASH) | Q(verification_status__in=[Payment.Verification.MANUAL, Payment.Verification.PROVIDER])).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
        "unverified_mpesa": payments.filter(method=Payment.Method.MPESA, verification_status=Payment.Verification.UNVERIFIED).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
        "receivables": outstanding_receivables(),
        "department_activity": Encounter.objects.filter(created_at__gte=start).values("department").annotate(total=Count("id")).order_by("-total"),
        "recent_invoices": invoices.select_related("patient").order_by("-posted_at")[:25],
        "stock": stock_position(),
        "stock_activity": stock_activity(days),
        "receiving": receiving_summary(days),
        "last_refresh": timezone.now(),
    }


@role_required(Role.OWNER, Role.REVIEWER)
def report_download_pdf(request):
    context = _report_context(request.GET.get("days", "7"))
    pdf = build_financial_report_pdf(
        context=context,
        hospital_name=settings.HOSPITAL_NAME,
        generated_by=str(request.user.staff_profile),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(
        True, f"KFBH-owner-report-{context['days']}-days.pdf"
    )
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "report.exported_pdf", request.user.staff_profile, after={"days": context["days"]}, request=request)
    return response


@role_required(Role.OWNER, Role.REVIEWER)
def payment_verify(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        payment = verify_mpesa(
            actor=request.user,
            payment_id=pk,
            provider_confirmed=request.POST.get("provider_confirmed") == "1",
            request=request,
        )
        ExceptionRecord.objects.filter(
            category="unverified_mpesa",
            summary__icontains=payment.reference,
        ).update(status=ExceptionRecord.Status.RESOLVED, resolved_at=timezone.now(), resolution="Payment verified through the reviewer workflow.")
        messages.success(request, f"M-PESA {payment.reference} verified independently.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("reports")


@role_required(Role.REVIEWER, Role.OWNER)
def credit_note_review(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        note = approve_credit_note(
            actor=request.user,
            credit_note_id=pk,
            approve=request.POST.get("decision") == "approve",
            request=request,
        )
        messages.success(request, f"Credit note {note.get_status_display().lower()}.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("reports")


@role_required(Role.REVIEWER, Role.OWNER)
def exceptions(request):
    records = ExceptionRecord.objects.select_related("assigned_to").order_by("status", "-severity", "-created_at")
    return render(request, "hospital/exceptions.html", {"records": records})


@role_required(Role.OWNER, Role.REVIEWER)
def audit_review(request):
    records = AuditEvent.objects.select_related("actor__staff_profile")
    query = request.GET.get("q", "").strip()
    action = request.GET.get("action", "").strip()
    role = request.GET.get("role", "").strip()
    if query:
        records = records.filter(
            Q(entity_id__icontains=query)
            | Q(reason__icontains=query)
            | Q(actor__username__icontains=query)
        )
    if action:
        records = records.filter(action__icontains=action)
    if role:
        records = records.filter(effective_role=role)
    page = Paginator(records, 75).get_page(request.GET.get("page"))
    return render(request, "hospital/audit_review.html", {
        "records": page,
        "page": page,
        "filter_query": _filter_query(q=query, action=action, role=role),
        "query": query,
        "action_filter": action,
        "role_filter": role,
        "role_choices": Role.choices,
    })


@role_required(Role.CLINICIAN, Role.NURSE, Role.LAB)
def departments(request):
    work = ServiceOrder.objects.select_related("encounter__patient", "service", "requested_by").order_by("status", "created_at")
    return render(request, "hospital/departments.html", {"work": work})


@role_required(Role.LAB, Role.CLINICIAN)
def service_order_update(request, pk):
    order = get_object_or_404(ServiceOrder.objects.select_related("encounter__patient", "service"), pk=pk)
    form = ServiceResultForm(request.POST or None, instance=order)
    if request.method == "POST" and form.is_valid():
        updated = form.save(commit=False)
        if updated.status == ServiceOrder.Status.RELEASED and order.requested_by_id == request.user.id:
            form.add_error("status", "The requester cannot release their own result. Send it for independent review.")
        else:
            updated.performer = request.user
            if updated.status == ServiceOrder.Status.RELEASED:
                updated.released_at = timezone.now()
            updated.save()
            audit(request.user, f"service_order.{updated.status}", updated, request=request)
            messages.success(request, "Department work item updated.")
            return redirect("departments")
    return render(request, "hospital/service_result_form.html", {"form": form, "order": order})


@role_required(Role.CLINICIAN, Role.NURSE, Role.OWNER)
def wards(request):
    wards_qs = Ward.objects.prefetch_related("beds__admissions__patient").filter(active=True)
    return render(request, "hospital/wards.html", {"wards": wards_qs, "admissions": Admission.objects.filter(discharged_at__isnull=True).select_related("patient", "bed__ward")})


@role_required(Role.CLINICIAN)
def admission_create(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    form = AdmissionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        admission = form.save(commit=False)
        admission.patient = encounter.patient
        admission.encounter = encounter
        admission.admitted_by = request.user
        admission.save()
        audit(request.user, "admission.created", admission, request=request)
        messages.success(request, f"{encounter.patient.full_name} admitted to {admission.bed.ward.name}, {admission.bed.label}.")
        return redirect("wards")
    return render(request, "hospital/admission_form.html", {"form": form, "encounter": encounter})


@role_required(Role.EYE, Role.CLINICIAN, Role.OWNER)
def eye_clinic(request):
    waiting = EyeCase.objects.select_related("patient", "session").order_by("status", "created_at")
    sessions = EyeSession.objects.annotate(patient_count=Count("cases"), eye_count=Sum(models_eye_count())).order_by("-session_date")
    payables = ClinicianPayable.objects.select_related("eye_case__patient")
    return render(request, "hospital/eye.html", {"waiting": waiting, "sessions": sessions, "payables": payables})


@role_required(Role.EYE, Role.CLINICIAN)
def eye_case_complete(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        case = complete_eye_case(actor=request.user, case_id=pk, request=request)
        messages.success(request, f"Case for {case.patient.full_name} completed and one clinician payable accrued.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("eye_clinic")


def models_eye_count():
    from django.db.models import Case, IntegerField, When
    return Case(When(cases__eye="both", then=2), default=1, output_field=IntegerField())


@role_required(Role.OWNER)
def settings_view(request):
    settings_rows = Setting.objects.all().order_by("production_confirmed", "key")
    return render(request, "hospital/settings.html", {"settings_rows": settings_rows})


@role_required(Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def purchasing(request):
    # The template prints both staff profiles per row; without them on the
    # select_related chain that is two extra queries for every purchase order.
    orders = PurchaseOrder.objects.select_related(
        "supplier", "requested_by__staff_profile", "approved_by__staff_profile"
    ).prefetch_related("lines__item", "receipts").order_by("-created_at")
    return render(request, "hospital/purchasing.html", {"orders": orders})


@role_required(Role.PROCUREMENT)
def purchase_order_create(request):
    form = PurchaseOrderForm(request.POST or None)
    lines = PurchaseOrderLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        line_data = [
            row.cleaned_data for row in lines
            if row.cleaned_data and not row.cleaned_data.get("DELETE")
        ]
        with transaction.atomic():
            order = form.save(commit=False)
            order.requested_by = request.user
            order.save()
            for line in line_data:
                PurchaseOrderLine.objects.create(
                    order=order,
                    item=line["product"],
                    quantity_base_units=line["quantity_base_units"],
                    quoted_unit_cost=line["quoted_unit_cost"],
                )
            audit(request.user, "purchase_order.requested", order, request=request)
        messages.success(request, f"Purchase request {order.order_number} with {len(line_data)} line(s) submitted for independent review.")
        return redirect("purchasing")
    return render(request, "hospital/purchase_order_form.html", {"form": form, "formset": lines})


@role_required(Role.REVIEWER, Role.OWNER)
def purchase_order_approve(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        order = approve_purchase_order(actor=request.user, order_id=pk, request=request)
        messages.success(request, f"{order.order_number} approved independently.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("purchasing")


def _validate_product_csv(uploaded):
    raw = uploaded.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV must be UTF-8 encoded.") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"code", "name", "department", "base_unit", "sale_unit", "units_per_sale_unit", "sale_price", "reorder_level", "prescription_required"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValidationError(f"CSV must contain: {', '.join(sorted(required))}.")
    rows, errors = [], []
    for index, row in enumerate(reader, start=2):
        if index > 5001:
            errors.append({"row": index, "error": "Maximum 5,000 rows per import."})
            break
        try:
            parsed = {
                "code": row["code"].strip(), "name": row["name"].strip(), "department": row["department"].strip(),
                "base_unit": row["base_unit"].strip(), "sale_unit": row["sale_unit"].strip(),
                "units_per_sale_unit": str(Decimal(row["units_per_sale_unit"])), "sale_price": str(Decimal(row["sale_price"])),
                "reorder_level": str(Decimal(row["reorder_level"])),
                "prescription_required": row["prescription_required"].strip().lower() in {"1", "true", "yes", "y"},
            }
            if not all(parsed[k] for k in ["code", "name", "department", "base_unit", "sale_unit"]):
                raise ValueError("Required text field is blank")
            if Decimal(parsed["units_per_sale_unit"]) <= 0 or Decimal(parsed["sale_price"]) <= 0:
                raise ValueError("Conversion and sale price must be greater than zero")
            if CatalogueItem.objects.filter(code=parsed["code"]).exists():
                raise ValueError("Code already exists; imports never overwrite prices silently")
            rows.append(parsed)
        except Exception as exc:
            errors.append({"row": index, "code": row.get("code", ""), "error": str(exc)})
    return digest, rows, errors


@role_required(Role.OWNER, Role.PROCUREMENT)
def csv_import(request):
    form = CsvImportForm(request.POST or None, request.FILES or None)
    preview_job = None
    if request.method == "POST" and request.POST.get("commit_job"):
        job = get_object_or_404(ImportJob, pk=request.POST["commit_job"], created_by=request.user)
        if job.status == "committed" or ImportJob.objects.filter(idempotency_key=job.idempotency_key, status="committed").exclude(pk=job.pk).exists():
            messages.info(request, "This import was already committed; no duplicate records were created.")
            return redirect("csv_import")
        if not job.dry_run or job.status != "validated" or job.error_count:
            messages.error(request, "Only a successful dry run can be committed.")
            return redirect("csv_import")
        with transaction.atomic():
            for row in job.report.get("rows", []):
                item = CatalogueItem.objects.create(
                    code=row["code"], name=row["name"], department=row["department"], kind=CatalogueItem.Kind.PRODUCT,
                    base_unit=row["base_unit"], sale_unit=row["sale_unit"], units_per_sale_unit=Decimal(row["units_per_sale_unit"]),
                    reorder_level=Decimal(row["reorder_level"]), prescription_required=row["prescription_required"],
                )
                PriceVersion.objects.create(item=item, amount=Decimal(row["sale_price"]), reason=f"Import {job.filename}", approved_by=request.user)
            job.status = "committed"
            job.dry_run = False
            job.save(update_fields=["status", "dry_run", "updated_at"])
            audit(request.user, "import.committed", job, after={"rows": len(job.report.get("rows", []))}, request=request)
        messages.success(request, f"Imported {job.row_count} product rows. Opening stock still requires a witnessed count.")
        return redirect("csv_import")
    if request.method == "POST" and form.is_valid():
        try:
            digest, rows, errors = _validate_product_csv(form.cleaned_data["csv_file"])
            preview_job, created = ImportJob.objects.get_or_create(
                idempotency_key=digest,
                defaults={"kind": form.cleaned_data["import_kind"], "filename": form.cleaned_data["csv_file"].name, "status": "validated" if not errors else "failed", "dry_run": True, "row_count": len(rows), "error_count": len(errors), "report": {"rows": rows, "errors": errors}, "created_by": request.user},
            )
            if not created:
                messages.info(request, "This exact file was already uploaded; showing its existing report.")
            audit(request.user, "import.dry_run", preview_job, after={"rows": len(rows), "errors": len(errors)}, request=request)
        except ValidationError as exc:
            form.add_error("csv_file", _validation_message(exc))
    recent = ImportJob.objects.filter(created_by=request.user).order_by("-created_at")[:10]
    return render(request, "hospital/csv_import.html", {"form": form, "preview_job": preview_job, "recent": recent})


@login_required
def screen_lock(request):
    profile = request.user.staff_profile
    profile.locked_at = timezone.now()
    profile.save(update_fields=["locked_at"])
    return redirect("screen_unlock")


@login_required
def screen_unlock(request):
    if request.method == "POST":
        user = authenticate(request, username=request.user.username, password=request.POST.get("password", ""))
        if user:
            profile = request.user.staff_profile
            profile.locked_at = None
            profile.save(update_fields=["locked_at"])
            login(request, user)
            return redirect("dashboard")
        messages.error(request, "Password not accepted.")
    return render(request, "hospital/unlock.html")


@login_required
def downtime_forms(request):
    return render(request, "hospital/downtime_forms.html")


class ThrottledLoginView(LoginView):
    """Sign-in with a per-username lockout.

    Django authenticates in constant work per attempt and never rate-limits, so
    an unauthenticated workstation on the ward LAN can try passwords for as long
    as it likes. Failures are counted in the database so a service restart does
    not hand an attacker a clean slate.
    """

    template_name = "registration/login.html"

    def post(self, request, *args, **kwargs):
        username = (request.POST.get("username") or "").strip()
        if LoginAttempt.is_locked(username):
            context = self.get_context_data(form=self.get_form())
            context["lockout_minutes"] = LoginAttempt.LOCKOUT_WINDOW_MINUTES
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)


@role_required(Role.PHARMACY, Role.NURSE, Role.CLINICIAN, Role.PROCUREMENT, Role.OWNER, Role.REVIEWER)
def custody(request):
    """Stock that left the pharmacy and has not yet been accounted for."""
    position = departmental_custody()
    issues = DepartmentIssue.objects.select_related("patient", "issued_by__staff_profile").prefetch_related(
        "lines__batch__item"
    )
    role = user_role(request.user)
    return render(request, "hospital/custody.html", {
        "position": position,
        "outstanding": issues.filter(status=DepartmentIssue.Status.OUTSTANDING)[:40],
        "settled": issues.filter(status=DepartmentIssue.Status.SETTLED)[:15],
        "can_issue": role == Role.PHARMACY,
        "can_account": role in {Role.NURSE, Role.CLINICIAN, Role.PHARMACY},
    })


@role_required(Role.PHARMACY)
def custody_issue(request):
    form = DepartmentIssueForm(request.POST or None)
    lines = DepartmentIssueLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        patient = None
        number = form.cleaned_data.get("patient_number", "").strip()
        if number:
            patient = Patient.objects.filter(patient_number__iexact=number).first()
            if not patient:
                form.add_error("patient_number", "No patient carries that number.")
        if not form.errors:
            payload = [
                {"batch": row.cleaned_data["batch"], "quantity": row.cleaned_data["quantity"]}
                for row in lines
                if row.cleaned_data and not row.cleaned_data.get("DELETE")
            ]
            try:
                issue = issue_to_department(
                    actor=request.user,
                    department=form.cleaned_data["department"],
                    received_by_name=form.cleaned_data["received_by_name"],
                    kind=form.cleaned_data["kind"],
                    patient=patient,
                    notes=form.cleaned_data["notes"],
                    lines=payload,
                    request=request,
                )
            except ValidationError as exc:
                messages.error(request, _validation_message(exc))
            else:
                messages.success(
                    request,
                    f"{issue.reference} issued to {issue.department}. It stays outstanding until {issue.received_by_name} accounts for it.",
                )
                return redirect("custody")
    return render(request, "hospital/custody_issue_form.html", {"form": form, "formset": lines})


@role_required(Role.NURSE, Role.CLINICIAN, Role.PHARMACY)
def custody_account(request, pk):
    issue = get_object_or_404(
        DepartmentIssue.objects.select_related("patient").prefetch_related("lines__batch__item"), pk=pk
    )
    if request.method == "POST":
        outcomes = {}
        for line in issue.lines.all():
            def amount(prefix):
                raw = request.POST.get(f"{prefix}-{line.pk}", "").strip()
                return Decimal(raw) if raw else Decimal("0")
            try:
                outcomes[line.pk] = {
                    "consumed": amount("consumed"),
                    "returned": amount("returned"),
                    "wasted": amount("wasted"),
                }
            except (ArithmeticError, ValueError):
                messages.error(request, f"{line.batch.item.name}: enter numbers only.")
                outcomes = None
                break
        if outcomes is not None:
            try:
                issue = account_for_issue(actor=request.user, issue_id=issue.pk, outcomes=outcomes, request=request)
            except ValidationError as exc:
                messages.error(request, _validation_message(exc))
            else:
                if issue.status == DepartmentIssue.Status.SETTLED:
                    messages.success(request, f"{issue.reference} is fully accounted for.")
                else:
                    messages.success(
                        request,
                        f"{issue.reference} updated. {issue.outstanding_quantity} units remain in departmental custody.",
                    )
                return redirect("custody")
    return render(request, "hospital/custody_account_form.html", {"issue": issue})


@role_required(Role.PHARMACY, Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def write_offs(request):
    records = StockWriteOff.objects.select_related(
        "batch__item", "requested_by__staff_profile", "reviewed_by__staff_profile"
    )
    role = user_role(request.user)
    return render(request, "hospital/write_offs.html", {
        "pending": records.filter(status=StockWriteOff.Status.PENDING),
        "decided": records.exclude(status=StockWriteOff.Status.PENDING)[:25],
        "form": WriteOffRequestForm() if role in {Role.PHARMACY, Role.PROCUREMENT} else None,
        "review_form": WriteOffReviewForm(),
        "can_request": role in {Role.PHARMACY, Role.PROCUREMENT},
        "can_review": role in {Role.REVIEWER, Role.OWNER},
        "expired": stock_position()["expired"],
    })


@role_required(Role.PHARMACY, Role.PROCUREMENT)
def write_off_request(request):
    if request.method != "POST":
        raise Http404
    form = WriteOffRequestForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(error for errors in form.errors.values() for error in errors))
        return redirect("write_offs")
    try:
        write_off = request_write_off(
            actor=request.user,
            batch_id=form.cleaned_data["batch"].pk,
            quantity=form.cleaned_data["quantity"],
            reason=form.cleaned_data["reason"],
            narrative=form.cleaned_data["narrative"],
            request=request,
        )
        messages.success(
            request,
            f"{write_off.reference} proposed for KES {write_off.value_at_cost:,.2f}. Stock stays on the balance until a reviewer approves it.",
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("write_offs")


@role_required(Role.REVIEWER, Role.OWNER)
def write_off_review(request, pk):
    if request.method != "POST":
        raise Http404
    form = WriteOffReviewForm(request.POST)
    notes = form.cleaned_data["review_notes"] if form.is_valid() else ""
    try:
        write_off = review_write_off(
            actor=request.user,
            write_off_id=pk,
            approve=request.POST.get("decision") == "approve",
            review_notes=notes,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    else:
        if write_off.status == StockWriteOff.Status.APPROVED:
            messages.success(request, f"{write_off.reference} approved; KES {write_off.value_at_cost:,.2f} removed from stock.")
        else:
            messages.success(request, f"{write_off.reference} rejected. No balance changed.")
    return redirect("write_offs")


@role_required(Role.REVIEWER, Role.OWNER)
def batch_disposition(request, pk):
    if request.method != "POST":
        raise Http404
    form = BatchDispositionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Record a disposition and the reason for it.")
        return redirect("stock")
    try:
        batch = set_batch_disposition(
            actor=request.user, batch_id=pk,
            status=form.cleaned_data["status"], reason=form.cleaned_data["reason"], request=request,
        )
        messages.success(request, f"{batch.item.name} batch {batch.batch_number} is now {batch.get_status_display().lower()}.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("stock")


@role_required(Role.OWNER, Role.REVIEWER, Role.PROCUREMENT)
def stock_intelligence(request):
    """Shrinkage, supplier price drift and whether the controls are being used.

    Three questions the ledger can answer but no screen was asking: how much is
    going missing, whether suppliers are walking their prices up, and whether
    the controls are followed or quietly skipped.
    """
    days = _period_days(request.GET.get("days", "90"), default=90)
    return render(request, "hospital/stock_intelligence.html", {
        "days": days,
        "shrinkage": shrinkage(days),
        "prices": supplier_price_history(days),
        "adoption": control_adoption(min(days, 90)),
        "custody": departmental_custody(),
    })


def owner_brief_context(days=7):
    """What needs the owner's attention, assembled in one place.

    The specification forbids automatic external messaging, so this is a
    standing in-app brief rather than a notification. Every line links to the
    records behind it.
    """
    position = stock_position()
    adoption = control_adoption(30)
    custody = departmental_custody()
    loss = shrinkage(90)

    items = []
    if position["expired_count"]:
        items.append({
            "severity": "urgent",
            "headline": f"{position['expired_count']} expired batch{'es' if position['expired_count'] != 1 else ''} still on the shelf",
            "detail": f"KES {position['expired_value']:,.2f} at cost. Expired stock cannot be sold and stays in the ledger until it is written off.",
            "url": reverse("write_offs"),
            "action": "Write it off",
        })
    if adoption["write_offs_pending"]:
        items.append({
            "severity": "warning",
            "headline": f"{adoption['write_offs_pending']} write-off{'s' if adoption['write_offs_pending'] != 1 else ''} awaiting your approval",
            "detail": "Stock stays on the balance until somebody with authority removes it.",
            "url": reverse("write_offs"),
            "action": "Review",
        })
    unchecked = receiving_summary(30)["unchecked_count"]
    if unchecked:
        items.append({
            "severity": "warning",
            "headline": f"{unchecked} deliver{'ies' if unchecked != 1 else 'y'} never independently checked",
            "detail": "A delivery confirmed only by the person who received it has had no second pair of eyes.",
            "url": reverse("deliveries"),
            "action": "Open deliveries",
        })
    if custody["stale_count"]:
        items.append({
            "severity": "warning",
            "headline": f"{custody['stale_count']} ward issue{'s' if custody['stale_count'] != 1 else ''} unaccounted for over a week",
            "detail": f"KES {custody['stale_value']:,.2f} of stock left the pharmacy and nobody has said what happened to it.",
            "url": reverse("custody"),
            "action": "Chase it",
        })
    if position["below_reorder_count"]:
        items.append({
            "severity": "info",
            "headline": f"{position['below_reorder_count']} product{'s' if position['below_reorder_count'] != 1 else ''} at or below reorder level",
            "detail": "Running out stops sales as surely as losing stock does.",
            "url": f"{reverse('stock')}?view=low",
            "action": "See what to order",
        })
    if position["expiring_soon_count"]:
        items.append({
            "severity": "info",
            "headline": f"{position['expiring_soon_count']} batch{'es' if position['expiring_soon_count'] != 1 else ''} expiring within {position['expiry_window_days']} days",
            "detail": "Use or review these first.",
            "url": f"{reverse('stock')}?view=expiring",
            "action": "See them",
        })
    if loss["measured"] and loss["loss_value"] > 0:
        percent = f" — {loss['as_percent_of_cogs']}% of cost of goods" if loss["as_percent_of_cogs"] is not None else ""
        items.append({
            "severity": "urgent",
            "headline": f"KES {loss['loss_value']:,.2f} of unexplained stock loss in 90 days{percent}",
            "detail": "Counted short against the ledger, with no write-off explaining it.",
            "url": reverse("stock_intelligence"),
            "action": "Investigate",
        })
    if not adoption["counts_approved"]:
        items.append({
            "severity": "warning",
            "headline": "No stock count has been approved in the last 30 days",
            "detail": "Without a count, stock loss cannot be measured at all — only guessed at.",
            "url": reverse("stock_counts"),
            "action": "Start a count",
        })

    order = {"urgent": 0, "warning": 1, "info": 2}
    items.sort(key=lambda item: order[item["severity"]])
    return {
        "brief_items": items,
        "brief_urgent": sum(1 for item in items if item["severity"] == "urgent"),
        "brief_adoption": adoption,
        "brief_generated_at": timezone.now(),
    }


@role_required(Role.OWNER, Role.REVIEWER)
def owner_brief(request):
    context = owner_brief_context()
    context.update({"shrinkage": shrinkage(90), "custody": departmental_custody()})
    return render(request, "hospital/owner_brief.html", context)
