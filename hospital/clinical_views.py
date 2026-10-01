"""Encounter queue, notes, prescriptions, and clinical orders."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Max, Value, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import ClinicalNoteForm, EncounterForm, PrescriptionForm, PrescriptionFormSet, ServiceOrderForm
from .models import (
    Admission,
    ClinicalNote,
    Encounter,
    ExceptionRecord,
    Patient,
    Prescription,
    PrescriptionItem,
    Role,
    ServiceOrder,
)
from .permissions import role_required
from .services import audit, close_encounter
from .view_helpers import _validation_message, _worklist_page


@role_required(Role.OWNER, Role.RECEPTION, Role.CLINICIAN)
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


#: Clinical priority. Never sort the queue on the raw ``urgency`` string —
#: alphabetically "routine" precedes "urgent", which pushes urgent patients
#: below routine ones.
TRIAGE_RANK = Case(
    When(urgency="emergency", then=Value(0)),
    When(urgency="urgent", then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)


def open_clinical_encounters():
    active_admissions = Admission.objects.filter(discharged_at__isnull=True).values("encounter_id")
    return Encounter.objects.exclude(status=Encounter.Status.CLOSED).exclude(pk__in=active_admissions)


@role_required(Role.OWNER, Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.LAB)
def queue(request):
    encounters = (
        open_clinical_encounters()
        .select_related("patient", "assigned_clinician")
        .annotate(triage_rank=TRIAGE_RANK)
        .order_by("triage_rank", "created_at", "pk")
    )
    encounters, queue_links = _worklist_page(request, encounters, 50, "page", "queue-list")
    return render(request, "hospital/queue.html", {"encounters": encounters, "queue_links": queue_links})


@role_required(Role.OWNER, Role.CLINICIAN)
def encounter_close(request, encounter_id):
    if request.method != "POST":
        raise Http404
    try:
        encounter = close_encounter(
            actor=request.user, encounter_id=encounter_id,
            reason=request.POST.get("reason", ""), request=request,
        )
        messages.success(request, f"Encounter {encounter.encounter_number} closed. Any outstanding balance remains visible to reception.")
    except ValidationError as exc:
        messages.error(request, _validation_message(exc))
    return redirect("queue")


@role_required(Role.OWNER, Role.CLINICIAN)
def clinical_note(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    notes = ClinicalNote.objects.filter(encounter=encounter, author=request.user)
    draft = notes.filter(status=ClinicalNote.Status.DRAFT).select_related("parent_note").first()
    latest_signed = notes.filter(status=ClinicalNote.Status.SIGNED).order_by("-version", "-pk").first()
    parent = draft.parent_note if draft else latest_signed
    initial = {
        "expected_revision": draft.revision if draft else 0,
        "expected_parent_note_id": parent.pk if parent else 0,
    }
    if parent and not draft:
        initial.update({field: getattr(parent, field) for field in (
            "complaints", "history", "examination", "assessment", "plan", "follow_up",
        )})
    form = ClinicalNoteForm(request.POST or None, instance=draft, initial=initial, is_amendment=bool(parent))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Serialise note numbering on the encounter. Two tabs can no longer
            # calculate the same next version and collide on the unique key.
            locked_encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
            current_draft = ClinicalNote.objects.select_for_update().filter(
                encounter=locked_encounter,
                author=request.user,
                status=ClinicalNote.Status.DRAFT,
            ).first()
            current_signed = ClinicalNote.objects.filter(
                encounter=locked_encounter, author=request.user, status=ClinicalNote.Status.SIGNED,
            ).order_by("-version", "-pk").first()
            current_parent = current_draft.parent_note if current_draft else current_signed
            expected_revision = form.cleaned_data["expected_revision"]
            if expected_revision != (current_draft.revision if current_draft else 0):
                form.add_error(None, "This note changed in another tab. Reload before saving again.")
            elif (form.cleaned_data["expected_parent_note_id"] or 0) != (current_parent.pk if current_parent else 0):
                form.add_error(None, "A signed note changed in another tab. Reload before creating an amendment.")
            elif current_draft and current_signed and current_draft.parent_note_id != current_signed.pk:
                form.add_error(None, "A newer signed note exists. Reload before continuing this amendment.")
            else:
                locked_form = ClinicalNoteForm(request.POST, instance=current_draft, is_amendment=bool(current_parent))
                if locked_form.is_valid():
                    note = locked_form.save(commit=False)
                    if not note.pk:
                        note.encounter = locked_encounter
                        note.author = request.user
                        note.parent_note = current_parent
                        note.version = (
                            ClinicalNote.objects.filter(
                                encounter=locked_encounter, author=request.user
                            ).aggregate(v=Max("version"))["v"]
                            or 0
                        ) + 1
                    else:
                        note.revision = current_draft.revision + 1
                    note.save()
                    if request.POST.get("action") == "sign":
                        note.sign()
                        if current_parent:
                            current_parent.status = ClinicalNote.Status.AMENDED
                            current_parent.save(update_fields=["status", "updated_at"])
                        if locked_encounter.status != Encounter.Status.CLOSED:
                            has_pending_tests = ServiceOrder.objects.filter(encounter=locked_encounter).exclude(
                                status=ServiceOrder.Status.RELEASED
                            ).exists()
                            locked_encounter.status = Encounter.Status.TESTS if has_pending_tests else Encounter.Status.PHARMACY
                            locked_encounter.save(update_fields=["status", "updated_at"])
                        action = "clinical_note.amended" if current_parent else "clinical_note.signed"
                        audit(request.user, action, note, reason=note.amendment_reason, request=request)
                        messages.success(request, "Amendment signed and linked to the original." if current_parent else "Clinical note signed. Future changes require an attributed amendment.")
                    else:
                        audit(request.user, "clinical_note.saved", note, request=request)
                        messages.success(request, "Draft saved on the server.")
                    return redirect("patient_detail", pk=encounter.patient_id)
    return render(request, "hospital/clinical_note_form.html", {
        "form": form, "encounter": encounter, "draft": draft, "parent": parent,
    })


@role_required(Role.OWNER, Role.CLINICIAN)
def prescription_create(request, encounter_id):
    encounter = get_object_or_404(Encounter.objects.select_related("patient"), pk=encounter_id)
    # Accept the original single-line payload as well as the new formset so
    # bookmarked clients and downtime back-entry tools keep working.
    legacy_payload = request.method == "POST" and "items-TOTAL_FORMS" not in request.POST
    form = PrescriptionForm(request.POST or None) if legacy_payload else None
    formset = PrescriptionFormSet(request.POST or None, prefix="items")
    is_valid = form.is_valid() if legacy_payload else formset.is_valid()
    if request.method == "POST" and is_valid:
        lines = [form.cleaned_data] if legacy_payload else [
            row.cleaned_data for row in formset
            if row.cleaned_data and not row.cleaned_data.get("DELETE")
        ]
        with transaction.atomic():
            prescription = Prescription.objects.create(encounter=encounter, prescriber=request.user, signed_at=timezone.now())
            for line in lines:
                PrescriptionItem.objects.create(
                    prescription=prescription,
                    product=line["product"], strength=line["strength"],
                    dose=line["dose"], route=line["route"], frequency=line["frequency"],
                    duration=line["duration"], quantity_base_units=line["quantity_base_units"],
                    instructions=line["instructions"],
                )
            audit(request.user, "prescription.signed", prescription, request=request)
        messages.success(request, f"Prescription with {len(lines)} item(s) signed and sent to pharmacy for pricing.")
        return redirect("patient_detail", pk=encounter.patient_id)
    return render(request, "hospital/prescription_form.html", {"form": form, "formset": formset, "encounter": encounter})


@role_required(Role.OWNER, Role.CLINICIAN)
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
