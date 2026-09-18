import csv
import hashlib
import io
import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import (
    AdmissionForm,
    ClinicalNoteForm,
    CsvImportForm,
    EncounterForm,
    PatientForm,
    PaymentForm,
    PharmacyBasketForm,
    PrescriptionForm,
    PurchaseOrderForm,
    ServiceOrderForm,
    ServiceResultForm,
    ShiftCloseForm,
    ShiftOpenForm,
)
from .models import (
    Admission,
    AuditEvent,
    CashShift,
    CatalogueItem,
    ClinicalNote,
    ClinicianPayable,
    Encounter,
    ExceptionRecord,
    EyeCase,
    EyeSession,
    ImportJob,
    Invoice,
    Patient,
    Payment,
    PharmacyOrder,
    Prescription,
    PrescriptionItem,
    PriceVersion,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    ServiceOrder,
    Setting,
    StockBatch,
    StockMovement,
    Ward,
)
from .permissions import role_required, user_role
from .services import approve_purchase_order, audit, deterministic_key, dispense_order, prepare_pharmacy_order, record_payment


def _validation_message(exc):
    if hasattr(exc, "messages"):
        return " ".join(exc.messages)
    return str(exc)


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
        "low_stock": [b for b in StockBatch.objects.select_related("item").all() if b.quantity_on_hand <= b.item.reorder_level][:8],
        "exceptions": ExceptionRecord.objects.exclude(status=ExceptionRecord.Status.RESOLVED).order_by("-created_at")[:6],
        "queue": Encounter.objects.exclude(status=Encounter.Status.CLOSED).select_related("patient").order_by("created_at")[:8],
        "my_shift": CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN).first(),
    }
    if role == Role.OWNER:
        valid_payments = Payment.objects.filter(status=Payment.Status.VALID, received_at__gte=start)
        posted = Invoice.objects.filter(posted_at__gte=start).exclude(status=Invoice.Status.DRAFT)
        context.update({
            "net_billed": sum((invoice.total for invoice in posted), Decimal("0.00")),
            "verified_collections": valid_payments.filter(Q(method=Payment.Method.CASH) | Q(verification_status__in=[Payment.Verification.MANUAL, Payment.Verification.PROVIDER])).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
            "unverified_mpesa": valid_payments.filter(method=Payment.Method.MPESA, verification_status=Payment.Verification.UNVERIFIED).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
            "receivables": sum((invoice.balance for invoice in Invoice.objects.exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.PAID])), Decimal("0.00")),
            "occupied_beds": Admission.objects.filter(discharged_at__isnull=True).count(),
            "active_beds": sum(w.beds.filter(active=True).count() for w in Ward.objects.filter(active=True)),
            "eye_waiting": EyeCase.objects.filter(status="waiting").count(),
        })
    return render(request, "hospital/dashboard.html", context)


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.OWNER, Role.EYE)
def patient_list(request):
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if query:
        patients = patients.filter(Q(patient_number__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query) | Q(phone__icontains=query))
    return render(request, "hospital/patient_list.html", {"patients": patients[:100], "query": query})


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
    return render(request, "hospital/patient_detail.html", {"patient": patient, "invoices": invoices, "notes": notes, "encounters": patient.encounters.all()})


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


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.LAB)
def queue(request):
    encounters = Encounter.objects.exclude(status=Encounter.Status.CLOSED).select_related("patient", "assigned_clinician").order_by("urgency", "created_at")
    return render(request, "hospital/queue.html", {"encounters": encounters})


