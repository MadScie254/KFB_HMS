"""Bounded CSV dry runs and commits for opening hospital data."""

import csv
import hashlib
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CsvImportForm
from .models import (
    CatalogueItem,
    ImportJob,
    Invoice,
    InvoiceLine,
    Patient,
    PriceVersion,
    Role,
    StockBatch,
    StockMovement,
)
from .permissions import role_required, user_role
from .services import audit
from .view_helpers import _validation_message

IMPORT_MAX_ROWS = 5000
IMPORT_PREVIEW_MAX_AGE = timedelta(hours=24)
IMPORT_DISPLAY_ERRORS = 50


def _read_csv(uploaded, required):
    raw = uploaded.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV must be UTF-8 encoded.") from exc
    reader = csv.DictReader(io.StringIO(text), strict=True)
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValidationError(f"CSV must contain: {', '.join(sorted(required))}.")
    rows = []
    try:
        for index, row in enumerate(reader, start=2):
            if len(rows) == IMPORT_MAX_ROWS:
                raise ValidationError(f"Maximum {IMPORT_MAX_ROWS:,} rows per import.")
            rows.append((index, row))
    except csv.Error as exc:
        raise ValidationError(f"CSV could not be parsed: {exc}.") from exc
    if not rows:
        raise ValidationError("CSV must contain at least one data row.")
    return digest, rows


def _finish_csv_validation(digest, rows, errors):
    errors.sort(key=lambda error: error["row"])
    invalid_rows = {error["row"] for error in errors}
    return digest, [parsed for index, parsed in rows if index not in invalid_rows], errors


def _validate_product_csv(uploaded):
    required = {"code", "name", "department", "base_unit", "sale_unit", "units_per_sale_unit", "sale_price", "reorder_level", "prescription_required"}
    digest, source_rows = _read_csv(uploaded, required)
    rows, errors, seen = [], [], set()
    for index, row in source_rows:
        try:
            parsed = {
                "code": row["code"].strip(), "name": row["name"].strip(), "department": row["department"].strip(),
                "base_unit": row["base_unit"].strip(), "sale_unit": row["sale_unit"].strip(),
                "units_per_sale_unit": str(Decimal(row["units_per_sale_unit"])), "sale_price": str(Decimal(row["sale_price"])),
                "reorder_level": str(Decimal(row["reorder_level"])),
                "prescription_required": row["prescription_required"].strip().lower() in {"1", "true", "yes", "y"},
            }
            if not all(parsed[k] for k in ["code", "name", "department", "base_unit", "sale_unit"]):
                raise ValueError("Required text field is blank")
            if Decimal(parsed["units_per_sale_unit"]) <= 0 or Decimal(parsed["sale_price"]) <= 0:
                raise ValueError("Conversion and sale price must be greater than zero")
            if parsed["code"] in seen:
                raise ValueError("Code is duplicated in this file")
            seen.add(parsed["code"])
            rows.append((index, parsed))
        except Exception as exc:
            errors.append({"row": index, "code": row.get("code", ""), "error": str(exc)})
    existing = set(CatalogueItem.objects.filter(code__in=seen).values_list("code", flat=True))
    for index, parsed in rows:
        if parsed["code"] in existing:
            errors.append({"row": index, "code": parsed["code"], "error": "Code already exists; imports never overwrite prices silently"})
    return _finish_csv_validation(digest, rows, errors)


def _validate_patient_csv(uploaded):
    required = {
        "external_reference", "first_name", "last_name", "date_of_birth",
        "estimated_age_years", "sex", "phone", "guardian_name", "guardian_phone",
    }
    digest, source_rows = _read_csv(uploaded, required)
    rows, errors, seen = [], [], set()
    for index, row in source_rows:
        try:
            reference = row["external_reference"].strip()
            birth_date = row["date_of_birth"].strip()
            age = row["estimated_age_years"].strip()
            parsed = {
                "external_reference": reference,
                "first_name": row["first_name"].strip(),
                "last_name": row["last_name"].strip(),
                "date_of_birth": date.fromisoformat(birth_date).isoformat() if birth_date else "",
                "estimated_age_years": int(age) if age else None,
                "sex": row["sex"].strip().upper(),
                "phone": row["phone"].strip(),
                "guardian_name": row["guardian_name"].strip(),
                "guardian_phone": row["guardian_phone"].strip(),
            }
            if not reference or not parsed["first_name"] or not parsed["last_name"]:
                raise ValueError("External reference and patient names are required")
            if not parsed["date_of_birth"] and parsed["estimated_age_years"] is None:
                raise ValueError("Record a date of birth or estimated age")
            if parsed["sex"] not in {"", "F", "M", "O"}:
                raise ValueError("Sex must be F, M, O or blank")
            if reference in seen:
                raise ValueError("External reference is duplicated in this file")
            seen.add(reference)
            rows.append((index, parsed))
        except Exception as exc:
            errors.append({"row": index, "code": row.get("external_reference", ""), "error": str(exc)})
    existing = set(Patient.objects.filter(external_reference__in=seen).values_list("external_reference", flat=True))
    for index, parsed in rows:
        if parsed["external_reference"] in existing:
            errors.append({"row": index, "code": parsed["external_reference"], "error": "External reference already exists"})
    return _finish_csv_validation(digest, rows, errors)


