import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

MONEY = {"max_digits": 14, "decimal_places": 2, "default": Decimal("0.00")}
QUANTITY = {"max_digits": 14, "decimal_places": 3, "default": Decimal("0.000")}


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    RECEPTION = "reception", "Reception / cashier"
    CLINICIAN = "clinician", "Clinician"
    NURSE = "nurse", "Nurse"
    PHARMACY = "pharmacy", "Pharmacy"
    LAB = "lab", "Laboratory / imaging"
    EYE = "eye", "Eye clinic"
    PROCUREMENT = "procurement", "Administrator / procurement"
    REVIEWER = "reviewer", "Delegated reviewer"


class StaffProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="staff_profile")
    role = models.CharField(max_length=24, choices=Role.choices, default=Role.RECEPTION)
    display_name = models.CharField(max_length=120, blank=True)
    require_password_change = models.BooleanField(default=False)
    second_factor_required = models.BooleanField(default=False)
    active_shift_label = models.CharField(max_length=40, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.display_name or self.user.get_full_name() or self.user.username


class Setting(TimeStampedModel):
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    description = models.CharField(max_length=255, blank=True)
    production_confirmed = models.BooleanField(default=False)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)

    def __str__(self):
        return self.key


class Patient(TimeStampedModel):
    class Sex(models.TextChoices):
        FEMALE = "F", "Female"
        MALE = "M", "Male"
        OTHER = "O", "Other / not recorded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient_number = models.CharField(max_length=20, unique=True, blank=True)
    external_reference = models.CharField(max_length=80, blank=True, db_index=True)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    date_of_birth = models.DateField(null=True, blank=True)
    estimated_age_years = models.PositiveSmallIntegerField(null=True, blank=True)
    sex = models.CharField(max_length=1, choices=Sex.choices, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    id_number = models.CharField(max_length=40, blank=True)
    guardian_name = models.CharField(max_length=160, blank=True)
    guardian_phone = models.CharField(max_length=30, blank=True)
    allergy_status = models.CharField(max_length=20, default="unknown", choices=[("unknown", "Unknown"), ("none", "No known allergies"), ("recorded", "Allergy recorded")])
    allergy_details = models.TextField(blank=True)
    is_demo = models.BooleanField(default=False)
    registered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="patients_registered")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["last_name", "first_name"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["id_number"]),
        ]

    def save(self, *args, **kwargs):
        if not self.patient_number:
            # UUID-derived human identifier avoids unsafe MAX()+1 races.
            self.patient_number = f"KFB-{timezone.localdate():%y}-{uuid.uuid4().hex[:7].upper()}"
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.patient_number} · {self.full_name}"


class Encounter(TimeStampedModel):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Registered"
        TRIAGE = "triage", "Waiting for triage"
        CLINICIAN = "clinician", "Waiting for clinician"
        TESTS = "tests", "Tests / imaging"
        PHARMACY = "pharmacy", "Cashier / pharmacy"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter_number = models.CharField(max_length=24, unique=True, blank=True)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="encounters")
    department = models.CharField(max_length=40, default="Outpatient")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REGISTERED)
    urgency = models.CharField(max_length=20, default="routine", choices=[("routine", "Routine"), ("urgent", "Urgent"), ("emergency", "Emergency")])
    emergency_override_reason = models.TextField(blank=True)
    started_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="encounters_started")
    assigned_clinician = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_encounters")
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.encounter_number:
            self.encounter_number = f"ENC-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.encounter_number


class Observation(TimeStampedModel):
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="observations")
    observed_at = models.DateTimeField(default=timezone.now)
    temperature_c = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    pulse_bpm = models.PositiveSmallIntegerField(null=True, blank=True)
    respiratory_rate = models.PositiveSmallIntegerField(null=True, blank=True)
    systolic_bp = models.PositiveSmallIntegerField(null=True, blank=True)
    diastolic_bp = models.PositiveSmallIntegerField(null=True, blank=True)
    oxygen_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT)


