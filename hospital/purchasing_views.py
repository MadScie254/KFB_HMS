"""Supplier changes and purchase-order screens."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import PurchaseOrderForm, PurchaseOrderLineFormSet, SupplierChangeForm
from .models import PurchaseOrder, PurchaseOrderLine, Role, Supplier, SupplierChangeRequest
from .permissions import role_required
from .services import approve_purchase_order, audit, request_supplier_change, review_supplier_change
from .view_helpers import _validation_message, _worklist_page


@role_required(Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def supplier_changes(request):
    suppliers = Supplier.objects.order_by("name")
    changes = SupplierChangeRequest.objects.select_related(
        "supplier", "requested_by", "reviewed_by",
    ).order_by("-created_at")[:100]
    return render(request, "hospital/supplier_changes.html", {
        "suppliers": suppliers, "changes": changes,
    })


@role_required(Role.PROCUREMENT, Role.OWNER)
def supplier_change_request(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    form = SupplierChangeForm(request.POST or None, initial={
        "proposed_name": supplier.name,
        "proposed_phone": supplier.phone,
        "proposed_payment_details": supplier.payment_details,
        "proposed_active": supplier.active,
    })
    if request.method == "POST" and form.is_valid():
        try:
            request_supplier_change(actor=request.user, supplier_id=pk, request=request, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
        else:
            messages.success(request, "Supplier change sent for independent review.")
            return redirect("supplier_changes")
    return render(request, "hospital/supplier_change_form.html", {"supplier": supplier, "form": form})


@role_required(Role.REVIEWER, Role.OWNER)
def supplier_change_review(request, pk):
    if request.method != "POST":
        raise Http404
    get_object_or_404(SupplierChangeRequest, pk=pk)
    try:
        decision = request.POST.get("decision")
        if decision not in {"approve", "reject"}:
            raise ValidationError("Choose a valid review decision.")
        review_supplier_change(actor=request.user, change_id=pk, approve=decision == "approve", request=request)
        messages.success(request, "Supplier change reviewed.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("supplier_changes")


@role_required(Role.PROCUREMENT, Role.REVIEWER, Role.OWNER)
def purchasing(request):
    # The template prints both staff profiles per row; without them on the
    # select_related chain that is two extra queries for every purchase order.
    orders = PurchaseOrder.objects.select_related(
        "supplier", "requested_by__staff_profile", "approved_by__staff_profile"
    ).prefetch_related("lines__item", "receipts")
    active, active_links = _worklist_page(
        request, orders.filter(status__in=["requested", "approved", "part_received"])
        .order_by("-created_at", "-pk"), 30, "active_page", "active-orders",
    )
    history, history_links = _worklist_page(
        request, orders.filter(status__in=["received", "cancelled"])
        .order_by("-created_at", "-pk"), 30, "history_page", "order-history",
    )
    return render(request, "hospital/purchasing.html", {
        "orders": active, "active_links": active_links,
        "history": history, "history_links": history_links,
    })


@role_required(Role.OWNER, Role.PROCUREMENT)
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
