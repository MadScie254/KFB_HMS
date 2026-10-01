"""Cashier, payment, refund, shift, and credit-note screens."""

import uuid
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CreditNoteForm, PaymentForm, RefundRequestForm, ShiftCloseForm, ShiftOpenForm
from .models import CashShift, Invoice, Payment, Refund, Role
from .permissions import role_required
from .services import (
    approve_credit_note,
    audit,
    close_cash_shift,
    open_cash_shift,
    pay_refund,
    record_payment,
    request_refund,
    review_cash_shift,
    review_mpesa,
    review_refund,
)
from .view_helpers import _validation_message


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


@role_required(Role.OWNER, Role.RECEPTION)
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
            if payment.status != Payment.Status.VALID:
                messages.warning(request, f"Payment claim {payment.receipt_number} was already {payment.get_status_display().lower()}. Start a new payment request if needed.")
            elif payment.method == Payment.Method.MPESA and payment.verification_status == Payment.Verification.UNVERIFIED:
                messages.info(request, f"M-PESA claim {payment.receipt_number} recorded pending independent verification. The invoice remains unpaid until then.")
            else:
                messages.success(request, f"Payment recorded. Receipt {payment.receipt_number}.")
            return redirect("receipt", pk=payment.pk)
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
    return render(request, "hospital/payment_form.html", {"form": form, "invoice": invoice, "idempotency_key": uuid.uuid4().hex})


@role_required(Role.RECEPTION, Role.OWNER)
def receipt(request, pk):
    payment = get_object_or_404(Payment.objects.prefetch_related("allocations__invoice"), pk=pk)
    if request.method == "HEAD":
        return HttpResponse()
    issued_now = Payment.objects.filter(pk=pk, receipt_issued_at__isnull=True).update(
        receipt_issued_at=timezone.now()
    ) == 1
    duplicate = not issued_now or request.GET.get("reprint") == "1"
    if issued_now:
        audit(request.user, "receipt.issued", payment, request=request)
    if duplicate:
        audit(
            request.user, "receipt.reprinted", payment,
            reason="Explicit reprint" if request.GET.get("reprint") == "1" else "Receipt reopened",
            request=request,
        )
    refund_paid = payment.refunds.filter(status=Refund.Status.PAID).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    return render(request, "hospital/receipt.html", {
        "payment": payment, "duplicate": duplicate, "refund_paid": refund_paid,
    })


@role_required(Role.RECEPTION, Role.OWNER)
def refund_request(request, pk):
    payment = get_object_or_404(Payment.objects.prefetch_related("allocations__invoice"), pk=pk)
    form = RefundRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            refund = request_refund(actor=request.user, payment_id=pk, request=request, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
        else:
            messages.success(request, f"Refund request #{refund.pk} sent for independent review.")
            return redirect("refunds")
    return render(request, "hospital/refund_request.html", {"payment": payment, "form": form})


@role_required(Role.RECEPTION, Role.OWNER, Role.REVIEWER)
def refunds(request):
    records = Refund.objects.filter(invoice__isnull=False).select_related(
        "payment", "invoice", "requested_by", "reviewed_by", "paid_by", "paid_shift",
    ).order_by("-created_at")[:100]
    return render(request, "hospital/refunds.html", {"refunds": records})


@role_required(Role.RECEPTION, Role.OWNER, Role.REVIEWER)
def refund_action(request, pk):
    if request.method != "POST":
        raise Http404
    get_object_or_404(Refund, pk=pk)
    try:
        action = request.POST.get("action")
        if action in {"approve", "reject"}:
            review_refund(actor=request.user, refund_id=pk, approve=action == "approve", request=request)
        elif action == "pay":
            pay_refund(actor=request.user, refund_id=pk, request=request)
        else:
            raise ValidationError("Choose a valid refund action.")
        messages.success(request, "Refund action recorded.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("refunds")


@role_required(Role.OWNER, Role.RECEPTION)
def shift_manage(request):
    shift = CashShift.objects.filter(cashier=request.user, status=CashShift.Status.OPEN).first()
    form = ShiftCloseForm(request.POST or None, instance=shift) if shift else ShiftOpenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            if shift:
                close_cash_shift(actor=request.user, request=request, **form.cleaned_data)
                messages.success(request, "Shift closed and submitted for independent review.")
            else:
                open_cash_shift(actor=request.user, request=request, **form.cleaned_data)
                messages.success(request, "Shift opened.")
            return redirect("dashboard")
        except ValidationError as exc:
            form.add_error(None, _validation_message(exc))
            if shift:
                shift.refresh_from_db()
    return render(request, "hospital/shift_form.html", {"form": form, "shift": shift})


@role_required(Role.OWNER, Role.REVIEWER)
def shift_review(request):
    if request.method == "POST":
        try:
            review_cash_shift(actor=request.user, shift_id=request.POST.get("shift_id"), request=request)
            messages.success(request, "Shift independently reviewed.")
        except (ValidationError, CashShift.DoesNotExist, ValueError) as exc:
            messages.error(request, _validation_message(exc))
        return redirect("shift_review")
    shifts = CashShift.objects.filter(status=CashShift.Status.CLOSED).exclude(cashier=request.user).select_related("cashier").order_by("-closed_at")[:100]
    return render(request, "hospital/shift_review.html", {"shifts": shifts})


@role_required(Role.OWNER, Role.REVIEWER)
def payment_verify(request, pk):
    if request.method != "POST":
        raise Http404
    try:
        approve = request.POST.get("decision") == "verify"
        payment = review_mpesa(
            actor=request.user,
            payment_id=pk,
            approve=approve,
            review_notes=request.POST.get("review_notes", ""),
            request=request,
        )
        messages.success(request, f"M-PESA {payment.reference} {'verified' if approve else 'rejected'} independently.")
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