class ClinicalNote(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SIGNED = "signed", "Signed"
        AMENDED = "amended", "Amended"

    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="clinical_notes")
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="clinical_notes")
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    complaints = models.TextField(blank=True)
    history = models.TextField(blank=True)
    examination = models.TextField(blank=True)
    assessment = models.TextField(blank=True)
    plan = models.TextField(blank=True)
    follow_up = models.TextField(blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    parent_note = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="amendments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["encounter", "author", "version"], name="unique_note_version")]

    def sign(self):
        if self.status != self.Status.DRAFT:
            raise ValidationError("Only a draft note can be signed.")
        self.status = self.Status.SIGNED
        self.signed_at = timezone.now()
        self.save(update_fields=["status", "signed_at", "updated_at"])


class CatalogueItem(TimeStampedModel):
    class Kind(models.TextChoices):
        SERVICE = "service", "Service"
        PRODUCT = "product", "Medicine / product"

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    department = models.CharField(max_length=40)
    active = models.BooleanField(default=True)
    prescription_required = models.BooleanField(default=False)
    base_unit = models.CharField(max_length=30, blank=True)
    sale_unit = models.CharField(max_length=30, blank=True)
    units_per_sale_unit = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    reorder_level = models.DecimalField(**QUANTITY)
    provisional = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} · {self.name}"


class PriceVersion(models.Model):
    item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT, related_name="prices")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    effective_from = models.DateTimeField(default=timezone.now)
    effective_to = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=255)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from"]


class StockBatch(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        QUARANTINE = "quarantine", "Quarantined"
        EXPIRED = "expired", "Expired"

    item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT, related_name="batches", limit_choices_to={"kind": CatalogueItem.Kind.PRODUCT})
    batch_number = models.CharField(max_length=80)
    expiry_date = models.DateField(null=True, blank=True)
    purchase_cost_per_base_unit = models.DecimalField(**MONEY)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["item", "batch_number"], name="unique_item_batch")]

    @property
    def quantity_on_hand(self):
        return self.movements.aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")

    @property
    def can_dispense(self):
        return self.status == self.Status.ACTIVE and (not self.expiry_date or self.expiry_date >= timezone.localdate())

    @property
    def is_expired(self):
        return bool(self.expiry_date) and self.expiry_date < timezone.localdate()

    def days_to_expiry(self):
        if not self.expiry_date:
            return None
        return (self.expiry_date - timezone.localdate()).days

    def balance_at(self, cutoff):
        """Ledger balance as at a cutoff, by actual event time.

        A count sheet frozen at 14:00 must be compared with the stock the
        ledger says was there at 14:00, not with what it says now.
        """
        return self.movements.filter(event_at__lte=cutoff).aggregate(total=Sum("quantity_delta"))["total"] or Decimal("0.000")

    def __str__(self):
        return f"{self.item.name} · {self.batch_number}"


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        RECEIPT = "receipt", "Receipt"
        DISPENSE = "dispense", "Dispense"
        TRANSFER = "transfer", "Custody transfer"
        RETURN = "return", "Return"
        ADJUSTMENT = "adjustment", "Approved adjustment"
        CONSUMPTION = "consumption", "Department consumption"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=16, choices=MovementType.choices)
    quantity_delta = models.DecimalField(max_digits=14, decimal_places=3)
    from_location = models.CharField(max_length=80, blank=True)
    to_location = models.CharField(max_length=80, blank=True)
    reference_type = models.CharField(max_length=40)
    reference_id = models.CharField(max_length=80)
    reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True)
    event_at = models.DateTimeField(default=timezone.now)
    entered_at = models.DateTimeField(auto_now_add=True)
    entered_by = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        ordering = ["-entered_at"]


