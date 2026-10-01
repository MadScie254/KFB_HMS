from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import connection
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from . import billing_views as _billing_views
from . import clinical_board_views as _clinical_board_views
from . import clinical_views as _clinical_views
from . import custody_views as _custody_views
from . import import_views as _import_views
from . import patient_views as _patient_views
from . import pharmacy_views as _pharmacy_views
from . import purchasing_views as _purchasing_views
from . import reporting_views as _reporting_views
from . import stock_views as _stock_views
from . import view_helpers as _view_helpers
from .analytics import (
    stock_position,
)
from .models import (
    Admission,
    Bed,
    CashShift,
    Encounter,
    ExceptionRecord,
    GoodsReceipt,
    LoginAttempt,
    Patient,
    Payment,
    PharmacyOrder,
    Role,
    Setting,
    StockBatch,
)
from .permissions import has_capability, role_required, user_role

# Keep URL callbacks and the established imports available from hospital.views.
_filter_query = _view_helpers._filter_query
_period_days = _view_helpers._period_days
_validation_message = _view_helpers._validation_message
_worklist_page = _view_helpers._worklist_page

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

invoiced_total = _reporting_views.invoiced_total
net_billed_since = _reporting_views.net_billed_since
outstanding_receivables = _reporting_views.outstanding_receivables
verified_collections_since = _reporting_views.verified_collections_since
reports = _reporting_views.reports
_report_context = _reporting_views._report_context
report_download_pdf = _reporting_views.report_download_pdf
exceptions = _reporting_views.exceptions
audit_review = _reporting_views.audit_review
stock_intelligence = _reporting_views.stock_intelligence
owner_brief_context = _reporting_views.owner_brief_context
owner_brief = _reporting_views.owner_brief

departments = _clinical_board_views.departments
service_order_update = _clinical_board_views.service_order_update
wards = _clinical_board_views.wards
admission_create = _clinical_board_views.admission_create
admission_discharge = _clinical_board_views.admission_discharge
eye_clinic = _clinical_board_views.eye_clinic
eye_case_complete = _clinical_board_views.eye_case_complete
models_eye_count = _clinical_board_views.models_eye_count

# Search returns only records whose destination page the role can open.
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