def _validate_opening_stock_csv(uploaded):
    required = {
        "product_code", "batch_number", "expiry_date", "purchase_cost_per_base_unit",
        "physical_count_base_units", "witness_name", "count_reference",
    }
    digest, source_rows = _read_csv(uploaded, required)
    rows, errors, seen = [], [], set()
    for index, row in source_rows:
        try:
            code = row["product_code"].strip()
            batch_number = row["batch_number"].strip()
            expiry = row["expiry_date"].strip()
            cost = Decimal(row["purchase_cost_per_base_unit"])
            quantity = Decimal(row["physical_count_base_units"])
            witness = row["witness_name"].strip()
            reference = row["count_reference"].strip()
            if not batch_number or not witness or not reference:
                raise ValueError("Batch, witness and count reference are required")
            if cost < 0 or quantity <= 0:
                raise ValueError("Cost cannot be negative and physical count must be greater than zero")
            batch_key = (code, batch_number)
            if batch_key in seen:
                raise ValueError("This product batch is duplicated in the file")
            seen.add(batch_key)
            rows.append((index, {
                "product_code": code,
                "batch_number": batch_number,
                "expiry_date": date.fromisoformat(expiry).isoformat() if expiry else "",
                "purchase_cost_per_base_unit": str(cost),
                "physical_count_base_units": str(quantity),
                "witness_name": witness,
                "count_reference": reference,
            }))
        except Exception as exc:
            errors.append({"row": index, "code": row.get("product_code", ""), "error": str(exc)})
    items = dict(CatalogueItem.objects.filter(
        code__in={row["product_code"] for _, row in rows}, kind=CatalogueItem.Kind.PRODUCT,
    ).values_list("code", "pk"))
    existing = set(StockBatch.objects.filter(
        item_id__in=items.values(), batch_number__in={row["batch_number"] for _, row in rows},
    ).values_list("item_id", "batch_number")) if items else set()
    for index, parsed in rows:
        item_id = items.get(parsed["product_code"])
        if item_id is None:
            errors.append({"row": index, "code": parsed["product_code"], "error": "Product does not exist"})
        elif (item_id, parsed["batch_number"]) in existing:
            errors.append({"row": index, "code": parsed["product_code"], "error": "This product batch already exists"})
    return _finish_csv_validation(digest, rows, errors)


def _validate_opening_receivables_csv(uploaded):
    required = {
        "external_patient_reference", "external_invoice_reference", "original_invoice_date",
        "description", "department", "outstanding_amount", "review_reference",
    }
    digest, source_rows = _read_csv(uploaded, required)
    rows, errors, seen = [], [], set()
    for index, row in source_rows:
        try:
            patient_ref = row["external_patient_reference"].strip()
            invoice_ref = row["external_invoice_reference"].strip()
            amount = Decimal(row["outstanding_amount"])
            reviewed = row["review_reference"].strip()
            description = row["description"].strip()
            department = row["department"].strip()
            invoice_date = date.fromisoformat(row["original_invoice_date"].strip())
            if not invoice_ref or not reviewed or not description or not department:
                raise ValueError("Invoice reference, review reference, description and department are required")
            if amount <= 0:
                raise ValueError("Outstanding amount must be greater than zero")
            if invoice_ref in seen:
                raise ValueError("External invoice reference is duplicated in the file")
            seen.add(invoice_ref)
            rows.append((index, {
                "external_patient_reference": patient_ref,
                "external_invoice_reference": invoice_ref,
                "original_invoice_date": invoice_date.isoformat(),
                "description": description,
                "department": department,
                "outstanding_amount": str(amount),
                "review_reference": reviewed,
            }))
        except Exception as exc:
            errors.append({"row": index, "code": row.get("external_invoice_reference", ""), "error": str(exc)})
    patient_refs = {row["external_patient_reference"] for _, row in rows}
    patients = set(Patient.objects.filter(external_reference__in=patient_refs).values_list("external_reference", flat=True))
    invoice_refs = {row["external_invoice_reference"] for _, row in rows}
    invoices = set(Invoice.objects.filter(external_reference__in=invoice_refs).values_list("external_reference", flat=True))
    for index, parsed in rows:
        if parsed["external_patient_reference"] not in patients:
            errors.append({"row": index, "code": parsed["external_patient_reference"], "error": "Patient does not exist"})
        if parsed["external_invoice_reference"] in invoices:
            errors.append({"row": index, "code": parsed["external_invoice_reference"], "error": "External invoice reference already exists"})
    return _finish_csv_validation(digest, rows, errors)


