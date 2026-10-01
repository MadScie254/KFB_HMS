from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Prefetch, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header, urlencode

from . import billing_views as _billing_views
from . import clinical_views as _clinical_views
from . import custody_views as _custody_views
from . import import_views as _import_views
from . import patient_views as _patient_views
from . import pharmacy_views as _pharmacy_views
from . import purchasing_views as _purchasing_views
from . import stock_views as _stock_views
from .analytics import (
    control_adoption,
    departmental_custody,
    receiving_summary,
    shrinkage,
    stock_activity,
    stock_position,
    supplier_price_history,
    with_invoice_financials,
)
from .forms import (
    AdmissionForm,
    ServiceResultForm,
)
from .models import (
    Admission,
    AuditEvent,
    Bed,
    CashShift,
    ClinicianPayable,
    CreditNote,
    Encounter,
    ExceptionRecord,
    EyeCase,
    EyeSession,
    GoodsReceipt,
    Invoice,
    InvoiceLine,
    LoginAttempt,
    Patient,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    Refund,
    Role,
    ServiceOrder,
    Setting,
    StockBatch,
    Ward,
)
from .pdf_reports import build_financial_report_pdf
from .permissions import has_capability, role_required, user_role
from .services import (
    audit,
    complete_eye_case,
    discharge_admission,
    update_service_order,
)
from .view_helpers import _filter_query, _period_days, _validation_message, _worklist_page

# Keep URL callbacks and the established imports available from hospital.views.
credit_note_create = _billing_views.credit_note_create
invoice_payment = _billing_views.invoice_payment
receipt = _billing_views.receipt
refund_request = _billing_views.refund_request
refunds = _billing_views.refunds
refund_action = _billing_views.refund_action
shift_manage = _billing_views.shift_manage
shift_review = _billing_views.shift_review
payment_verify = _billing_views.payment_verify
credit_note_review = _billing_views.credit_note_review

IMPORT_MAX_ROWS = _import_views.IMPORT_MAX_ROWS
IMPORT_PREVIEW_MAX_AGE = _import_views.IMPORT_PREVIEW_MAX_AGE
IMPORT_DISPLAY_ERRORS = _import_views.IMPORT_DISPLAY_ERRORS
_read_csv = _import_views._read_csv
_finish_csv_validation = _import_views._finish_csv_validation
_validate_product_csv = _import_views._validate_product_csv
_validate_patient_csv = _import_views._validate_patient_csv
_validate_opening_stock_csv = _import_views._validate_opening_stock_csv
_validate_opening_receivables_csv = _import_views._validate_opening_receivables_csv
_commit_import_job = _import_views._commit_import_job
_check_import_job_current = _import_views._check_import_job_current
csv_import = _import_views.csv_import

patient_list = _patient_views.patient_list
patient_create = _patient_views.patient_create
patient_detail = _patient_views.patient_detail
patient_attachment_upload = _patient_views.patient_attachment_upload
patient_attachment_download = _patient_views.patient_attachment_download
patient_access_pdf = _patient_views.patient_access_pdf

encounter_create = _clinical_views.encounter_create
TRIAGE_RANK = _clinical_views.TRIAGE_RANK
open_clinical_encounters = _clinical_views.open_clinical_encounters
queue = _clinical_views.queue
encounter_close = _clinical_views.encounter_close
clinical_note = _clinical_views.clinical_note
prescription_create = _clinical_views.prescription_create
service_order_create = _clinical_views.service_order_create

pharmacy_orders = _pharmacy_views.pharmacy_orders
pharmacy_prepare_prescription = _pharmacy_views.pharmacy_prepare_prescription
pharmacy_order_create = _pharmacy_views.pharmacy_order_create
pharmacy_order_detail = _pharmacy_views.pharmacy_order_detail
pharmacy_dispense = _pharmacy_views.pharmacy_dispense