class Invoice(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        PART_PAID = "part_paid", "Part paid"
        PAID = "paid", "Paid"
        CREDITED = "credited", "Credited"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=30, unique=True, blank=True)
    external_reference = models.CharField(max_length=100, blank=True, db_index=True)
    original_invoice_date = models.DateField(null=True, blank=True)
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    encounter = models.ForeignKey(Encounter, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    customer_name = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = f"INV-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def total(self):
        return self.lines.aggregate(total=Sum("line_total"))["total"] or Decimal("0.00")

    @property
    def paid_amount(self):
        return self.allocations.filter(payment__status=Payment.Status.VALID).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    @property
    def balance(self):
        credits = self.credit_notes.filter(status=CreditNote.Status.APPROVED).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return self.total - credits - self.paid_amount

    def refresh_status(self):
        if self.status == self.Status.DRAFT:
            return
        balance = self.balance
        self.status = self.Status.PAID if balance <= 0 else (self.Status.PART_PAID if self.paid_amount > 0 else self.Status.POSTED)
        self.save(update_fields=["status", "updated_at"])


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT)
    description = models.CharField(max_length=200)
    department = models.CharField(max_length=40)
    quantity = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.00"))])
    line_total = models.DecimalField(**MONEY)
    price_version = models.ForeignKey(PriceVersion, null=True, blank=True, on_delete=models.PROTECT)
    package_included = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.line_total = (Decimal(str(self.quantity)) * Decimal(str(self.unit_price))).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)


class Payment(TimeStampedModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        MPESA = "mpesa", "M-PESA"
    class Verification(models.TextChoices):
        NOT_APPLICABLE = "n/a", "Not applicable"
        UNVERIFIED = "unverified", "Recorded — unverified"
        MANUAL = "manual", "Manually verified"
        PROVIDER = "provider", "Provider confirmed"
    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        REVERSED = "reversed", "Reversed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt_number = models.CharField(max_length=30, unique=True, blank=True)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=10, choices=Method.choices)
    reference = models.CharField(max_length=80, blank=True)
    verification_status = models.CharField(max_length=16, choices=Verification.choices, default=Verification.NOT_APPLICABLE)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.VALID)
    shift = models.ForeignKey("CashShift", null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    received_by = models.ForeignKey(User, on_delete=models.PROTECT)
    received_at = models.DateTimeField(default=timezone.now)
    idempotency_key = models.CharField(max_length=100, unique=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["method", "reference"], condition=~Q(reference=""), name="unique_payment_reference"),
        ]

    def save(self, *args, **kwargs):
        if not self.receipt_number:
            self.receipt_number = f"RCT-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        if self.method == self.Method.CASH:
            self.verification_status = self.Verification.NOT_APPLICABLE
        elif not self.reference:
            raise ValidationError("An M-PESA reference is required.")
        super().save(*args, **kwargs)

    @property
    def allocated_amount(self):
        return self.allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    @property
    def unapplied_amount(self):
        return self.amount - self.allocated_amount


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    created_at = models.DateTimeField(auto_now_add=True)
    allocated_by = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["payment", "invoice"], name="unique_payment_invoice_allocation")]


class CreditNote(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_notes")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    reason = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="credit_notes_requested")
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="credit_notes_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)


class CashShift(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed — awaiting review"
        REVIEWED = "reviewed", "Reviewed"
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name="cash_shifts")
    label = models.CharField(max_length=40)
    opened_at = models.DateTimeField(default=timezone.now)
    opening_float = models.DecimalField(**MONEY)
    closed_at = models.DateTimeField(null=True, blank=True)
    actual_cash = models.DecimalField(**MONEY)
    transfers_in = models.DecimalField(**MONEY)
    transfers_out = models.DecimalField(**MONEY)
    cash_refunds = models.DecimalField(**MONEY)
    variance_reason = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    reviewer = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="shifts_reviewed")

    @property
    def cash_receipts(self):
        return self.payments.filter(method=Payment.Method.CASH, status=Payment.Status.VALID).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    @property
    def expected_cash(self):
        return self.opening_float + self.cash_receipts + self.transfers_in - self.cash_refunds - self.transfers_out

    @property
    def variance(self):
        return self.actual_cash - self.expected_cash if self.closed_at else Decimal("0.00")


class Prescription(TimeStampedModel):
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="prescriptions")
    prescriber = models.ForeignKey(User, on_delete=models.PROTECT)
    status = models.CharField(max_length=14, choices=[("active", "Active"), ("cancelled", "Cancelled"), ("completed", "Completed")], default="active")
    signed_at = models.DateTimeField(null=True, blank=True)


