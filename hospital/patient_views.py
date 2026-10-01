"""Patient search, registration, chart access, and attachments."""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header

from .analytics import with_invoice_financials
from .forms import ClinicalAttachmentForm, PatientForm
from .models import ClinicalAttachment, ClinicalNote, Encounter, Patient, Role
from .pdf_reports import build_patient_access_pdf
from .permissions import has_capability, role_required, user_role
from .services import audit
from .view_helpers import _worklist_page


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.OWNER, Role.EYE)
def patient_list(request):
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(patient_number__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone__icontains=query)
            | Q(id_number__iexact=query)
            | Q(guardian_phone__icontains=query)
        )
    page = Paginator(patients, 50).get_page(request.GET.get("page"))
    return render(request, "hospital/patient_list.html", {"patients": page, "page": page, "query": query})


@role_required(Role.OWNER, Role.RECEPTION)
def patient_create(request):
    form = PatientForm(request.POST or None)
    duplicate_candidates = []
    if request.method == "POST" and form.is_valid():
        duplicate_candidates = Patient.objects.filter(
            first_name__iexact=form.cleaned_data["first_name"],
            last_name__iexact=form.cleaned_data["last_name"],
        )
        if form.cleaned_data.get("phone"):
            duplicate_candidates = duplicate_candidates.filter(phone=form.cleaned_data["phone"])
        if duplicate_candidates.exists() and request.POST.get("confirm_duplicate") != "yes":
            messages.warning(request, "Possible matching patient found. Review before creating a separate record.")
        else:
            patient = form.save(commit=False)
            patient.registered_by = request.user
            patient.is_demo = settings.DEMO_MODE
            patient.save()
            audit(request.user, "patient.registered", patient, request=request)
            messages.success(request, f"Patient {patient.patient_number} registered.")
            return redirect("patient_detail", pk=patient.pk)
    return render(request, "hospital/patient_form.html", {"form": form, "duplicates": duplicate_candidates})


@role_required(Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.OWNER, Role.EYE)
def patient_detail(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    role = user_role(request.user)
    audit(request.user, "patient.viewed", patient, request=request)
    active_encounters, active_links = _worklist_page(
        request, patient.encounters.exclude(status=Encounter.Status.CLOSED).order_by("-created_at", "-pk"),
        10, "active_page", "visits",
    )
    encounter_history, visit_links = _worklist_page(
        request, patient.encounters.filter(status=Encounter.Status.CLOSED).order_by("-created_at", "-pk"),
        20, "visit_page", "visits",
    )
    notes = attachments = invoices = None
    note_links = attachment_links = invoice_links = None
    if has_capability(role, "view_notes"):
        notes, note_links = _worklist_page(
            request,
            ClinicalNote.objects.filter(encounter__patient=patient)
            .select_related("author", "parent_note")
            .order_by("-created_at", "-pk"),
            20, "note_page", "notes",
        )
    if has_capability(role, "view_attachments"):
        attachments, attachment_links = _worklist_page(
            request,
            patient.attachments.select_related("uploaded_by__staff_profile").order_by("-created_at", "-pk"),
            20, "attachment_page", "attachments",
        )
    if has_capability(role, "view_billing"):
        invoices, invoice_links = _worklist_page(
            request, with_invoice_financials(patient.invoices.order_by("-created_at", "-pk")),
            20, "invoice_page", "billing",
        )
    return render(request, "hospital/patient_detail.html", {
        "patient": patient,
        "invoices": invoices,
        "invoice_links": invoice_links,
        "notes": notes,
        "note_links": note_links,
        "active_encounters": active_encounters,
        "active_links": active_links,
        "encounter_history": encounter_history,
        "visit_links": visit_links,
        "attachments": attachments,
        "attachment_links": attachment_links,
        "attachment_form": ClinicalAttachmentForm() if has_capability(role, "upload_attachment") else None,
    })


@role_required(Role.OWNER, Role.CLINICIAN, Role.NURSE)
def patient_attachment_upload(request, pk):
    if request.method != "POST":
        raise Http404
    patient = get_object_or_404(Patient, pk=pk)
    form = ClinicalAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        attachment = form.save(commit=False)
        attachment.patient = patient
        attachment.original_name = Path(attachment.file.name).name[:255]
        attachment.uploaded_by = request.user
        attachment.save()
        audit(request.user, "clinical_attachment.uploaded", attachment, after={"name": attachment.original_name}, request=request)
        messages.success(request, "Clinical attachment uploaded securely.")
    else:
        messages.error(request, " ".join(
            str(error) for errors in form.errors.values() for error in errors
        ))
    return redirect("patient_detail", pk=patient.pk)


@role_required(Role.CLINICIAN, Role.NURSE, Role.OWNER)
def patient_attachment_download(request, pk):
    attachment = get_object_or_404(ClinicalAttachment.objects.select_related("patient"), pk=pk)
    content_type = mimetypes.guess_type(attachment.original_name)[0] or "application/octet-stream"
    attachment.file.open("rb")
    response = FileResponse(attachment.file, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(True, attachment.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "clinical_attachment.downloaded", attachment, request=request)
    return response


@role_required(Role.OWNER, Role.CLINICIAN, Role.NURSE)
def patient_access_pdf(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    pdf = build_patient_access_pdf(
        patient=patient,
        hospital_name=settings.HOSPITAL_NAME,
        generated_by=str(request.user.staff_profile),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(
        True, f"KFBH-patient-record-{patient.patient_number}.pdf"
    )
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "patient.exported_pdf", patient, request=request)
    return response
