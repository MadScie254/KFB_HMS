"""Departmental custody, write-off, and batch-disposition screens."""

from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .analytics import departmental_custody, stock_position
from .forms import (
    BatchDispositionForm,
    DepartmentIssueForm,
    DepartmentIssueLineFormSet,
    WriteOffRequestForm,
    WriteOffReviewForm,
)
from .models import DepartmentIssue, Patient, Role, StockWriteOff
from .permissions import has_capability, role_required, user_role
from .services import account_for_issue, issue_to_department, request_write_off, review_write_off, set_batch_disposition
from .view_helpers import _validation_message, _worklist_page


@role_required(Role.PHARMACY, Role.NURSE, Role.CLINICIAN, Role.PROCUREMENT, Role.OWNER, Role.REVIEWER)
def custody(request):
    """Stock that left the pharmacy and has not yet been accounted for."""
    position = departmental_custody()
    issues = DepartmentIssue.objects.select_related("patient", "issued_by__staff_profile")
    outstanding, outstanding_links = _worklist_page(
        request,
        issues.filter(status=DepartmentIssue.Status.OUTSTANDING)
        .prefetch_related("lines__batch__item").order_by("issued_at", "pk"),
        40, "outstanding_page", "outstanding-issues",
    )
    settled, settled_links = _worklist_page(
        request, issues.filter(status=DepartmentIssue.Status.SETTLED).order_by("-issued_at", "-pk"),
        15, "settled_page", "settled-issues",
    )
    role = user_role(request.user)
    return render(request, "hospital/custody.html", {
        "position": position,
        "outstanding": outstanding,
        "outstanding_links": outstanding_links,
        "settled": settled,
        "settled_links": settled_links,
        "can_issue": role == Role.PHARMACY,
        "can_account": role in {Role.NURSE, Role.CLINICIAN, Role.PHARMACY},
    })


@role_required(Role.OWNER, Role.PHARMACY)
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


@role_required(Role.OWNER, Role.NURSE, Role.CLINICIAN, Role.PHARMACY)
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
        "form": WriteOffRequestForm() if has_capability(role, "request_write_off") else None,
        "review_form": WriteOffReviewForm(),
        "can_request": has_capability(role, "request_write_off"),
        "can_review": has_capability(role, "review_write_off"),
        "expired": stock_position()["expired"],
    })


@role_required(Role.OWNER, Role.PHARMACY, Role.PROCUREMENT)
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