class PrescriptionItem(models.Model):
    prescription = models.ForeignKey(Prescription, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT, limit_choices_to={"kind": CatalogueItem.Kind.PRODUCT})
    strength = models.CharField(max_length=80, blank=True)
    dose = models.CharField(max_length=80)
    route = models.CharField(max_length=80)
    frequency = models.CharField(max_length=80)
    duration = models.CharField(max_length=80)
    quantity_base_units = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    instructions = models.TextField(blank=True)
    dispensed_quantity = models.DecimalField(**QUANTITY)


class PharmacyOrder(TimeStampedModel):
    class Status(models.TextChoices):
        PREPARED = "prepared", "Prepared — payment required"
        CLEARED = "cleared", "Payment cleared"
        DISPENSED = "dispensed", "Dispensed"
        CANCELLED = "cancelled", "Cancelled"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.CharField(max_length=30, unique=True, blank=True)
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.PROTECT)
    customer_name = models.CharField(max_length=160, blank=True)
    encounter = models.ForeignKey(Encounter, null=True, blank=True, on_delete=models.PROTECT)
    prescription = models.ForeignKey(Prescription, null=True, blank=True, on_delete=models.PROTECT)
    invoice = models.OneToOneField(Invoice, null=True, blank=True, on_delete=models.PROTECT, related_name="pharmacy_order")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PREPARED)
    prepared_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="orders_prepared")
    dispensed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="orders_dispensed")
    dispensed_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = f"RX-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)


class PharmacyOrderItem(models.Model):
    order = models.ForeignKey(PharmacyOrder, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT)
    quantity_base_units = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.00"))])
    prescription_item = models.ForeignKey(PrescriptionItem, null=True, blank=True, on_delete=models.PROTECT)

    @property
    def line_total(self):
        return (Decimal(str(self.quantity_base_units)) * Decimal(str(self.unit_price))).quantize(Decimal("0.01"))


class Ward(TimeStampedModel):
    name = models.CharField(max_length=80, unique=True)
    active = models.BooleanField(default=True)


class Bed(TimeStampedModel):
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="beds")
    label = models.CharField(max_length=40)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ward", "label"], name="unique_ward_bed")]


class Admission(TimeStampedModel):
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="admissions")
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="admissions")
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="admissions")
    admitted_at = models.DateTimeField(default=timezone.now)
    discharged_at = models.DateTimeField(null=True, blank=True)
    clinical_status = models.CharField(max_length=18, default="admitted", choices=[("admitted", "Admitted"), ("discharged", "Clinically discharged")])
    financial_status = models.CharField(max_length=18, default="open", choices=[("open", "Balance open"), ("settled", "Settled")])
    admitted_by = models.ForeignKey(User, on_delete=models.PROTECT)
    discharge_summary = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bed"], condition=Q(discharged_at__isnull=True), name="one_active_admission_per_bed")]


class MedicationAdministration(TimeStampedModel):
    admission = models.ForeignKey(Admission, on_delete=models.PROTECT, related_name="administrations")
    prescription_item = models.ForeignKey(PrescriptionItem, on_delete=models.PROTECT)
    status = models.CharField(max_length=14, choices=[("given", "Given"), ("not_given", "Not given"), ("withheld", "Withheld")])
    dose_given = models.CharField(max_length=80, blank=True)
    administered_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField(blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT)


class NursingHandover(TimeStampedModel):
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="handovers")
    from_shift = models.CharField(max_length=40)
    to_shift = models.CharField(max_length=40)
    summary = models.TextField()
    authored_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="handovers_authored")
    accepted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="handovers_accepted")
    accepted_at = models.DateTimeField(null=True, blank=True)


class BedTransfer(TimeStampedModel):
    admission = models.ForeignKey(Admission, on_delete=models.PROTECT, related_name="transfers")
    from_bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="transfers_from")
    to_bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="transfers_to")
    transferred_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=255)
    transferred_by = models.ForeignKey(User, on_delete=models.PROTECT)


