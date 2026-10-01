"""Shared fixtures and real upload bytes for hospital tests."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from .models import CashShift, CatalogueItem, Patient, PriceVersion, Role, StockBatch, StockMovement
from .services import prepare_pharmacy_order

# Upload validation reads leading bytes, so test images need a JPEG signature.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class HospitalFixtureMixin:
    """Shared demo fixture: one of each role, a priced product and a stocked batch."""

    password = "Safe-Test-Password-2026!"

    def make_user(self, username, role):
        user = User.objects.create_user(username=username, password=self.password)
        user.staff_profile.role = role
        user.staff_profile.save(update_fields=["role"])
        return user

    def setUp(self):
        self.owner = self.make_user("owner", Role.OWNER)
        self.reception = self.make_user("reception", Role.RECEPTION)
        self.pharmacist = self.make_user("pharmacist", Role.PHARMACY)
        self.clinician = self.make_user("clinician", Role.CLINICIAN)
        self.reviewer = self.make_user("reviewer", Role.REVIEWER)
        self.nurse = self.make_user("nurse", Role.NURSE)
        self.procurement = self.make_user("procurement", Role.PROCUREMENT)
        self.patient = Patient.objects.create(first_name="Test", last_name="Patient", estimated_age_years=30, registered_by=self.reception)
        self.product = CatalogueItem.objects.create(
            code="TEST-TAB", name="Test tablet", kind=CatalogueItem.Kind.PRODUCT,
            department="Pharmacy", base_unit="tablet", sale_unit="box",
            units_per_sale_unit=100, reorder_level=10,
        )
        PriceVersion.objects.create(item=self.product, amount=Decimal("5.00"), reason="Test", approved_by=self.owner)
        self.batch = StockBatch.objects.create(item=self.product, batch_number="B-001", expiry_date=timezone.localdate() + timedelta(days=365), purchase_cost_per_base_unit=Decimal("2.00"))
        StockMovement.objects.create(batch=self.batch, movement_type=StockMovement.MovementType.RECEIPT, quantity_delta=200, to_location="Pharmacy", reference_type="OpeningCount", reference_id="COUNT-1", idempotency_key="opening-test", entered_by=self.pharmacist)
        self.shift = CashShift.objects.create(cashier=self.reception, label="Day", opening_float=Decimal("1000.00"))

    def prepare(self, qty=15):
        return prepare_pharmacy_order(actor=self.pharmacist, customer_name="Walk-in Test", patient=None, items=[(self.product, Decimal(qty))])
