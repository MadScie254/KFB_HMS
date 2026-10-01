"""Stock position, delivery evidence, and count review screens."""

import mimetypes
from decimal import Decimal
from pathlib import Path

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header

from .analytics import receiving_summary, stock_activity, stock_position
from .forms import (
    DeliveryCheckForm,
    GoodsReceiptForm,
    GoodsReceiptLineFormSet,
    StockCountOpenForm,
    StockCountReviewForm,
)
from .models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, Role, StockCount, StockMovement
from .permissions import has_capability, role_required, user_role
from .services import audit, check_delivery, open_stock_count, receive_delivery, review_stock_count, submit_stock_count
from .view_helpers import _filter_query, _period_days, _validation_message

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
        matching_batch_items = {
            row["item"].pk for row in position["rows"]
            if needle in row["batch"].batch_number.lower()
        }
        products = [
            row for row in products
            if needle in row["item"].name.lower()
            or needle in row["item"].code.lower()
            or row["item"].pk in matching_batch_items
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
        "can_receive": has_capability(user_role(request.user), "receive_delivery"),
        "can_count": has_capability(user_role(request.user), "start_stock_count"),
        "can_dispose": has_capability(user_role(request.user), "review_write_off"),
        "can_request_write_off": has_capability(user_role(request.user), "request_write_off"),
    })


@role_required(Role.PROCUREMENT, Role.PHARMACY, Role.REVIEWER, Role.OWNER)
def deliveries(request):
    """Every delivery received, with its invoice evidence and check status."""
    receipts = GoodsReceipt.objects.select_related(
        "purchase_order__supplier", "received_by__staff_profile", "checked_by__staff_profile"
    ).prefetch_related("lines__order_line__item")
    awaiting = Paginator(
        receipts.filter(checked_by__isnull=True).order_by("-delivered_at", "-pk"), 20
    ).get_page(request.GET.get("check_page"))
    history = Paginator(
        receipts.order_by("-delivered_at", "-pk"), 50
    ).get_page(request.GET.get("history_page"))
    open_orders = PurchaseOrder.objects.filter(
        status__in=["approved", "part_received"]
    ).select_related("supplier").prefetch_related("lines__item").order_by("-created_at")
    return render(request, "hospital/deliveries.html", {
        "receipts": history,
        "awaiting_check": awaiting,
        "open_orders": open_orders,
        "summary": receiving_summary(30),
        "can_receive": has_capability(user_role(request.user), "receive_delivery"),
    })


@role_required(Role.OWNER, Role.PROCUREMENT, Role.PHARMACY)
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

    order_lines = list(order.lines.all())
    received_so_far = dict(
        GoodsReceiptLine.objects.filter(order_line__order=order)
        .values("order_line_id").annotate(total=Sum("quantity_received"))
        .values_list("order_line_id", "total")
    )
    return render(request, "hospital/goods_receipt_form.html", {
        "form": form,
        "formset": lines,
        "order": order,
        "received_so_far": [
            (line, received_so_far.get(line.pk, Decimal("0.000")), Decimal(str(line.quantity_base_units)) - received_so_far.get(line.pk, Decimal("0.000")))
            for line in order_lines
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
        "can_open": has_capability(user_role(request.user), "start_stock_count"),
        "can_review": has_capability(user_role(request.user), "review_stock_count"),
    })


@role_required(Role.OWNER, Role.PHARMACY, Role.PROCUREMENT)
def stock_count_open(request):
    if request.method != "POST":
        raise Http404
    form = StockCountOpenForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Only Pharmacy stock can be counted against this ledger.")
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
    lines = list(count.lines.select_related("batch__item").order_by("batch__item__name", "batch__expiry_date"))
    form_error = ""
    if request.method == "POST":
        counted = {}
        reasons = {}
        quantity_field = forms.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0"))
        for line in lines:
            line.submitted_counted = request.POST.get(f"counted-{line.pk}", "")
            line.submitted_reason = request.POST.get(f"reason-{line.pk}", "")
            reasons[line.pk] = line.submitted_reason.strip()
            try:
                counted[line.pk] = quantity_field.clean(line.submitted_counted)
            except ValidationError as exc:
                line.count_error = _validation_message(exc)
        if any(getattr(line, "count_error", "") for line in lines):
            form_error = "Correct the marked counts. Your other entries are still here."
        else:
            try:
                submit_stock_count(actor=request.user, count_id=count.pk, counted=counted, reasons=reasons, request=request)
            except ValidationError as exc:
                form_error = _validation_message(exc)
                count.refresh_from_db()
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
        "form_error": form_error,
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