class ServiceOrder(TimeStampedModel):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        IN_PROGRESS = "in_progress", "In progress"
        REVIEW = "review", "Awaiting review"
        RELEASED = "released", "Released"
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="service_orders")
    service = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT, limit_choices_to={"kind": CatalogueItem.Kind.SERVICE})
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="service_orders_requested")
    performer = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="service_orders_performed")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REQUESTED)
    specimen_details = models.CharField(max_length=255, blank=True)
    result = models.TextField(blank=True)
    released_at = models.DateTimeField(null=True, blank=True)


class EyeSession(TimeStampedModel):
    session_date = models.DateField()
    visiting_doctor = models.CharField(max_length=160)
    theatre = models.CharField(max_length=80)
    status = models.CharField(max_length=14, choices=[("proposed", "Proposed"), ("confirmed", "Confirmed"), ("completed", "Completed"), ("cancelled", "Cancelled")], default="proposed")


class EyeCase(TimeStampedModel):
    class Eye(models.TextChoices):
        RIGHT = "right", "Right"
        LEFT = "left", "Left"
        BOTH = "both", "Both"
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="eye_cases")
    session = models.ForeignKey(EyeSession, null=True, blank=True, on_delete=models.PROTECT, related_name="cases")
    proposed_procedure = models.CharField(max_length=160)
    eye = models.CharField(max_length=8, choices=Eye.choices)
    readiness = models.CharField(max_length=18, choices=[("assessment", "Assessment pending"), ("ready", "Clinically ready"), ("not_ready", "Not ready")], default="assessment")
    status = models.CharField(max_length=14, choices=[("waiting", "Waiting"), ("confirmed", "Confirmed"), ("completed", "Completed"), ("cancelled", "Cancelled")], default="waiting")
    package_price = models.DecimalField(**MONEY)
    payment_status = models.CharField(max_length=14, default="unpaid", choices=[("unpaid", "Unpaid"), ("partial", "Part paid"), ("paid", "Paid")])
    completed_at = models.DateTimeField(null=True, blank=True)


class ClinicianPayable(TimeStampedModel):
    eye_case = models.OneToOneField(EyeCase, on_delete=models.PROTECT, related_name="payable")
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=14, choices=[("accrued", "Accrued"), ("approved", "Approved"), ("paid", "Paid")], default="accrued")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)


class EyePackageItem(TimeStampedModel):
    package_code = models.CharField(max_length=40)
    item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT)
    quantity = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["package_code", "item"], name="unique_eye_package_item")]


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=160, unique=True)
    phone = models.CharField(max_length=30, blank=True)
    payment_details = models.TextField(blank=True)
    active = models.BooleanField(default=True)


class PurchaseOrder(TimeStampedModel):
    order_number = models.CharField(max_length=30, unique=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    status = models.CharField(max_length=18, choices=[("requested", "Requested"), ("approved", "Approved"), ("part_received", "Part received"), ("received", "Received"), ("cancelled", "Cancelled")], default="requested")
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="purchase_orders_requested")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_orders_approved")
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = f"PO-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        if self.approved_by_id and self.approved_by_id == self.requested_by_id:
            raise ValidationError("The requester cannot approve their own purchase order.")
        super().save(*args, **kwargs)


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT, limit_choices_to={"kind": CatalogueItem.Kind.PRODUCT})
    quantity_base_units = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    quoted_unit_cost = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.00"))])

    @property
    def line_total(self):
        return (self.quantity_base_units * self.quoted_unit_cost).quantize(Decimal("0.01"))


