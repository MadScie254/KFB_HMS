"""Financial reporting, audit review, stock intelligence, and owner brief."""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header

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
from .models import (
    AuditEvent,
    CreditNote,
    Encounter,
    ExceptionRecord,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    Refund,
    Role,
)
from .pdf_reports import build_financial_report_pdf
from .permissions import has_capability, role_required, user_role
from .services import audit
from .view_helpers import _filter_query, _period_days


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