@role_required(Role.CLINICIAN)
def clinical_note(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    draft = ClinicalNote.objects.filter(encounter=encounter, author=request.user, status=ClinicalNote.Status.DRAFT).first()
    form = ClinicalNoteForm(request.POST or None, instance=draft)
    if request.method == "POST" and form.is_valid():
        note = form.save(commit=False)
        if not note.pk:
            note.encounter = encounter
            note.author = request.user
            note.version = (ClinicalNote.objects.filter(encounter=encounter, author=request.user).aggregate(v=models_max_version())["v"] or 0) + 1
        note.save()
        if request.POST.get("action") == "sign":
            note.sign()
            encounter.status = Encounter.Status.PHARMACY
            encounter.save(update_fields=["status", "updated_at"])
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
    form = PrescriptionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            prescription = Prescription.objects.create(encounter=encounter, prescriber=request.user, signed_at=timezone.now())
            PrescriptionItem.objects.create(
                prescription=prescription,
                product=form.cleaned_data["product"], strength=form.cleaned_data["strength"],
                dose=form.cleaned_data["dose"], route=form.cleaned_data["route"], frequency=form.cleaned_data["frequency"],
                duration=form.cleaned_data["duration"], quantity_base_units=form.cleaned_data["quantity_base_units"],
                instructions=form.cleaned_data["instructions"],
            )
            audit(request.user, "prescription.signed", prescription, request=request)
        messages.success(request, "Prescription signed and sent to pharmacy for pricing.")
        return redirect("patient_detail", pk=encounter.patient_id)
    return render(request, "hospital/prescription_form.html", {"form": form, "encounter": encounter})


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


def models_max_version():
    from django.db.models import Max
    return Max("version")


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


@role_required(Role.PHARMACY, Role.PROCUREMENT, Role.OWNER)
def stock_view(request):
    batches = StockBatch.objects.select_related("item").order_by("item__name", "expiry_date")
    movements = StockMovement.objects.select_related("batch__item", "entered_by")[:50]
    return render(request, "hospital/stock.html", {"batches": batches, "movements": movements})


@role_required(Role.OWNER, Role.REVIEWER)
def reports(request):
    days = int(request.GET.get("days", "7")) if request.GET.get("days", "7").isdigit() else 7
    start = timezone.now() - timedelta(days=min(days, 365))
    invoices = Invoice.objects.filter(posted_at__gte=start).exclude(status=Invoice.Status.DRAFT)
    payments = Payment.objects.filter(received_at__gte=start, status=Payment.Status.VALID)
    context = {
        "days": days,
        "net_billed": sum((invoice.total for invoice in invoices), Decimal("0.00")),
        "verified_collections": payments.filter(Q(method=Payment.Method.CASH) | Q(verification_status__in=[Payment.Verification.MANUAL, Payment.Verification.PROVIDER])).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
        "unverified_mpesa": payments.filter(method=Payment.Method.MPESA, verification_status=Payment.Verification.UNVERIFIED).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
        "receivables": sum((invoice.balance for invoice in Invoice.objects.exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.PAID])), Decimal("0.00")),
        "department_activity": Encounter.objects.filter(created_at__gte=start).values("department").annotate(total=Count("id")).order_by("-total"),
        "recent_invoices": invoices.select_related("patient").order_by("-posted_at")[:25],
        "last_refresh": timezone.now(),
    }
    return render(request, "hospital/reports.html", context)


@role_required(Role.REVIEWER, Role.OWNER)
def exceptions(request):
    records = ExceptionRecord.objects.select_related("assigned_to").order_by("status", "-severity", "-created_at")
    return render(request, "hospital/exceptions.html", {"records": records})


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


def models_eye_count():
    from django.db.models import Case, IntegerField, When
    return Case(When(cases__eye="both", then=2), default=1, output_field=IntegerField())


@role_required(Role.OWNER)
def settings_view(request):
    settings_rows = Setting.objects.all().order_by("production_confirmed", "key")
    return render(request, "hospital/settings.html", {"settings_rows": settings_rows})


@role_required(Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def purchasing(request):
    orders = PurchaseOrder.objects.select_related("supplier", "requested_by", "approved_by").prefetch_related("lines").order_by("-created_at")
    return render(request, "hospital/purchasing.html", {"orders": orders})


@role_required(Role.PROCUREMENT)
def purchase_order_create(request):
    form = PurchaseOrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            order = form.save(commit=False)
            order.requested_by = request.user
            order.save()
            PurchaseOrderLine.objects.create(
                order=order,
                item=form.cleaned_data["product"],
                quantity_base_units=form.cleaned_data["quantity_base_units"],
                quoted_unit_cost=form.cleaned_data["quoted_unit_cost"],
            )
            audit(request.user, "purchase_order.requested", order, request=request)
        messages.success(request, f"Purchase request {order.order_number} submitted for independent review.")
        return redirect("purchasing")
    return render(request, "hospital/purchase_order_form.html", {"form": form})


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
