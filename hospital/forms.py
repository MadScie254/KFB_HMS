from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import (
    CashShift,
    CatalogueItem,
    ClinicalNote,
    CreditNote,
    Encounter,
    Patient,
    Payment,
    PharmacyOrder,
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