class GoodsReceipt(TimeStampedModel):
    """A delivery actually received against a purchase order.

    The supplier's invoice or delivery note is photographed at the counter and
    stored with the receipt. Stock balances against a document the hospital
    holds, not against a typed reference only the receiver ever saw.
    """

    receipt_number = models.CharField(max_length=30, unique=True, blank=True)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    supplier_invoice_reference = models.CharField(max_length=100)
    invoice_photo = models.FileField(upload_to="supplier_invoices/%Y/%m/", blank=True)
    invoice_photo_name = models.CharField(max_length=255, blank=True)
    invoice_amount = models.DecimalField(**MONEY)
    invoice_date = models.DateField(null=True, blank=True)
    delivered_at = models.DateTimeField(default=timezone.now)
    posted_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="goods_received")
    checked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="goods_checked")
    checked_at = models.DateTimeField(null=True, blank=True)
    discrepancy_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-delivered_at"]
        constraints = [models.UniqueConstraint(fields=["purchase_order", "supplier_invoice_reference"], name="unique_supplier_invoice_per_order")]

    def save(self, *args, **kwargs):
        if not self.receipt_number:
            self.receipt_number = f"GRN-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    def clean(self):
        if self.checked_by_id and self.checked_by_id == self.received_by_id:
            raise ValidationError("The delivery checker must differ from the receiver.")

    @property
    def received_value(self):
        """What the delivered quantities cost at the unit costs entered."""
        return sum(
            (line.line_cost for line in self.lines.all()),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"))

    @property
    def invoice_variance(self):
        """Supplier invoice total minus the value of what was physically counted in."""
        return (self.invoice_amount - self.received_value).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.receipt_number} · {self.supplier_invoice_reference}"


class GoodsReceiptLine(models.Model):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name="lines")
    order_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    batch = models.ForeignKey(StockBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="receipt_lines")
    quantity_received = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    batch_number = models.CharField(max_length=80)
    expiry_date = models.DateField(null=True, blank=True)
    actual_unit_cost = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.00"))])

    @property
    def line_cost(self):
        return (Decimal(str(self.quantity_received)) * Decimal(str(self.actual_unit_cost))).quantize(Decimal("0.01"))

    @property
    def cost_variance(self):
        """Actual unit cost minus the quoted unit cost on the approved order."""
        return (Decimal(str(self.actual_unit_cost)) - Decimal(str(self.order_line.quoted_unit_cost))).quantize(Decimal("0.01"))


class DepartmentIssue(TimeStampedModel):
    """Stock handed from pharmacy into a named department's custody.

    Issuing moves custody; it does not consume. The quantity stays visible as
    unconsumed departmental stock until it is administered, consumed, returned
    or written off, so medicine that left the pharmacy and never reached a
    patient is a number somebody can see rather than an absence nobody notices.
    """

    class Kind(models.TextChoices):
        PATIENT = "patient", "Patient-specific"
        GENERAL = "general", "General consumable"

    class Status(models.TextChoices):
        OUTSTANDING = "outstanding", "In departmental custody"
        SETTLED = "settled", "Fully accounted for"

    reference = models.CharField(max_length=30, unique=True, blank=True)
    department = models.CharField(max_length=80)
    received_by_name = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.GENERAL)
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_issues")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OUTSTANDING)
    notes = models.TextField(blank=True)
    issued_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="stock_issues")
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-issued_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"ISS-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    def clean(self):
        if self.kind == self.Kind.PATIENT and not self.patient_id:
            raise ValidationError("A patient-specific issue must name the patient it is for.")

    @property
    def outstanding_quantity(self):
        return sum((line.outstanding for line in self.lines.all()), Decimal("0.000"))

    def refresh_status(self):
        self.status = self.Status.SETTLED if self.outstanding_quantity <= 0 else self.Status.OUTSTANDING
        self.save(update_fields=["status", "updated_at"])

    def __str__(self):
        return f"{self.reference} · {self.department}"


class DepartmentIssueLine(models.Model):
    issue = models.ForeignKey(DepartmentIssue, on_delete=models.PROTECT, related_name="lines")
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT, related_name="issue_lines")
    quantity_issued = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    quantity_consumed = models.DecimalField(**QUANTITY)
    quantity_returned = models.DecimalField(**QUANTITY)
    quantity_wasted = models.DecimalField(**QUANTITY)

    @property
    def accounted(self):
        return self.quantity_consumed + self.quantity_returned + self.quantity_wasted

    @property
    def outstanding(self):
        """Issued but not yet administered, returned or written off."""
        return self.quantity_issued - self.accounted

    @property
    def issued_value(self):
        return (self.quantity_issued * self.batch.purchase_cost_per_base_unit).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.batch} × {self.quantity_issued}"


