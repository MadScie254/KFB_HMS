"""Department work, admissions, and eye-clinic boards."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import AdmissionForm, ServiceResultForm
from .models import Admission, ClinicianPayable, Encounter, EyeCase, EyeSession, Role, ServiceOrder, Ward
from .permissions import role_required
from .services import audit, complete_eye_case, discharge_admission, update_service_order
from .view_helpers import _validation_message, _worklist_page


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