STOCK_VIEWS = _stock_views.STOCK_VIEWS
stock_view = _stock_views.stock_view
deliveries = _stock_views.deliveries
goods_receipt_create = _stock_views.goods_receipt_create
goods_receipt_detail = _stock_views.goods_receipt_detail
goods_receipt_invoice = _stock_views.goods_receipt_invoice
goods_receipt_check = _stock_views.goods_receipt_check
stock_counts = _stock_views.stock_counts
stock_count_open = _stock_views.stock_count_open
stock_count_detail = _stock_views.stock_count_detail
stock_count_review = _stock_views.stock_count_review

supplier_changes = _purchasing_views.supplier_changes
supplier_change_request = _purchasing_views.supplier_change_request
supplier_change_review = _purchasing_views.supplier_change_review
purchasing = _purchasing_views.purchasing
purchase_order_create = _purchasing_views.purchase_order_create
purchase_order_approve = _purchasing_views.purchase_order_approve

custody = _custody_views.custody
custody_issue = _custody_views.custody_issue
custody_account = _custody_views.custody_account
write_offs = _custody_views.write_offs
write_off_request = _custody_views.write_off_request
write_off_review = _custody_views.write_off_review
batch_disposition = _custody_views.batch_disposition


def invoiced_total(invoices):
    """Billed value of an invoice queryset in one query."""
    return InvoiceLine.objects.filter(invoice__in=invoices).aggregate(v=Sum("line_total"))["v"] or Decimal("0.00")