class StockCount(TimeStampedModel):
    """A physical count reconciled against the ledger at a frozen cutoff.

    Approval is what posts the correcting movements; the count itself never
    edits a balance, so a miscount is visible as a reviewed variance rather
    than an untraceable overwrite.
    """

    class Status(models.TextChoices):
        FROZEN = "frozen", "Snapshot frozen"
        SUBMITTED = "submitted", "Submitted for review"
        APPROVED = "approved", "Approved and posted"
        REJECTED = "rejected", "Rejected"

    reference = models.CharField(max_length=30, unique=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.FROZEN)
    location = models.CharField(max_length=80, default="Pharmacy")
    cutoff_at = models.DateTimeField(default=timezone.now)
    blind_count = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    review_notes = models.TextField(blank=True)
    counted_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="stock_counts")
    witnessed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_counts_witnessed")
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_counts_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-cutoff_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"SC-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def net_variance(self):
        return sum((line.variance for line in self.lines.all()), Decimal("0.000"))

    @property
    def variance_line_count(self):
        return sum(1 for line in self.lines.all() if line.variance)

    def __str__(self):
        return f"{self.reference} · {self.location}"


class StockCountLine(models.Model):
    count = models.ForeignKey(StockCount, on_delete=models.PROTECT, related_name="lines")
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT, related_name="count_lines")
    expected_quantity = models.DecimalField(**QUANTITY)
    counted_quantity = models.DecimalField(**QUANTITY)
    reason = models.CharField(max_length=255, blank=True)

    @property
    def variance(self):
        return self.counted_quantity - self.expected_quantity

    @property
    def variance_value(self):
        """Variance priced at the batch purchase cost, for a reviewable figure."""
        return (self.variance * self.batch.purchase_cost_per_base_unit).quantize(Decimal("0.01"))


class StockWriteOff(TimeStampedModel):
    """A proposal to remove stock that can no longer be sold or used.

    Expired medicine sits in the balance and in the valuation until somebody
    authorises its removal. Requesting is separate from approving so that no
    one person can quietly make stock disappear, and approval is what posts the
    movement.
    """

    class Reason(models.TextChoices):
        EXPIRED = "expired", "Expired"
        DAMAGED = "damaged", "Damaged or broken"
        CONTAMINATED = "contaminated", "Contaminated or unsafe"
        RECALLED = "recalled", "Recalled by supplier or authority"
        OTHER = "other", "Other, described below"

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting independent approval"
        APPROVED = "approved", "Approved and posted"
        REJECTED = "rejected", "Rejected"

    reference = models.CharField(max_length=30, unique=True, blank=True)
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT, related_name="write_offs")
    quantity = models.DecimalField(**QUANTITY, validators=[MinValueValidator(Decimal("0.001"))])
    reason = models.CharField(max_length=16, choices=Reason.choices)
    narrative = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="write_offs_requested")
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="write_offs_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"WO-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def value_at_cost(self):
        return (self.quantity * self.batch.purchase_cost_per_base_unit).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.reference} · {self.batch}"


class TheatreCase(TimeStampedModel):
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="theatre_cases")
    procedure_label = models.CharField(max_length=200)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    team = models.TextField(blank=True)
    consent_recorded = models.BooleanField(default=False)
    preoperative_documentation = models.TextField(blank=True)
    anaesthetic_record = models.TextField(blank=True)
    procedure_record = models.TextField(blank=True)
    recovery_observations = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=[("scheduled", "Scheduled"), ("completed", "Completed"), ("cancelled", "Cancelled")], default="scheduled")
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT)


class DentalRecord(TimeStampedModel):
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="dental_records")
    complaint = models.TextField()
    findings = models.TextField(blank=True)
    tooth_identifier = models.CharField(max_length=40, blank=True)
    procedure_label = models.CharField(max_length=160, blank=True)
    follow_up = models.TextField(blank=True)
    clinician = models.ForeignKey(User, on_delete=models.PROTECT)


