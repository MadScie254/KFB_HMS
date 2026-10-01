"""Inventory intelligence and owner brief regressions."""

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .analytics import control_adoption, shrinkage, supplier_price_history
from .models import DepartmentIssue, PurchaseOrder, PurchaseOrderLine, StockBatch, StockMovement, Supplier
from .services import approve_purchase_order, check_delivery, issue_to_department, receive_delivery
from .test_support import JPEG_BYTES, HospitalFixtureMixin
from .views import owner_brief_context


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class IntelligenceAndBriefTests(HospitalFixtureMixin, TestCase):
    """The figures that tell the owner whether the controls are working."""

    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.supplier = Supplier.objects.create(name="Intelligence Supplies")

    def deliver(self, unit_cost, reference, batch, check=False):
        order = PurchaseOrder.objects.create(supplier=self.supplier, requested_by=self.procurement)
        line = PurchaseOrderLine.objects.create(
            order=order, item=self.product, quantity_base_units=Decimal("10"), quoted_unit_cost=Decimal("2.00")
        )
        approve_purchase_order(actor=self.reviewer, order_id=order.pk)
        with override_settings(MEDIA_ROOT=self.media_root):
            receipt = receive_delivery(
                actor=self.procurement, purchase_order_id=order.pk,
                supplier_invoice_reference=reference,
                invoice_amount=(unit_cost * Decimal("10")), invoice_date=timezone.localdate(),
                invoice_photo=SimpleUploadedFile("i.jpg", JPEG_BYTES, content_type="image/jpeg"),
                lines=[{"order_line": line, "quantity_received": Decimal("10"), "batch_number": batch,
                        "expiry_date": timezone.localdate() + timedelta(days=400), "actual_unit_cost": unit_cost}],
            )
        if check:
            check_delivery(actor=self.pharmacist, receipt_id=receipt.pk)
        return receipt

    def test_supplier_price_history_surfaces_a_rising_unit_cost(self):
        self.deliver(Decimal("2.00"), "PH-1", "PH-B1")
        self.deliver(Decimal("2.50"), "PH-2", "PH-B2")
        history = supplier_price_history(365)
        row = next(r for r in history["rows"] if r["item"].pk == self.product.pk)
        self.assertEqual(row["delivery_count"], 2)
        self.assertEqual(row["latest"]["unit_cost"], Decimal("2.50"))
        self.assertEqual(row["change"], Decimal("0.50"))
        self.assertEqual(row["percent_change"], Decimal("25.00"))
        self.assertIn(row, history["rising"])

    def test_price_history_reports_a_first_delivery_without_inventing_a_change(self):
        self.deliver(Decimal("2.00"), "PH-ONLY", "PH-ONLY-B")
        row = next(r for r in supplier_price_history(365)["rows"] if r["item"].pk == self.product.pk)
        self.assertIsNone(row["percent_change"])
        self.assertIsNone(row["previous"])

    def test_adoption_measures_evidence_and_prompt_checking(self):
        self.deliver(Decimal("2.00"), "AD-1", "AD-B1", check=True)
        self.deliver(Decimal("2.00"), "AD-2", "AD-B2", check=False)
        adoption = control_adoption(30)
        self.assertEqual(adoption["deliveries"], 2)
        self.assertEqual(adoption["evidence_percent"], Decimal("100.0"))
        self.assertEqual(adoption["checked_percent"], Decimal("50.0"))
        self.assertEqual(adoption["checked_within_24h"], 1)

    def test_adoption_returns_no_percentage_when_there_is_nothing_to_measure(self):
        adoption = control_adoption(30)
        self.assertEqual(adoption["deliveries"], 0)
        self.assertIsNone(adoption["evidence_percent"], "A percentage of nothing is not zero, it is unavailable.")

    def test_shrinkage_is_not_claimed_before_a_count_has_been_approved(self):
        result = shrinkage(90)
        self.assertFalse(result["measured"])
        self.assertEqual(result["loss_value"], Decimal("0.00"))

    def test_brief_raises_expired_stock_and_an_absent_count(self):
        batch = StockBatch.objects.create(
            item=self.product, batch_number="BRIEF-EXP",
            expiry_date=timezone.localdate() - timedelta(days=2),
            purchase_cost_per_base_unit=Decimal("2.00"),
        )
        StockMovement.objects.create(
            batch=batch, movement_type=StockMovement.MovementType.RECEIPT, quantity_delta=Decimal("30"),
            reference_type="Opening", reference_id="b", idempotency_key="brief-exp", entered_by=self.pharmacist,
        )
        headlines = " ".join(item["headline"] for item in owner_brief_context()["brief_items"])
        self.assertIn("expired batch", headlines)
        self.assertIn("No stock count has been approved", headlines)

    def test_brief_raises_a_delivery_nobody_checked(self):
        self.deliver(Decimal("2.00"), "BRIEF-UNCHECKED", "BRIEF-B", check=False)
        headlines = " ".join(item["headline"] for item in owner_brief_context()["brief_items"])
        self.assertIn("never independently checked", headlines)

    def test_brief_raises_ward_stock_nobody_accounted_for(self):
        issue = issue_to_department(
            actor=self.pharmacist, department="Theatre", received_by_name="Sister Wanjiru",
            lines=[{"batch": self.batch, "quantity": Decimal("5")}],
        )
        DepartmentIssue.objects.filter(pk=issue.pk).update(issued_at=timezone.now() - timedelta(days=10))
        headlines = " ".join(item["headline"] for item in owner_brief_context()["brief_items"])
        self.assertIn("unaccounted for over a week", headlines)

    def test_brief_is_ordered_with_the_urgent_items_first(self):
        batch = StockBatch.objects.create(
            item=self.product, batch_number="ORDER-EXP",
            expiry_date=timezone.localdate() - timedelta(days=2), purchase_cost_per_base_unit=Decimal("2.00"),
        )
        StockMovement.objects.create(
            batch=batch, movement_type=StockMovement.MovementType.RECEIPT, quantity_delta=Decimal("10"),
            reference_type="Opening", reference_id="o", idempotency_key="order-exp", entered_by=self.pharmacist,
        )
        severities = [item["severity"] for item in owner_brief_context()["brief_items"]]
        self.assertEqual(severities, sorted(severities, key=lambda s: {"urgent": 0, "warning": 1, "info": 2}[s]))

    def test_brief_and_intelligence_screens_open_for_the_owner_only(self):
        self.client.login(username=self.owner.username, password=self.password)
        self.assertEqual(self.client.get(reverse("owner_brief")).status_code, 200)
        self.assertEqual(self.client.get(reverse("stock_intelligence")).status_code, 200)
        self.client.login(username=self.reception.username, password=self.password)
        self.assertEqual(self.client.get(reverse("owner_brief")).status_code, 403)
        self.assertEqual(self.client.get(reverse("stock_intelligence")).status_code, 403)
