from decimal import Decimal

from django import forms
from django.core.validators import FileExtensionValidator
from django.forms import formset_factory
from django.utils import timezone

from .models import (
    Admission,
    Bed,
    CashShift,
    CatalogueItem,
    ClinicalAttachment,
    ClinicalNote,
    CreditNote,
    DepartmentIssue,
    Encounter,
    Patient,
    Payment,
    PurchaseOrder,
    PurchaseOrderLine,
    ServiceOrder,
    StockBatch,
    StockWriteOff,
    Supplier,
)


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "field-control")


class PatientForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Patient
        fields = ["first_name", "last_name", "date_of_birth", "estimated_age_years", "sex", "phone", "id_number", "guardian_name", "guardian_phone", "allergy_status", "allergy_details"]
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"}), "allergy_details": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        if not data.get("date_of_birth") and data.get("estimated_age_years") is None:
            self.add_error("estimated_age_years", "Record either a date of birth or an estimated age.")
        return data


class EncounterForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Encounter
        fields = ["department", "urgency", "emergency_override_reason"]
        widgets = {"emergency_override_reason": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        if data.get("urgency") == "emergency" and not data.get("emergency_override_reason"):
            self.add_error("emergency_override_reason", "Record why emergency care is proceeding outside the routine flow.")
        return data


class ClinicalNoteForm(StyledFormMixin, forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = ClinicalNote
        fields = ["complaints", "history", "examination", "assessment", "plan", "follow_up"]
        widgets = {name: forms.Textarea(attrs={"rows": 3}) for name in fields}


class PharmacyBasketForm(StyledFormMixin, forms.Form):
    customer_name = forms.CharField(max_length=160, required=False)
    patient_number = forms.CharField(max_length=20, required=False, help_text="Optional for walk-in retail customers")
    product = forms.ModelChoiceField(queryset=CatalogueItem.objects.none())
    quantity = forms.DecimalField(min_value=Decimal("0.001"), decimal_places=3, max_digits=14, help_text="Quantity in the product base unit")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = CatalogueItem.objects.filter(kind=CatalogueItem.Kind.PRODUCT, active=True).order_by("name")

    def clean(self):
        data = super().clean()
        if not data.get("customer_name") and not data.get("patient_number"):
            self.add_error("customer_name", "Record a customer name or patient number.")
        product = data.get("product")
        if product and product.prescription_required:
            self.add_error("product", "This demonstration basket cannot bypass the configured prescription requirement.")
        return data


class PaymentForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, max_digits=14)
    method = forms.ChoiceField(choices=Payment.Method.choices)
    reference = forms.CharField(max_length=80, required=False, help_text="Required for M-PESA; hospital record only until verified")

    def clean(self):
        data = super().clean()
        if data.get("method") == Payment.Method.MPESA and not data.get("reference"):
            self.add_error("reference", "Enter the M-PESA reference.")
        return data


class ShiftOpenForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CashShift
        fields = ["label", "opening_float"]


class ShiftCloseForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CashShift
        fields = ["actual_cash", "transfers_in", "transfers_out", "cash_refunds", "variance_reason"]
        widgets = {"variance_reason": forms.Textarea(attrs={"rows": 2})}


class CreditNoteForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CreditNote
        fields = ["amount", "reason"]
        widgets = {"reason": forms.Textarea(attrs={"rows": 3})}


class ServiceOrderForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ServiceOrder
        fields = ["service", "specimen_details"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = CatalogueItem.objects.filter(kind=CatalogueItem.Kind.SERVICE, active=True).order_by("department", "name")


class ServiceResultForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ServiceOrder
        fields = ["status", "result"]
        widgets = {"result": forms.Textarea(attrs={"rows": 7})}

    def clean(self):
        data = super().clean()
        if data.get("status") in {ServiceOrder.Status.REVIEW, ServiceOrder.Status.RELEASED} and not data.get("result", "").strip():
            self.add_error("result", "Enter a result before review or release.")
        return data


class AdmissionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Admission
        fields = ["bed"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        occupied = Admission.objects.filter(discharged_at__isnull=True).values_list("bed_id", flat=True)
        self.fields["bed"].queryset = Bed.objects.filter(active=True).exclude(pk__in=occupied).select_related("ward")


class PurchaseOrderForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "reference", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(active=True).order_by("name")


class PurchaseOrderLineForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=CatalogueItem.objects.none())
    quantity_base_units = forms.DecimalField(
        min_value=Decimal("0.001"), max_digits=14, decimal_places=3, label="Quantity"
    )
    quoted_unit_cost = forms.DecimalField(
        min_value=Decimal("0.00"), max_digits=14, decimal_places=2, label="Quoted unit cost"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = CatalogueItem.objects.filter(
            kind=CatalogueItem.Kind.PRODUCT, active=True
        ).order_by("name")


PurchaseOrderLineFormSet = formset_factory(
    PurchaseOrderLineForm, extra=2, min_num=1, validate_min=True, can_delete=True
)


class CsvImportForm(StyledFormMixin, forms.Form):
    import_kind = forms.ChoiceField(choices=[
        ("products", "Products and prices"),
        ("patients", "Patients"),
        ("opening_stock", "Opening stock counts"),
        ("opening_receivables", "Opening receivables"),
    ])
    csv_file = forms.FileField(help_text="UTF-8 CSV, maximum 1 MB. First row must contain column names.")

    def clean_csv_file(self):
        uploaded = self.cleaned_data["csv_file"]
        if uploaded.size > 1024 * 1024:
            raise forms.ValidationError("File exceeds the 1 MB limit.")
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file.")
        return uploaded


class PrescriptionForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=CatalogueItem.objects.none())
    strength = forms.CharField(max_length=80, required=False)
    dose = forms.CharField(max_length=80)
    route = forms.CharField(max_length=80)
    frequency = forms.CharField(max_length=80)
    duration = forms.CharField(max_length=80)
    quantity_base_units = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, label="Total quantity")
    instructions = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = CatalogueItem.objects.filter(kind=CatalogueItem.Kind.PRODUCT, active=True).order_by("name")


PrescriptionFormSet = formset_factory(
    PrescriptionForm, extra=2, min_num=1, validate_min=True, can_delete=True
)


class ClinicalAttachmentForm(StyledFormMixin, forms.ModelForm):
    file = forms.FileField(
        validators=[FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
        help_text="PDF, JPG or PNG. Maximum 10 MB.",
    )

    class Meta:
        model = ClinicalAttachment
        fields = ["file", "description"]

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File exceeds the 10 MB limit.")
        allowed_types = {"application/pdf", "image/jpeg", "image/png"}
        if uploaded.content_type not in allowed_types:
            raise forms.ValidationError("Upload a PDF, JPG or PNG file.")
        return uploaded


class GoodsReceiptForm(StyledFormMixin, forms.Form):
    """Delivery header: the supplier's own document, photographed at the counter."""

    supplier_invoice_reference = forms.CharField(
        max_length=100,
        label="Supplier invoice / delivery note number",
        help_text="As printed on the document. One invoice cannot be received twice against the same order.",
    )
    invoice_amount = forms.DecimalField(
        min_value=Decimal("0.00"), max_digits=14, decimal_places=2,
        label="Invoice total (KES)",
        help_text="The amount the supplier is billing. A difference against the goods counted in is flagged for review.",
    )
    invoice_date = forms.DateField(
        required=False, label="Invoice date", widget=forms.DateInput(attrs={"type": "date"})
    )
    delivered_on = forms.DateField(
        label="Delivered on", widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The day the goods physically arrived, which may differ from today.",
    )
    invoice_photo = forms.FileField(
        label="Photograph of the supplier invoice",
        validators=[FileExtensionValidator(["jpg", "jpeg", "png", "pdf", "heic"])],
        help_text="Required. Photograph or scan the invoice/delivery note. JPG, PNG, HEIC or PDF, maximum 10 MB.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["delivered_on"].initial = timezone.localdate()

    def clean_invoice_photo(self):
        uploaded = self.cleaned_data["invoice_photo"]
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File exceeds the 10 MB limit. Photograph the invoice at a lower resolution.")
        allowed_types = {"image/jpeg", "image/png", "image/heic", "image/heif", "application/pdf"}
        if uploaded.content_type not in allowed_types:
            raise forms.ValidationError("Upload a photograph (JPG, PNG or HEIC) or a PDF scan.")
        return uploaded

    def clean_delivered_on(self):
        delivered = self.cleaned_data["delivered_on"]
        if delivered > timezone.localdate():
            raise forms.ValidationError("A delivery cannot be recorded as arriving in the future.")
        return delivered

    def clean(self):
        data = super().clean()
        invoice_date = data.get("invoice_date")
        if invoice_date and invoice_date > timezone.localdate():
            self.add_error("invoice_date", "The invoice date cannot be in the future.")
        return data


class GoodsReceiptLineForm(StyledFormMixin, forms.Form):
    """One delivered batch. Quantities are in the product base unit."""

    order_line = forms.ModelChoiceField(queryset=PurchaseOrderLine.objects.none(), label="Ordered item")
    quantity_received = forms.DecimalField(
        min_value=Decimal("0.001"), max_digits=14, decimal_places=3, label="Quantity received",
    )
    batch_number = forms.CharField(max_length=80, label="Batch number")
    expiry_date = forms.DateField(required=False, label="Expiry date", widget=forms.DateInput(attrs={"type": "date"}))
    actual_unit_cost = forms.DecimalField(
        min_value=Decimal("0.00"), max_digits=14, decimal_places=2, label="Actual unit cost",
    )

    def __init__(self, *args, order=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = PurchaseOrderLine.objects.none() if order is None else PurchaseOrderLine.objects.filter(order=order).select_related("item")
        self.fields["order_line"].queryset = queryset
        self.fields["order_line"].label_from_instance = lambda line: (
            f"{line.item.name} — ordered {line.quantity_base_units} {line.item.base_unit or 'units'}"
        )

    def clean_expiry_date(self):
        expiry = self.cleaned_data.get("expiry_date")
        if expiry and expiry < timezone.localdate():
            raise forms.ValidationError("This batch has already expired and cannot be received as sellable stock.")
        return expiry


class BaseGoodsReceiptLineFormSet(forms.BaseFormSet):
    """Passes the order down so each row can only choose that order's lines."""

    def __init__(self, *args, order=None, **kwargs):
        self.order = order
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["order"] = self.order
        return kwargs


GoodsReceiptLineFormSet = formset_factory(
    GoodsReceiptLineForm, formset=BaseGoodsReceiptLineFormSet, extra=1, min_num=1, validate_min=True, can_delete=True
)


class DeliveryCheckForm(StyledFormMixin, forms.Form):
    """Independent confirmation that the delivery matches its invoice."""

    discrepancy_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="What you found",
        help_text="Record anything that did not match: short counts, damage, different batches, a different price.",
    )


class StockCountOpenForm(StyledFormMixin, forms.Form):
    location = forms.CharField(max_length=80, initial="Pharmacy", label="Counting location")
    blind_count = forms.BooleanField(
        required=False, initial=True, label="Blind count",
        help_text="Hide expected quantities while counting so the shelf is counted, not confirmed.",
    )
    notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes",
        help_text="Why this count is being taken, and who is witnessing it.",
    )


class StockCountReviewForm(StyledFormMixin, forms.Form):
    review_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Review notes",
        help_text="Approving posts an adjustment movement for every variance. Nothing is edited in place.",
    )


class DepartmentIssueForm(StyledFormMixin, forms.Form):
    """Who is taking custody of stock leaving the pharmacy."""

    department = forms.CharField(max_length=80, label="Department or ward")
    received_by_name = forms.CharField(
        max_length=160, label="Received by",
        help_text="The named person taking custody. Not a role; a person.",
    )
    kind = forms.ChoiceField(choices=DepartmentIssue.Kind.choices, label="Issue type", initial=DepartmentIssue.Kind.GENERAL)
    patient_number = forms.CharField(
        max_length=20, required=False, label="Patient number",
        help_text="Required for a patient-specific issue.",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes")

    def clean(self):
        data = super().clean()
        if data.get("kind") == DepartmentIssue.Kind.PATIENT and not data.get("patient_number"):
            self.add_error("patient_number", "A patient-specific issue must name the patient it is for.")
        return data


class DepartmentIssueLineForm(StyledFormMixin, forms.Form):
    batch = forms.ModelChoiceField(queryset=StockBatch.objects.none(), label="Batch")
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, label="Quantity")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = (
            StockBatch.objects.filter(status=StockBatch.Status.ACTIVE)
            .select_related("item").order_by("item__name", "expiry_date")
        )
        self.fields["batch"].label_from_instance = lambda batch: (
            f"{batch.item.name} — batch {batch.batch_number}"
            + (f", expires {batch.expiry_date:%b %Y}" if batch.expiry_date else "")
        )


DepartmentIssueLineFormSet = formset_factory(
    DepartmentIssueLineForm, extra=2, min_num=1, validate_min=True, can_delete=True
)


class WriteOffRequestForm(StyledFormMixin, forms.Form):
    """Proposing that stock leave the balance, with a reason someone can review."""

    batch = forms.ModelChoiceField(queryset=StockBatch.objects.none(), label="Batch")
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=14, decimal_places=3, label="Quantity")
    reason = forms.ChoiceField(choices=StockWriteOff.Reason.choices, label="Reason")
    narrative = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), label="What happened",
        help_text="A reviewer who was not there has to be able to judge this.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = StockBatch.objects.select_related("item").order_by("item__name", "expiry_date")
        self.fields["batch"].label_from_instance = lambda batch: (
            f"{batch.item.name} — batch {batch.batch_number}"
            + (f", expires {batch.expiry_date:%b %Y}" if batch.expiry_date else "")
            + (" (expired)" if batch.is_expired else "")
        )


class WriteOffReviewForm(StyledFormMixin, forms.Form):
    review_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Review notes",
        help_text="Approving posts an adjustment movement that removes the stock. Rejecting changes no balance.",
    )


class BatchDispositionForm(StyledFormMixin, forms.Form):
    status = forms.ChoiceField(
        choices=[
            (StockBatch.Status.ACTIVE, "Release back to sellable stock"),
            (StockBatch.Status.QUARANTINE, "Hold in quarantine"),
        ],
        label="Disposition",
    )
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), label="Why this was authorised")
