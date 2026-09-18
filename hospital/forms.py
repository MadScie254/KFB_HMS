from decimal import Decimal

from django import forms
from django.core.validators import FileExtensionValidator
from django.forms import formset_factory

from .models import (
    Admission,
    Bed,
    CashShift,
    CatalogueItem,
    ClinicalAttachment,
    ClinicalNote,
    CreditNote,
    Encounter,
    Patient,
    Payment,
    PurchaseOrder,
    ServiceOrder,
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