def net_billed_since(start):
    """Charges posted less credits approved during the same period."""
    charges = InvoiceLine.objects.filter(invoice__posted_at__gte=start).exclude(
        invoice__status=Invoice.Status.DRAFT,
    ).aggregate(v=Sum("line_total"))["v"] or Decimal("0.00")
    credits = CreditNote.objects.filter(
        status=CreditNote.Status.APPROVED,
        reviewed_at__gte=start,
        invoice__posted_at__isnull=False,
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    return charges - credits


def outstanding_receivables():
    """Posted-but-unsettled value after credits, settled payments, and refunds.

    Four aggregates regardless of ledger size, in place of two queries per
    open invoice.
    """
    open_invoices = Invoice.objects.exclude(status=Invoice.Status.DRAFT)
    billed = invoiced_total(open_invoices)
    credited = CreditNote.objects.filter(
        invoice__in=open_invoices, status=CreditNote.Status.APPROVED
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    paid = PaymentAllocation.objects.filter(
        invoice__in=open_invoices, payment__status=Payment.Status.VALID
    ).filter(
        Q(payment__method=Payment.Method.CASH)
        | Q(payment__verification_status__in=[Payment.Verification.MANUAL, Payment.Verification.PROVIDER])
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    refunded = Refund.objects.filter(
        invoice__in=open_invoices, status=Refund.Status.PAID
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    return billed - credited - paid + refunded


def verified_collections_since(start):
    """Net received cash and independently verified M-PESA after paid refunds."""
    received = Payment.objects.filter(status=Payment.Status.VALID).filter(
        Q(method=Payment.Method.CASH, received_at__gte=start)
        | Q(
            method=Payment.Method.MPESA,
            verification_status__in=[Payment.Verification.MANUAL, Payment.Verification.PROVIDER],
            reviewed_at__gte=start,
        )
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    refunded = Refund.objects.filter(status=Refund.Status.PAID, paid_at__gte=start).aggregate(
        v=Sum("amount")
    )["v"] or Decimal("0.00")
    return received - refunded


# What each role is allowed to find. Search must never become a way around the
# page permissions: a receptionist searching a batch number finds nothing,
# because they cannot open the stock ledger either.
SEARCH_SCOPES = {
    "patients": {Role.OWNER, Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.EYE},
    "encounters": {Role.OWNER, Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.LAB},
    "stock": {Role.OWNER, Role.PHARMACY, Role.PROCUREMENT, Role.REVIEWER},
    "orders": {Role.OWNER, Role.PHARMACY, Role.RECEPTION},
    "deliveries": {Role.OWNER, Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER},
}


@login_required
def quick_search(request):
    """Everything the signed-in person may reach, from one box.

    Finding a patient used to mean opening Patients and searching there;
    finding a batch meant knowing which stock filter hid it. This answers the
    question directly, and only ever returns records the caller's role could
    already open.
    """
    term = (request.GET.get("q") or "").strip()
    role = user_role(request.user)
    results = []
    if len(term) < 2:
        return JsonResponse({"query": term, "results": results})

    def allowed(scope):
        return role in SEARCH_SCOPES.get(scope, set())

    if allowed("patients"):
        for patient in Patient.objects.filter(
            Q(first_name__icontains=term) | Q(last_name__icontains=term)
            | Q(patient_number__icontains=term) | Q(phone__icontains=term)
            | Q(id_number__icontains=term)
        ).order_by("last_name")[:5]:
            results.append({
                "kind": "Patient", "icon": "users", "label": patient.full_name,
                "detail": f"{patient.patient_number}" + (f" · {patient.phone}" if patient.phone else ""),
                "url": reverse("patient_detail", args=[patient.pk]),
            })

    if allowed("encounters"):
        for encounter in Encounter.objects.filter(
            encounter_number__icontains=term
        ).select_related("patient").order_by("-created_at")[:4]:
            results.append({
                "kind": "Visit", "icon": "list", "label": encounter.encounter_number,
                "detail": f"{encounter.patient.full_name} · {encounter.get_status_display()}",
                "url": reverse("patient_detail", args=[encounter.patient_id]),
            })

    if allowed("stock"):
        for batch in StockBatch.objects.filter(
            Q(batch_number__icontains=term) | Q(item__name__icontains=term) | Q(item__code__icontains=term)
        ).select_related("item").order_by("item__name")[:5]:
            results.append({
                "kind": "Batch", "icon": "box", "label": f"{batch.item.name}",
                "detail": f"Batch {batch.batch_number}" + (f" · expires {batch.expiry_date:%b %Y}" if batch.expiry_date else ""),
                "url": f"{reverse('stock')}?{urlencode({'q': batch.batch_number})}",
            })

    if allowed("orders"):
        for order in PharmacyOrder.objects.filter(
            order_number__icontains=term
        ).select_related("patient").order_by("-created_at")[:4]:
            results.append({
                "kind": "Pharmacy order", "icon": "pill", "label": order.order_number,
                "detail": order.customer_name or (order.patient.full_name if order.patient_id else ""),
                "url": reverse("pharmacy_order_detail", args=[order.pk]),
            })

    if allowed("deliveries"):
        for receipt in GoodsReceipt.objects.filter(
            Q(receipt_number__icontains=term) | Q(supplier_invoice_reference__icontains=term)
        ).select_related("purchase_order__supplier").order_by("-delivered_at")[:4]:
            results.append({
                "kind": "Delivery", "icon": "truck", "label": receipt.receipt_number,
                "detail": f"{receipt.purchase_order.supplier.name} · invoice {receipt.supplier_invoice_reference}",
                "url": reverse("goods_receipt_detail", args=[receipt.pk]),
            })

    return JsonResponse({"query": term, "results": results[:16]})


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


def session_status(request):
    """Authenticated server check; only an explicit POST extends the session."""
    if request.method not in {"GET", "POST"}:
        return JsonResponse({"error": "Method not allowed"}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False}, status=401)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    if request.method == "POST":
        request.session["_kfb_expires_at"] = (
            timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE)
        ).isoformat()
    return JsonResponse({
        "status": "ok", "authenticated": True,
        "expires_at": request.session.get("_kfb_expires_at", ""),
    })


@login_required
def dashboard(request):
    role = user_role(request.user)
    today = timezone.localdate()
    start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    show_patient_queue = role in {Role.OWNER, Role.RECEPTION, Role.CLINICIAN, Role.NURSE}
    show_exceptions = has_capability(role, "exceptions")
    context = {
        "role": role,
        "show_exceptions": show_exceptions,
        "show_patient_queue": show_patient_queue,
    }
    if show_exceptions:
        context["exceptions"] = ExceptionRecord.objects.exclude(
            status=ExceptionRecord.Status.RESOLVED
        ).order_by("-created_at")[:6]
    if show_patient_queue:
        context["queue"] = open_clinical_encounters().select_related("patient").order_by("created_at")[:8]
    if role == Role.RECEPTION:
        context["my_shift"] = CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN).first()
    if role in {Role.PHARMACY, Role.PROCUREMENT, Role.OWNER}:
        # The people who can act on a shortage are the only ones shown one.
        position = stock_position()
        context.update({
            "stock": position,
            "low_stock": position["below_reorder"][:6],
            "expiring_stock": position["expiring_soon"][:6],
        })
        if role in {Role.PHARMACY, Role.PROCUREMENT}:
            context["unchecked_deliveries"] = GoodsReceipt.objects.filter(checked_by__isnull=True).count()
    if role == Role.OWNER:
        valid_payments = Payment.objects.filter(status=Payment.Status.VALID, received_at__gte=start)
        context.update({
            "net_billed": net_billed_since(start),
            "verified_collections": verified_collections_since(start),
            "unverified_mpesa": valid_payments.filter(method=Payment.Method.MPESA, verification_status=Payment.Verification.UNVERIFIED).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
            "receivables": outstanding_receivables(),
            "occupied_beds": Admission.objects.filter(discharged_at__isnull=True).count(),
            "active_beds": Bed.objects.filter(active=True, ward__active=True).count(),
        })
    return render(request, "hospital/dashboard.html", context)


@role_required(Role.OWNER, Role.REVIEWER)
def reports(request):
    context = _report_context(request.GET.get("days", "7"))
    context.update({
        "unverified_payments": Paginator(
            Payment.objects.filter(
                method=Payment.Method.MPESA,
                status=Payment.Status.VALID,
                verification_status=Payment.Verification.UNVERIFIED,
            ).select_related("received_by__staff_profile").order_by("-received_at", "-pk"), 50
        ).get_page(request.GET.get("mpesa_page")),
        "pending_credits": Paginator(
            CreditNote.objects.filter(
                status=CreditNote.Status.PENDING
            ).select_related("invoice", "requested_by__staff_profile").order_by("-created_at", "-pk"), 50
        ).get_page(request.GET.get("credits_page")),
    })
    return render(request, "hospital/reports.html", context)


def _report_context(days_value):
    days = _period_days(days_value)
    start = timezone.now() - timedelta(days=days)
    invoices = Invoice.objects.filter(posted_at__gte=start).exclude(status=Invoice.Status.DRAFT)
    payments = Payment.objects.filter(received_at__gte=start, status=Payment.Status.VALID)
    return {
        "days": days,
        "net_billed": net_billed_since(start),
        "verified_collections": verified_collections_since(start),
        "unverified_mpesa": payments.filter(method=Payment.Method.MPESA, verification_status=Payment.Verification.UNVERIFIED).aggregate(v=Sum("amount"))["v"] or Decimal("0.00"),
        "receivables": outstanding_receivables(),
        "department_activity": Encounter.objects.filter(created_at__gte=start).values("department").annotate(total=Count("id")).order_by("-total"),
        "recent_invoices": with_invoice_financials(
            invoices.select_related("patient").order_by("-posted_at")
        )[:25],
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


@role_required(Role.OWNER, Role.CLINICIAN, Role.NURSE, Role.LAB)
def departments(request):
    orders = ServiceOrder.objects.select_related(
        "encounter__patient", "service", "requested_by__staff_profile"
    )
    work, work_links = _worklist_page(
        request, orders.exclude(status=ServiceOrder.Status.RELEASED).order_by("created_at", "pk"),
        40, "work_page", "active-work",
    )
    history, history_links = _worklist_page(
        request, orders.filter(status=ServiceOrder.Status.RELEASED).order_by("-released_at", "-pk"),
        30, "history_page", "work-history",
    )
    return render(request, "hospital/departments.html", {
        "work": work, "work_links": work_links,
        "history": history, "history_links": history_links,
    })


@role_required(Role.OWNER, Role.LAB, Role.CLINICIAN)
def service_order_update(request, pk):
    order = get_object_or_404(ServiceOrder.objects.select_related("encounter__patient", "service"), pk=pk)
    can_update = order.status != ServiceOrder.Status.RELEASED and (
        order.status != ServiceOrder.Status.REVIEW
        or request.user.id not in {order.requested_by_id, order.performer_id}
    )
    form = ServiceResultForm(request.POST or None, instance=order)
    if request.method == "POST" and form.is_valid():
        try:
            update_service_order(
                actor=request.user, order_id=order.pk,
                status=form.cleaned_data["status"], result=form.cleaned_data["result"], request=request,
            )
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
            order.refresh_from_db()
        else:
            messages.success(request, "Department work item updated.")
            return redirect("departments")
    return render(request, "hospital/service_result_form.html", {
        "form": form, "order": order, "can_update": can_update,
    })


@role_required(Role.CLINICIAN, Role.NURSE, Role.OWNER)
def wards(request):
    active_admissions = Prefetch(
        "beds__admissions",
        queryset=Admission.objects.filter(discharged_at__isnull=True).select_related("patient"),
        to_attr="active_admissions",
    )
    wards_qs = Ward.objects.filter(active=True).prefetch_related("beds", active_admissions)
    return render(request, "hospital/wards.html", {"wards": wards_qs})


@role_required(Role.OWNER, Role.CLINICIAN)
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


@role_required(Role.OWNER, Role.CLINICIAN)
def admission_discharge(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        admission = discharge_admission(
            actor=request.user, admission_id=pk,
            summary=request.POST.get("summary", ""), request=request,
        )
        messages.success(request, f"{admission.patient.full_name} clinically discharged. The bed is available; any outstanding balance stays open.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("wards")


@role_required(Role.EYE, Role.CLINICIAN, Role.OWNER)
def eye_clinic(request):
    waiting = EyeCase.objects.select_related("patient", "session").order_by("status", "created_at")
    sessions = EyeSession.objects.annotate(patient_count=Count("cases"), eye_count=Sum(models_eye_count())).order_by("-session_date")
    payables = ClinicianPayable.objects.select_related("eye_case__patient")
    return render(request, "hospital/eye.html", {"waiting": waiting, "sessions": sessions, "payables": payables})


@role_required(Role.OWNER, Role.EYE, Role.CLINICIAN)
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


@login_required
def screen_lock(request):
    profile = request.user.staff_profile
    profile.locked_at = timezone.now()
    profile.save(update_fields=["locked_at"])
    return redirect("screen_unlock")


@login_required
def screen_unlock(request):
    locked_out = LoginAttempt.is_locked(request.user.username, request.META.get("REMOTE_ADDR"))
    if locked_out:
        return render(request, "hospital/unlock.html", {
            "lockout_minutes": LoginAttempt.LOCKOUT_WINDOW_MINUTES,
        }, status=429)
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
        if LoginAttempt.is_locked(username, request.META.get("REMOTE_ADDR")):
            context = self.get_context_data(form=self.get_form())
            context["lockout_minutes"] = LoginAttempt.LOCKOUT_WINDOW_MINUTES
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)


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


def owner_brief_context(days=7, role=Role.OWNER):
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
            "action": "Propose write-off" if has_capability(role, "request_write_off") else "Review expired stock",
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
            "action": "Start a count" if has_capability(role, "start_stock_count") else "Review count sheets",
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
    context = owner_brief_context(role=user_role(request.user))
    context.update({"shrinkage": shrinkage(90), "custody": departmental_custody()})
    return render(request, "hospital/owner_brief.html", context)