def _commit_import_job(job, actor):
    rows = job.report.get("rows", [])
    if job.kind == "products":
        for row in rows:
            item = CatalogueItem.objects.create(
                code=row["code"], name=row["name"], department=row["department"],
                kind=CatalogueItem.Kind.PRODUCT, base_unit=row["base_unit"],
                sale_unit=row["sale_unit"], units_per_sale_unit=Decimal(row["units_per_sale_unit"]),
                reorder_level=Decimal(row["reorder_level"]),
                prescription_required=row["prescription_required"],
            )
            PriceVersion.objects.create(
                item=item, amount=Decimal(row["sale_price"]),
                reason=f"Import {job.filename}", approved_by=actor,
            )
    elif job.kind == "patients":
        for row in rows:
            Patient.objects.create(
                external_reference=row["external_reference"],
                first_name=row["first_name"], last_name=row["last_name"],
                date_of_birth=date.fromisoformat(row["date_of_birth"]) if row["date_of_birth"] else None,
                estimated_age_years=row["estimated_age_years"], sex=row["sex"],
                phone=row["phone"], guardian_name=row["guardian_name"],
                guardian_phone=row["guardian_phone"], registered_by=actor,
                is_demo=settings.DEMO_MODE,
            )
    elif job.kind == "opening_stock":
        for index, row in enumerate(rows):
            item = CatalogueItem.objects.get(code=row["product_code"])
            batch = StockBatch.objects.create(
                item=item, batch_number=row["batch_number"],
                expiry_date=date.fromisoformat(row["expiry_date"]) if row["expiry_date"] else None,
                purchase_cost_per_base_unit=Decimal(row["purchase_cost_per_base_unit"]),
            )
            StockMovement.objects.create(
                batch=batch, movement_type=StockMovement.MovementType.RECEIPT,
                quantity_delta=Decimal(row["physical_count_base_units"]),
                to_location="Pharmacy", reference_type="OpeningCount",
                reference_id=row["count_reference"],
                reason=f"Witnessed by {row['witness_name']}",
                idempotency_key=f"import:{job.pk}:{index}", entered_by=actor,
            )
    elif job.kind == "opening_receivables":
        opening_item, _ = CatalogueItem.objects.get_or_create(
            code="OPENING-AR",
            defaults={
                "name": "Opening receivable", "kind": CatalogueItem.Kind.SERVICE,
                "department": "Finance", "active": True, "base_unit": "balance",
                "sale_unit": "balance", "units_per_sale_unit": Decimal("1"),
            },
        )
        for row in rows:
            patient = Patient.objects.get(external_reference=row["external_patient_reference"])
            invoice = Invoice.objects.create(
                patient=patient, status=Invoice.Status.POSTED, posted_at=timezone.now(),
                created_by=actor, external_reference=row["external_invoice_reference"],
                original_invoice_date=date.fromisoformat(row["original_invoice_date"]),
            )
            InvoiceLine.objects.create(
                invoice=invoice, item=opening_item,
                description=f"{row['description']} / Review {row['review_reference']}",
                department=row["department"], quantity=Decimal("1"),
                unit_price=Decimal(row["outstanding_amount"]),
            )
    else:
        raise ValidationError("Unsupported import type.")


def _check_import_job_current(job):
    """A dry run is a preview, not a reservation of database references."""
    rows = job.report.get("rows", [])
    if not rows:
        raise ValidationError("The dry run has no valid rows to commit.")

    errors = []
    if job.kind == "products":
        codes = [row["code"] for row in rows]
        existing = set(CatalogueItem.objects.filter(code__in=codes).values_list("code", flat=True))
        for number, code in enumerate(codes, start=2):
            if code in existing:
                errors.append(f"Row {number}: product code {code} now exists.")
    elif job.kind == "patients":
        references = [row["external_reference"] for row in rows]
        existing = set(Patient.objects.filter(external_reference__in=references).values_list("external_reference", flat=True))
        for number, reference in enumerate(references, start=2):
            if reference in existing:
                errors.append(f"Row {number}: patient reference {reference} now exists.")
    elif job.kind == "opening_stock":
        codes = {row["product_code"] for row in rows}
        items = dict(CatalogueItem.objects.filter(code__in=codes, kind=CatalogueItem.Kind.PRODUCT).values_list("code", "pk"))
        existing = set(StockBatch.objects.filter(
            item_id__in=items.values(), batch_number__in={row["batch_number"] for row in rows}
        ).values_list("item_id", "batch_number"))
        for number, row in enumerate(rows, start=2):
            item_id = items.get(row["product_code"])
            if item_id is None:
                errors.append(f"Row {number}: product {row['product_code']} is no longer available.")
            elif (item_id, row["batch_number"]) in existing:
                errors.append(f"Row {number}: batch {row['batch_number']} now exists for {row['product_code']}.")
    elif job.kind == "opening_receivables":
        patient_refs = {row["external_patient_reference"] for row in rows}
        existing_patients = set(Patient.objects.filter(
            external_reference__in=patient_refs
        ).values_list("external_reference", flat=True))
        invoice_refs = {row["external_invoice_reference"] for row in rows}
        existing_invoices = set(Invoice.objects.filter(
            external_reference__in=invoice_refs
        ).values_list("external_reference", flat=True))
        for number, row in enumerate(rows, start=2):
            if row["external_patient_reference"] not in existing_patients:
                errors.append(f"Row {number}: patient {row['external_patient_reference']} is no longer available.")
            if row["external_invoice_reference"] in existing_invoices:
                errors.append(f"Row {number}: invoice reference {row['external_invoice_reference']} now exists.")
    else:
        raise ValidationError("Unsupported import type.")

    if errors:
        remainder = f" {len(errors) - 5} more row(s) have conflicts." if len(errors) > 5 else ""
        raise ValidationError("The dry run is out of date. " + " ".join(errors[:5]) + remainder + " Upload again to review current data.")


