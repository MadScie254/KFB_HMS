"""Pharmacy orders, prescription pricing, and dispensing screens."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .analytics import with_invoice_financials
from .forms import PharmacyBasketForm
from .models import Invoice, Patient, PharmacyOrder, Prescription, Role
from .permissions import role_required, user_role
from .services import dispense_order, prepare_pharmacy_order
from .view_helpers import _validation_message


@role_required(Role.OWNER, Role.PHARMACY, Role.RECEPTION)
def pharmacy_orders(request):
    orders = PharmacyOrder.objects.select_related("patient", "prepared_by").prefetch_related(
        Prefetch("invoice", queryset=with_invoice_financials(Invoice.objects.all()))
    ).order_by("-created_at")[:100]
    pending_prescriptions = Prescription.objects.filter(status="active", pharmacyorder__isnull=True).select_related("encounter__patient", "prescriber").prefetch_related("items__product") if user_role(request.user) == Role.PHARMACY else []
    return render(request, "hospital/pharmacy_orders.html", {"orders": orders, "pending_prescriptions": pending_prescriptions})


@role_required(Role.OWNER, Role.PHARMACY)
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


@role_required(Role.OWNER, Role.PHARMACY)
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


@role_required(Role.OWNER, Role.PHARMACY, Role.RECEPTION)
def pharmacy_order_detail(request, pk):
    order = get_object_or_404(PharmacyOrder.objects.select_related("patient", "invoice", "prepared_by", "dispensed_by"), pk=pk)
    return render(request, "hospital/pharmacy_order_detail.html", {"order": order})


@role_required(Role.OWNER, Role.PHARMACY)
def pharmacy_dispense(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        order = dispense_order(actor=request.user, order_id=pk, idempotency_key=request.POST.get("idempotency_key") or str(pk), request=request)
        messages.success(request, f"{order.order_number} dispensed and stock posted once.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("pharmacy_order_detail", pk=pk)