class MaternityRecord(TimeStampedModel):
    encounter = models.OneToOneField(Encounter, on_delete=models.PROTECT, related_name="maternity_record")
    maternal_observations = models.TextField(blank=True)
    labour_delivery_record = models.TextField(blank=True)
    outcome = models.TextField(blank=True)
    template_reviewed_for_production = models.BooleanField(default=False)
    authored_by = models.ForeignKey(User, on_delete=models.PROTECT)


class NewbornLink(TimeStampedModel):
    maternity_record = models.ForeignKey(MaternityRecord, on_delete=models.PROTECT, related_name="newborns")
    newborn = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="birth_links")
    relationship_notes = models.CharField(max_length=255, blank=True)


class ClinicalAttachment(TimeStampedModel):
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="attachments")
    encounter = models.ForeignKey(Encounter, null=True, blank=True, on_delete=models.PROTECT, related_name="attachments")
    file = models.FileField(upload_to="clinical/%Y/%m/")
    original_name = models.CharField(max_length=255)
    description = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)


class Refund(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Rejected"
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    reason = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="refunds_requested")
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="refunds_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    @property
    def refundable_remaining(self):
        already = self.payment.refunds.filter(status__in=[self.Status.APPROVED, self.Status.PAID]).exclude(pk=self.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return self.payment.amount - already


class ExceptionRecord(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_REVIEW = "in_review", "In review"
        RESOLVED = "resolved", "Resolved"
    category = models.CharField(max_length=40)
    severity = models.CharField(max_length=12, choices=[("info", "Information"), ("warning", "Warning"), ("urgent", "Urgent")], default="warning")
    summary = models.CharField(max_length=255)
    evidence = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_exceptions")
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class ImportJob(TimeStampedModel):
    idempotency_key = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=40)
    filename = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=[("uploaded", "Uploaded"), ("validated", "Validated"), ("committed", "Committed"), ("failed", "Failed")])
    dry_run = models.BooleanField(default=True)
    row_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    report = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)


class DowntimeEntry(TimeStampedModel):
    paper_reference = models.CharField(max_length=80, unique=True)
    event_type = models.CharField(max_length=30)
    event_at = models.DateTimeField()
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.PROTECT)
    details = models.JSONField(default=dict)
    entered_by = models.ForeignKey(User, on_delete=models.PROTECT)
    reconciled = models.BooleanField(default=False)


class AppendOnlyQuerySet(models.QuerySet):
    """Refuse the bulk paths that bypass Model.save()."""

    def update(self, **kwargs):
        raise ValidationError("Audit events are append-only; they cannot be updated.")

    def delete(self):
        raise ValidationError("Audit events are append-only; they cannot be deleted.")


class AuditEvent(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)
    effective_role = models.CharField(max_length=24, blank=True)
    action = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    session_key = models.CharField(max_length=80, blank=True)
    device_hint = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["entity_type", "entity_id"]), models.Index(fields=["created_at"])]

    def save(self, *args, **kwargs):
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events are append-only; they cannot be deleted.")


class LoginAttempt(models.Model):
    """Failed sign-in evidence. Kept in the database, not process memory, so a
    restart does not clear a lockout and the owner can review attempts."""

    username = models.CharField(max_length=150, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    attempted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-attempted_at"]
        indexes = [models.Index(fields=["username", "attempted_at"])]

    LOCKOUT_THRESHOLD = 8
    LOCKOUT_WINDOW_MINUTES = 15

    @classmethod
    def window_start(cls):
        return timezone.now() - timedelta(minutes=cls.LOCKOUT_WINDOW_MINUTES)

    @classmethod
    def recent_failures(cls, username):
        return cls.objects.filter(username=username[:150], attempted_at__gte=cls.window_start()).count()

    @classmethod
    def is_locked(cls, username):
        if not username:
            return False
        return cls.recent_failures(username) >= cls.LOCKOUT_THRESHOLD

    @classmethod
    def clear(cls, username):
        cls.objects.filter(username=username[:150]).delete()