@role_required(Role.OWNER, Role.PROCUREMENT)
def csv_import(request):
    form = CsvImportForm(request.POST or None, request.FILES or None)
    preview_job = None
    if request.method == "POST" and request.POST.get("commit_job"):
        try:
            with transaction.atomic():
                job = get_object_or_404(
                    ImportJob.objects.select_for_update(), pk=request.POST["commit_job"], created_by=request.user
                )
                if job.status == "committed":
                    messages.info(request, "This import was already committed; no duplicate records were created.")
                    return redirect("csv_import")
                if not job.dry_run or job.status != "validated" or job.error_count:
                    raise ValidationError("Only a successful dry run can be committed.")
                if job.created_at < timezone.now() - IMPORT_PREVIEW_MAX_AGE:
                    raise ValidationError("This dry run expired after 24 hours. Upload again to review current data.")
                if user_role(request.user) == Role.PROCUREMENT and job.kind not in {"products", "opening_stock"}:
                    raise ValidationError("Only the owner may import patient or receivable records.")
                _check_import_job_current(job)
                _commit_import_job(job, request.user)
                job.status = "committed"
                job.dry_run = False
                job.save(update_fields=["status", "dry_run", "updated_at"])
                audit(request.user, "import.committed", job, after={"rows": len(job.report.get("rows", []))}, request=request)
        except ValidationError as exc:
            messages.error(request, _validation_message(exc))
            return redirect("csv_import")
        except IntegrityError:
            messages.error(request, "Import data changed during commit. No rows were saved; upload again to review current data.")
            return redirect("csv_import")
        messages.success(request, f"Imported {job.row_count} {job.kind.replace('_', ' ')} row(s).")
        return redirect("csv_import")
    if request.method == "POST" and form.is_valid():
        try:
            kind = form.cleaned_data["import_kind"]
            if user_role(request.user) == Role.PROCUREMENT and kind not in {"products", "opening_stock"}:
                raise ValidationError("Only the owner may import patient or receivable records.")
            validators = {
                "products": _validate_product_csv,
                "patients": _validate_patient_csv,
                "opening_stock": _validate_opening_stock_csv,
                "opening_receivables": _validate_opening_receivables_csv,
            }
            digest, rows, errors = validators[kind](form.cleaned_data["csv_file"])
            preview_job = ImportJob.objects.create(
                idempotency_key=f"{kind}:{digest[:32]}:{uuid.uuid4().hex[:16]}",
                kind=kind, filename=form.cleaned_data["csv_file"].name,
                status="validated" if not errors else "failed", dry_run=True,
                row_count=len(rows), error_count=len(errors),
                report={"rows": rows if not errors else [], "errors": errors[:IMPORT_DISPLAY_ERRORS]},
                created_by=request.user,
            )
            audit(request.user, "import.dry_run", preview_job, after={"rows": len(rows), "errors": len(errors)}, request=request)
        except ValidationError as exc:
            form.add_error("csv_file", _validation_message(exc))
    recent = list(ImportJob.objects.filter(created_by=request.user).order_by("-created_at")[:10])
    cutoff = timezone.now() - IMPORT_PREVIEW_MAX_AGE
    for job in recent:
        job.can_commit = (
            job.status == "validated" and job.dry_run and not job.error_count and job.row_count
            and job.created_at >= cutoff
            and (user_role(request.user) != Role.PROCUREMENT or job.kind in {"products", "opening_stock"})
        )
    return render(request, "hospital/csv_import.html", {"form": form, "preview_job": preview_job, "recent": recent})
