"""Stock receipt, reconciliation, custody, and write-off workflows."""

import re
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .analytics import departmental_custody, shrinkage, stock_activity, stock_position
from .models import (
    AuditEvent,
    CatalogueItem,
    DepartmentIssue,
    ExceptionRecord,
    GoodsReceipt,
    Payment,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    Setting,
    StockBatch,
    StockCount,
    StockMovement,
    StockWriteOff,
    Supplier,
)
from .services import (
    account_for_issue,
    approve_purchase_order,
    check_delivery,
    dispense_order,
    issue_to_department,
    open_stock_count,
    receive_delivery,
    record_payment,
    request_write_off,
    review_stock_count,
    review_write_off,
    set_batch_disposition,
    submit_stock_count,
)
from .test_support import JPEG_BYTES, HospitalFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StockControlTests(HospitalFixtureMixin, TestCase):
    """Stock entering the hospital, and the shelf being reconciled with the ledger.

    Before these workflows existed the ledger could only ever go down: a sale
    deducted stock and nothing put any back. Each test here pins one of the
    controls that make the balance trustworthy in both directions.
    """

    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.supplier = Supplier.objects.create(name="Demo Medical Supplies")

    def photo(self, name="invoice.jpg"):
        return SimpleUploadedFile(name, JPEG_BYTES, content_type="image/jpeg")

    def approved_order(self, quantity=Decimal("100"), unit_cost=Decimal("2.00")):
        order = PurchaseOrder.objects.create(supplier=self.supplier, requested_by=self.procurement)
        PurchaseOrderLine.objects.create(
            order=order, item=self.product, quantity_base_units=quantity, quoted_unit_cost=unit_cost
        )
        approve_purchase_order(actor=self.reviewer, order_id=order.pk)
        order.refresh_from_db()
        return order

    def delivery_line(self, order, quantity=Decimal("100"), batch="B-NEW", expiry_days=365, unit_cost=Decimal("2.00")):
        return {
            "order_line": order.lines.first(),
            "quantity_received": quantity,
            "batch_number": batch,
            "expiry_date": timezone.localdate() + timedelta(days=expiry_days),
            "actual_unit_cost": unit_cost,
        }

    def receive(self, order, *, reference="SUP-INV-001", amount=Decimal("200.00"), actor=None, lines=None, photo=True):
        with override_settings(MEDIA_ROOT=self.media_root):
            return receive_delivery(
                actor=actor or self.procurement,
                purchase_order_id=order.pk,
                supplier_invoice_reference=reference,
                invoice_amount=amount,
                invoice_date=timezone.localdate(),
                invoice_photo=self.photo() if photo else None,
                lines=lines if lines is not None else [self.delivery_line(order)],
            )

    def test_delivery_increases_stock_and_balances_against_the_invoice(self):
        order = self.approved_order()
        receipt = self.receive(order)
        batch = StockBatch.objects.get(item=self.product, batch_number="B-NEW")
        self.assertEqual(batch.quantity_on_hand, Decimal("100"))
        self.assertEqual(receipt.received_value, Decimal("200.00"))
        self.assertEqual(receipt.invoice_variance, Decimal("0.00"))
        self.assertTrue(receipt.invoice_photo)
        self.assertIsNotNone(receipt.posted_at)
        order.refresh_from_db()
        self.assertEqual(order.status, "received")
        movement = StockMovement.objects.get(reference_type="GoodsReceipt", reference_id=str(receipt.pk))
        self.assertEqual(movement.movement_type, StockMovement.MovementType.RECEIPT)
        self.assertEqual(movement.quantity_delta, Decimal("100.000"))

    def test_repeated_delivery_lines_share_one_quantity_aggregate(self):
        order = self.approved_order(quantity=Decimal("100"))
        lines = [
            self.delivery_line(order, quantity=Decimal("40"), batch=f"B-REPEAT-{index}")
            for index in range(3)
        ]
        with CaptureQueriesContext(connection) as queries:
            receipt = self.receive(order, amount=Decimal("240.00"), lines=lines)
        receipt_queries = [
            query["sql"] for query in queries
            if "hospital_goodsreceiptline" in query["sql"].lower() and "SUM(" in query["sql"].upper()
        ]
        self.assertEqual(len(receipt_queries), 1)
        self.assertEqual(receipt.received_value, Decimal("240.00"))
        order.refresh_from_db()
        self.assertEqual(order.status, "received")
        self.assertTrue(ExceptionRecord.objects.filter(summary__contains="exceeds the approved order").exists())

    def test_repeat_batch_receipts_use_weighted_average_and_preserve_dispense_cost(self):
        first_order = self.approved_order(unit_cost=Decimal("3.00"))
        first = self.receive(
            first_order, reference="COST-001", amount=Decimal("300"),
            lines=[self.delivery_line(first_order, batch="B-001", unit_cost=Decimal("3.00"))],
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.purchase_cost_per_base_unit, Decimal("2.333333"))

        second_order = self.approved_order(unit_cost=Decimal("5.00"))
        second = self.receive(
            second_order, reference="COST-002", amount=Decimal("500"),
            lines=[self.delivery_line(second_order, batch="B-001", unit_cost=Decimal("5.00"))],
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.purchase_cost_per_base_unit, Decimal("3.000000"))
        self.assertEqual(stock_position()["stock_value_cost"], Decimal("1200.00"))
        self.assertEqual(first.lines.get().actual_unit_cost, Decimal("3.00"))
        self.assertEqual(second.lines.get().actual_unit_cost, Decimal("5.00"))

        basket = self.prepare(50)
        record_payment(
            actor=self.reception, invoice_id=basket.invoice_id, amount=250,
            method=Payment.Method.CASH, reference="", idempotency_key="weighted-cost-payment",
        )
        dispense_order(actor=self.pharmacist, order_id=basket.pk, idempotency_key="weighted-cost-dispense")
        movement = StockMovement.objects.get(
            reference_type="PharmacyOrder", reference_id=str(basket.pk),
        )
        self.assertEqual(movement.unit_cost_at_event, Decimal("3.000000"))
        self.assertEqual(stock_position()["stock_value_cost"], Decimal("1050.00"))
        activity = stock_activity(7)
        self.assertEqual(activity["cost_of_goods_dispensed"], Decimal("150.00"))
        self.assertEqual(activity["product_gross_margin"], Decimal("100.00"))

        later_order = self.approved_order(quantity=Decimal("50"), unit_cost=Decimal("7.00"))
        self.receive(
            later_order, reference="COST-003", amount=Decimal("350"),
            lines=[self.delivery_line(
                later_order, quantity=Decimal("50"), batch="B-001", unit_cost=Decimal("7.00"),
            )],
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.purchase_cost_per_base_unit, Decimal("3.500000"))
        self.assertEqual(stock_position()["stock_value_cost"], Decimal("1400.00"))
        self.assertEqual(stock_activity(7)["cost_of_goods_dispensed"], Decimal("150.00"))

    def test_delivery_without_an_invoice_photograph_is_refused(self):
        order = self.approved_order()
        with self.assertRaisesMessage(ValidationError, "photograph"):
            self.receive(order, photo=False)
        self.assertFalse(StockBatch.objects.filter(batch_number="B-NEW").exists())

    def test_same_supplier_invoice_cannot_be_received_twice_on_one_order(self):
        order = self.approved_order(quantity=Decimal("200"))
        self.receive(order, lines=[self.delivery_line(order, quantity=Decimal("50"))])
        with self.assertRaisesMessage(ValidationError, "already been received"):
            self.receive(order, lines=[self.delivery_line(order, quantity=Decimal("50"), batch="B-TWO")])
        self.assertEqual(
            StockMovement.objects.filter(movement_type=StockMovement.MovementType.RECEIPT, reference_type="GoodsReceipt").count(),
            1,
        )

    def test_expired_stock_cannot_be_received(self):
        order = self.approved_order()
        line = self.delivery_line(order, expiry_days=-1)
        with self.assertRaisesMessage(ValidationError, "Expired stock cannot be received"):
            self.receive(order, lines=[line])
        self.assertFalse(GoodsReceipt.objects.exists())

    def test_invalid_stored_thresholds_block_receiving_without_partial_post(self):
        order = self.approved_order()
        thresholds = {
            "near_expiry_days": ("90", "NaN"),
            "purchase_cost_variance_fraction": ("0.10", "-1"),
            "supplier_invoice_tolerance": ("1.00", "Infinity"),
        }
        for key, (valid, _) in thresholds.items():
            Setting.objects.create(key=key, value=valid)
        for key, (valid, invalid) in thresholds.items():
            with self.subTest(key=key):
                Setting.objects.filter(key=key).update(value=invalid)
                with self.assertRaisesMessage(ValidationError, key):
                    self.receive(order)
                self.assertFalse(GoodsReceipt.objects.exists())
                Setting.objects.filter(key=key).update(value=valid)

    def test_partial_delivery_leaves_the_order_open(self):
        order = self.approved_order(quantity=Decimal("100"))
        self.receive(order, amount=Decimal("80.00"), lines=[self.delivery_line(order, quantity=Decimal("40"))])
        order.refresh_from_db()
        self.assertEqual(order.status, "part_received")
        self.receive(
            order,
            reference="SUP-INV-002",
            amount=Decimal("120.00"),
            lines=[self.delivery_line(order, quantity=Decimal("60"), batch="B-NEW-2")],
        )
        order.refresh_from_db()
        self.assertEqual(order.status, "received")

    def test_price_and_invoice_differences_are_flagged_not_hidden(self):
        order = self.approved_order(quantity=Decimal("100"), unit_cost=Decimal("2.00"))
        # Invoiced at 3.00 against a 2.00 quote, and the invoice total does not
        # match the goods either: both are review items, neither blocks the post.
        self.receive(
            order,
            amount=Decimal("500.00"),
            lines=[self.delivery_line(order, unit_cost=Decimal("3.00"))],
        )
        flags = ExceptionRecord.objects.filter(category="purchase_discrepancy")
        self.assertTrue(flags.filter(summary__icontains="cost differs").exists())
        self.assertTrue(flags.filter(summary__icontains="does not match the goods").exists())
        self.assertEqual(
            StockBatch.objects.get(batch_number="B-NEW").quantity_on_hand,
            Decimal("100"),
            "The stock that arrived must still be recorded when a price is queried.",
        )

    def test_receiver_cannot_check_their_own_delivery(self):
        order = self.approved_order()
        receipt = self.receive(order)
        with self.assertRaisesMessage(ValidationError, "cannot also check"):
            check_delivery(actor=self.procurement, receipt_id=receipt.pk)
        checked = check_delivery(actor=self.pharmacist, receipt_id=receipt.pk, discrepancy_notes="One box dented.")
        self.assertEqual(checked.checked_by, self.pharmacist)
        self.assertEqual(checked.discrepancy_notes, "One box dented.")

    def test_reception_cannot_receive_stock(self):
        order = self.approved_order()
        with self.assertRaisesMessage(ValidationError, "procurement or pharmacy"):
            self.receive(order, actor=self.reception)

    def test_unapproved_order_cannot_receive_stock(self):
        order = PurchaseOrder.objects.create(supplier=self.supplier, requested_by=self.procurement)
        PurchaseOrderLine.objects.create(
            order=order, item=self.product, quantity_base_units=Decimal("10"), quoted_unit_cost=Decimal("2.00")
        )
        with self.assertRaisesMessage(ValidationError, "independently approved"):
            self.receive(order, lines=[self.delivery_line(order, quantity=Decimal("10"))])

    def test_sale_and_delivery_reconcile_in_one_balance(self):
        order = self.approved_order()
        self.receive(order)
        basket = self.prepare(15)
        record_payment(
            actor=self.reception, invoice_id=basket.invoice_id, amount=75,
            method=Payment.Method.CASH, reference="", idempotency_key="pay-reconcile",
        )
        dispense_order(actor=self.pharmacist, order_id=basket.pk, idempotency_key="dispense-reconcile")
        # 200 opening + 100 received - 15 sold, with FEFO taking from the
        # earliest-expiring batch first.
        total = sum(batch.quantity_on_hand for batch in StockBatch.objects.filter(item=self.product))
        self.assertEqual(total, Decimal("285"))

    def test_approved_count_posts_an_adjustment_and_corrects_the_balance(self):
        count = open_stock_count(actor=self.pharmacist, location="Pharmacy", blind_count=True)
        line = count.lines.get(batch=self.batch)
        self.assertEqual(line.expected_quantity, Decimal("200.000"))
        submit_stock_count(
            actor=self.pharmacist, count_id=count.pk,
            counted={line.pk: Decimal("194")}, reasons={line.pk: "Three damaged, three unexplained"},
        )
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.SUBMITTED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"), "Submitting must not move stock.")

        review_stock_count(actor=self.reviewer, count_id=count.pk, approve=True, review_notes="Damage witnessed.")
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.APPROVED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("194"))
        adjustment = StockMovement.objects.get(movement_type=StockMovement.MovementType.ADJUSTMENT)
        self.assertEqual(adjustment.quantity_delta, Decimal("-6.000"))
        self.assertEqual(adjustment.entered_by, self.reviewer)

    def test_counter_cannot_approve_their_own_count(self):
        count = open_stock_count(actor=self.pharmacist)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(actor=self.pharmacist, count_id=count.pk, counted={line.pk: Decimal("190")})
        self.pharmacist.staff_profile.role = Role.REVIEWER
        self.pharmacist.staff_profile.save(update_fields=["role"])
        with self.assertRaisesMessage(ValidationError, "cannot be reviewed by the person who counted"):
            review_stock_count(actor=self.pharmacist, count_id=count.pk, approve=True)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))

    def test_rejected_count_changes_no_balance(self):
        count = open_stock_count(actor=self.pharmacist)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(actor=self.pharmacist, count_id=count.pk, counted={line.pk: Decimal("150")})
        review_stock_count(actor=self.reviewer, count_id=count.pk, approve=False, review_notes="Recount required.")
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.REJECTED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))
        self.assertFalse(StockMovement.objects.filter(movement_type=StockMovement.MovementType.ADJUSTMENT).exists())

    def test_count_snapshot_is_frozen_at_its_cutoff(self):
        count = open_stock_count(actor=self.pharmacist)
        StockMovement.objects.create(
            batch=self.batch, movement_type=StockMovement.MovementType.RECEIPT, quantity_delta=Decimal("50"),
            reference_type="Opening", reference_id="later", idempotency_key="after-cutoff", entered_by=self.pharmacist,
        )
        line = count.lines.get(batch=self.batch)
        self.assertEqual(
            line.expected_quantity, Decimal("200.000"),
            "A movement posted after the cutoff must not change what the count is judged against.",
        )

    def test_movement_during_count_requires_a_new_sheet(self):
        for movement_type, delta in (
            (StockMovement.MovementType.DISPENSE, Decimal("-2")),
            (StockMovement.MovementType.RECEIPT, Decimal("5")),
        ):
            with self.subTest(movement_type=movement_type):
                count = open_stock_count(actor=self.pharmacist)
                line = count.lines.get(batch=self.batch)
                StockMovement.objects.create(
                    batch=self.batch, movement_type=movement_type, quantity_delta=delta,
                    reference_type="Test", reference_id=str(count.pk),
                    idempotency_key=f"during-count-{count.pk}", entered_by=self.pharmacist,
                )
                with self.assertRaisesMessage(ValidationError, "Start a new count"):
                    submit_stock_count(
                        actor=self.pharmacist, count_id=count.pk,
                        counted={line.pk: line.expected_quantity + delta},
                    )
                count.refresh_from_db()
                self.assertEqual(count.status, StockCount.Status.FROZEN)
                self.assertFalse(StockMovement.objects.filter(
                    reference_type="StockCount", reference_id=str(count.pk),
                ).exists())

    def test_backdated_movement_after_submission_blocks_approval(self):
        count = open_stock_count(actor=self.pharmacist)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(actor=self.pharmacist, count_id=count.pk, counted={line.pk: Decimal("194")})
        StockMovement.objects.create(
            batch=self.batch, movement_type=StockMovement.MovementType.RECEIPT,
            quantity_delta=Decimal("2"), event_at=count.cutoff_at,
            reference_type="Test", reference_id="backdated",
            idempotency_key="late-backdated-count", entered_by=self.pharmacist,
        )
        with self.assertRaisesMessage(ValidationError, "Reject the stale sheet"):
            review_stock_count(actor=self.reviewer, count_id=count.pk, approve=True)
        self.assertFalse(StockMovement.objects.filter(
            reference_type="StockCount", reference_id=str(count.pk),
        ).exists())

    def test_movement_after_submission_does_not_block_valid_adjustment(self):
        count = open_stock_count(actor=self.pharmacist)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(actor=self.pharmacist, count_id=count.pk, counted={line.pk: Decimal("194")})
        StockMovement.objects.create(
            batch=self.batch, movement_type=StockMovement.MovementType.DISPENSE,
            quantity_delta=Decimal("-2"), reference_type="Test", reference_id="after-submit",
            idempotency_key="after-submit-count", entered_by=self.pharmacist,
        )
        review_stock_count(actor=self.reviewer, count_id=count.pk, approve=True)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("192"))

    def test_non_pharmacy_count_cannot_change_pharmacy_stock(self):
        with self.assertRaisesMessage(ValidationError, "Only Pharmacy stock"):
            open_stock_count(actor=self.pharmacist, location="Maternity")
        self.assertFalse(StockCount.objects.exists())

        # A sheet created before this restriction must not post a ward variance.
        count = open_stock_count(actor=self.pharmacist)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(actor=self.pharmacist, count_id=count.pk, counted={line.pk: Decimal("50")})
        count.location = "Maternity"
        count.save(update_fields=["location"])
        with self.assertRaisesMessage(ValidationError, "no separate ledger balance"):
            review_stock_count(actor=self.reviewer, count_id=count.pk, approve=True)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))
        self.assertFalse(StockMovement.objects.filter(reference_type="StockCount").exists())



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StockScreenTests(HospitalFixtureMixin, TestCase):
    """The screens themselves: a control nobody can reach is not a control."""

    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.supplier = Supplier.objects.create(name="Demo Medical Supplies")
        self.order = PurchaseOrder.objects.create(supplier=self.supplier, requested_by=self.procurement)
        self.order_line = PurchaseOrderLine.objects.create(
            order=self.order, item=self.product,
            quantity_base_units=Decimal("100"), quoted_unit_cost=Decimal("2.00"),
        )
        approve_purchase_order(actor=self.reviewer, order_id=self.order.pk)

    def test_batch_only_search_keeps_matching_product_visible(self):
        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse("stock"), {"q": self.batch.batch_number})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["item"].pk for row in response.context["products"]], [self.product.pk])
        self.assertContains(response, f"Batch <strong>{self.batch.batch_number}</strong>", html=False)

    def test_stock_page_two_labels_local_sorting_and_uses_epoch_dates(self):
        StockMovement.objects.bulk_create([
            StockMovement(
                batch=self.batch, movement_type=StockMovement.MovementType.RECEIPT,
                quantity_delta=1, unit_cost_at_event=Decimal("2.00"),
                to_location="Pharmacy", reference_type="SortTest",
                reference_id=str(index), idempotency_key=f"sort-page-{index}", entered_by=self.pharmacist,
            ) for index in range(45)
        ])
        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse("stock"), {"page": "2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].number, 2)
        self.assertContains(response, "Column sorting applies to this page only")
        self.assertRegex(response.content.decode(), r'<td data-sort-value="\d{10}">')

    def test_stock_sort_groups_each_product_with_its_batches(self):
        second = CatalogueItem.objects.create(
            code="ALPHA-TAB", name="Alpha tablet", kind=CatalogueItem.Kind.PRODUCT,
            department="Pharmacy", base_unit="tablet", sale_unit="box",
            units_per_sale_unit=100, reorder_level=10,
        )
        for item, number in (
            (self.product, "TEST-SECOND"),
            (second, "ALPHA-FIRST"),
            (second, "ALPHA-SECOND"),
        ):
            batch = StockBatch.objects.create(
                item=item, batch_number=number,
                purchase_cost_per_base_unit=Decimal("1.00"),
            )
            StockMovement.objects.create(
                batch=batch, movement_type=StockMovement.MovementType.RECEIPT,
                quantity_delta=10, to_location="Pharmacy", reference_type="OpeningCount",
                reference_id=number, idempotency_key=f"sort-{number}", entered_by=self.pharmacist,
            )
        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse("stock"))
        self.assertContains(response, "data-sort-grouped")
        groups = re.findall(r"<tbody data-sort-group>(.*?)</tbody>", response.content.decode(), re.S)
        self.assertEqual(len(groups), 2)
        alpha = next(group for group in groups if "Alpha tablet" in group)
        test = next(group for group in groups if "Test tablet" in group)
        self.assertIn("ALPHA-FIRST", alpha)
        self.assertIn("ALPHA-SECOND", alpha)
        self.assertNotIn("TEST-SECOND", alpha)
        self.assertIn("B-001", test)
        self.assertIn("TEST-SECOND", test)
        self.assertNotIn("ALPHA-FIRST", test)

    def test_quarantined_batch_release_is_visible_to_reviewer_only(self):
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["status"])
        url = reverse("batch_disposition", args=[self.batch.pk])
        self.client.force_login(self.pharmacist)
        self.assertNotContains(self.client.get(reverse("stock")), url)
        self.assertEqual(self.client.post(url, {
            "status": StockBatch.Status.ACTIVE, "reason": "Unapproved",
        }).status_code, 403)

        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(reverse("stock")), url)
        response = self.client.post(url, {
            "status": StockBatch.Status.ACTIVE,
            "reason": "Seal intact after independent inspection.",
        })
        self.assertRedirects(response, reverse("stock"))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, StockBatch.Status.ACTIVE)
        self.assertTrue(AuditEvent.objects.filter(action="stock_batch.disposition", entity_id=str(self.batch.pk)).exists())

    def test_expired_quarantined_batch_shows_write_off_instead_of_release(self):
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.save(update_fields=["status", "expiry_date"])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("stock"))
        self.assertContains(response, "Expired stock cannot be released")
        self.assertContains(response, reverse("write_offs"))
        self.assertNotContains(response, reverse("batch_disposition", args=[self.batch.pk]))

    def post_delivery(self, reference="SUP-INV-100"):
        return self.client.post(
            reverse("goods_receipt_create", args=[self.order.pk]),
            {
                "supplier_invoice_reference": reference,
                "invoice_amount": "200.00",
                "invoice_date": timezone.localdate().isoformat(),
                "delivered_on": timezone.localdate().isoformat(),
                "invoice_photo": SimpleUploadedFile("invoice.jpg", JPEG_BYTES, content_type="image/jpeg"),
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-order_line": str(self.order_line.pk),
                "lines-0-quantity_received": "100",
                "lines-0-batch_number": "B-SCREEN",
                "lines-0-expiry_date": (timezone.localdate() + timedelta(days=400)).isoformat(),
                "lines-0-actual_unit_cost": "2.00",
            },
            follow=True,
        )

    def test_stock_page_shows_valuation_and_shortages(self):
        self.client.login(username=self.pharmacist.username, password=self.password)
        response = self.client.get(reverse("stock"))
        self.assertEqual(response.status_code, 200)
        position = response.context["position"]
        # 200 tablets at a 2.00 purchase cost, priced for sale at 5.00.
        self.assertEqual(position["stock_value_cost"], Decimal("400.00"))
        self.assertEqual(position["stock_value_retail"], Decimal("1000.00"))
        self.assertContains(response, "Sellable stock at cost")

    def test_stock_page_filters_to_products_below_reorder_level(self):
        self.client.login(username=self.pharmacist.username, password=self.password)
        self.assertEqual(len(self.client.get(reverse("stock"), {"view": "low"}).context["products"]), 0)
        StockMovement.objects.create(
            batch=self.batch, movement_type=StockMovement.MovementType.ADJUSTMENT, quantity_delta=Decimal("-195"),
            reference_type="Test", reference_id="low", idempotency_key="drop-to-low", entered_by=self.pharmacist,
        )
        low = self.client.get(reverse("stock"), {"view": "low"}).context["products"]
        self.assertEqual([row["item"].code for row in low], ["TEST-TAB"])

    def test_procurement_can_record_a_delivery_through_the_screen(self):
        self.client.login(username=self.procurement.username, password=self.password)
        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.post_delivery()
        self.assertEqual(response.status_code, 200)
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(receipt.received_by, self.procurement)
        self.assertTrue(receipt.invoice_photo)
        self.assertEqual(StockBatch.objects.get(batch_number="B-SCREEN").quantity_on_hand, Decimal("100"))

    def test_delivery_screen_refuses_a_submission_without_a_photograph(self):
        self.client.login(username=self.procurement.username, password=self.password)
        response = self.client.post(
            reverse("goods_receipt_create", args=[self.order.pk]),
            {
                "supplier_invoice_reference": "SUP-INV-200",
                "invoice_amount": "200.00",
                "delivered_on": timezone.localdate().isoformat(),
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-order_line": str(self.order_line.pk),
                "lines-0-quantity_received": "100",
                "lines-0-batch_number": "B-NOPHOTO",
                "lines-0-actual_unit_cost": "2.00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(GoodsReceipt.objects.exists())
        self.assertFalse(StockBatch.objects.filter(batch_number="B-NOPHOTO").exists())

    def test_invoice_photograph_is_served_only_to_authorised_roles(self):
        self.client.login(username=self.procurement.username, password=self.password)
        with override_settings(MEDIA_ROOT=self.media_root):
            self.post_delivery()
            receipt = GoodsReceipt.objects.get()
            self.client.login(username=self.reception.username, password=self.password)
            self.assertEqual(self.client.get(reverse("goods_receipt_invoice", args=[receipt.pk])).status_code, 403)
            self.client.login(username=self.owner.username, password=self.password)
            allowed = self.client.get(reverse("goods_receipt_invoice", args=[receipt.pk]))
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(AuditEvent.objects.filter(action="goods_receipt.invoice_viewed").exists())

    def test_stock_count_sheet_can_be_counted_and_reviewed_through_the_screens(self):
        self.client.login(username=self.pharmacist.username, password=self.password)
        self.client.post(reverse("stock_count_open"), {"location": "Pharmacy", "blind_count": "on"})
        count = StockCount.objects.get()
        line = count.lines.get(batch=self.batch)
        self.client.post(
            reverse("stock_count_detail", args=[count.pk]),
            {f"counted-{line.pk}": "197", f"reason-{line.pk}": "Breakage"},
        )
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.SUBMITTED)

        self.client.login(username=self.reviewer.username, password=self.password)
        self.client.post(reverse("stock_count_review", args=[count.pk]), {"decision": "approve", "review_notes": "Seen"})
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.APPROVED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("197"))

    def test_invalid_variance_threshold_blocks_count_approval(self):
        count = open_stock_count(actor=self.pharmacist, location="Pharmacy", blind_count=True)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(
            actor=self.pharmacist, count_id=count.pk,
            counted={line.pk: Decimal("194")}, reasons={line.pk: "Damaged stock"},
        )
        Setting.objects.create(key="stock_variance_review_value", value="500")
        Setting.objects.filter(key="stock_variance_review_value").update(value="NaN")
        with self.assertRaisesMessage(ValidationError, "stock_variance_review_value"):
            review_stock_count(actor=self.reviewer, count_id=count.pk, approve=True)
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.SUBMITTED)
        self.assertFalse(StockMovement.objects.filter(reference_type="StockCount", reference_id=str(count.pk)).exists())
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))

    def test_invalid_count_preserves_every_typed_line_and_reason(self):
        StockBatch.objects.create(
            item=self.product, batch_number="SECOND-COUNT-BATCH",
            purchase_cost_per_base_unit=Decimal("2.00"),
        )
        count = open_stock_count(actor=self.pharmacist, blind_count=True)
        lines = list(count.lines.order_by("pk"))
        self.assertEqual(len(lines), 2)
        url = reverse("stock_count_detail", args=[count.pk])
        self.client.force_login(self.pharmacist)
        response = self.client.post(url, {
            f"counted-{lines[0].pk}": "111.000", f"reason-{lines[0].pk}": "Counted shelf",
            f"counted-{lines[1].pk}": "", f"reason-{lines[1].pk}": "Need recount",
        })
        self.assertEqual(response.status_code, 200)
        by_pk = {line.pk: line for line in response.context["lines"]}
        self.assertEqual(by_pk[lines[0].pk].submitted_counted, "111.000")
        self.assertEqual(by_pk[lines[0].pk].submitted_reason, "Counted shelf")
        self.assertEqual(by_pk[lines[1].pk].submitted_reason, "Need recount")
        self.assertTrue(by_pk[lines[1].pk].count_error)
        self.assertContains(response, 'value="111.000"')
        self.assertContains(response, 'value="Need recount"')
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.Status.FROZEN)
        self.assertEqual(count.lines.get(pk=lines[0].pk).counted_quantity, Decimal("0.000"))

        StockMovement.objects.create(
            batch=self.batch, movement_type=StockMovement.MovementType.RECEIPT,
            quantity_delta=1, to_location="Pharmacy", reference_type="Test",
            reference_id="stale", idempotency_key="stale-count-test", entered_by=self.pharmacist,
        )
        response = self.client.post(url, {
            f"counted-{lines[0].pk}": "111", f"reason-{lines[0].pk}": "Counted shelf",
            f"counted-{lines[1].pk}": "0", f"reason-{lines[1].pk}": "Empty",
        })
        self.assertContains(response, "Stock moved after this sheet")
        self.assertContains(response, 'value="111"')
        self.assertContains(response, 'value="Empty"')

    def test_stock_count_screen_rejects_a_ward_location(self):
        self.client.login(username=self.pharmacist.username, password=self.password)
        response = self.client.post(
            reverse("stock_count_open"), {"location": "Maternity", "blind_count": "on"}, follow=True,
        )
        self.assertContains(response, "Only Pharmacy stock")
        self.assertFalse(StockCount.objects.exists())

    def test_owner_report_carries_the_stock_position(self):
        self.client.login(username=self.owner.username, password=self.password)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stock"]["stock_value_cost"], Decimal("400.00"))
        self.assertContains(response, "Sellable stock at cost")
        pdf = self.client.get(reverse("report_download_pdf"))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_deliveries_screen_is_closed_to_clinical_roles(self):
        self.client.login(username=self.clinician.username, password=self.password)
        self.assertEqual(self.client.get(reverse("deliveries")).status_code, 403)
        self.assertEqual(self.client.get(reverse("stock")).status_code, 403)



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ValuationCorrectnessTests(HospitalFixtureMixin, TestCase):
    """Numbers the owner would act on have to be right, or absent.

    Stock value counted expired and quarantined batches, reporting medicine
    that cannot legally leave the shelf as though it were a realisable asset.
    Product gross margin divided a billed sale by a zero cost whenever nothing
    had been dispensed, which reads on screen as a 100% margin.
    """

    def expired_batch(self, quantity=Decimal("50")):
        batch = StockBatch.objects.create(
            item=self.product, batch_number="B-EXPIRED",
            expiry_date=timezone.localdate() - timedelta(days=1),
            purchase_cost_per_base_unit=Decimal("2.00"),
        )
        StockMovement.objects.create(
            batch=batch, movement_type=StockMovement.MovementType.RECEIPT, quantity_delta=quantity,
            reference_type="Opening", reference_id="exp", idempotency_key="expired-open", entered_by=self.pharmacist,
        )
        return batch

    def test_expired_stock_is_excluded_from_the_value_on_the_shelf(self):
        before = stock_position()["stock_value_cost"]
        self.expired_batch(Decimal("50"))
        after = stock_position()
        self.assertEqual(after["stock_value_cost"], before, "Expired stock must not inflate sellable value.")
        self.assertEqual(after["stock_value_unsellable_cost"], Decimal("100.00"))
        self.assertEqual(after["stock_value_all_cost"], before + Decimal("100.00"))

    def test_quarantined_stock_is_excluded_from_the_value_on_the_shelf(self):
        before = stock_position()["stock_value_cost"]
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["status"])
        after = stock_position()
        self.assertEqual(after["stock_value_cost"], Decimal("0.00"))
        self.assertEqual(after["stock_value_unsellable_cost"], before)

    def test_retail_value_also_excludes_stock_that_cannot_be_sold(self):
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["status"])
        self.assertEqual(stock_position()["stock_value_retail"], Decimal("0.00"))

    def test_margin_is_unavailable_when_nothing_was_dispensed(self):
        order = self.prepare(10)
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=50,
                       method=Payment.Method.CASH, reference="", idempotency_key="margin-pay")
        activity = stock_activity(7)
        self.assertGreater(activity["product_sales_value"], Decimal("0.00"))
        self.assertEqual(activity["dispensed_units"], Decimal("0.000"))
        self.assertFalse(activity["margin_available"])
        self.assertIsNone(activity["product_gross_margin"], "A billed-but-undispensed sale is not a 100% margin.")

    def test_margin_is_reported_once_both_halves_exist(self):
        order = self.prepare(10)
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=50,
                       method=Payment.Method.CASH, reference="", idempotency_key="margin-pay-2")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="margin-dispense")
        activity = stock_activity(7)
        self.assertTrue(activity["margin_available"])
        # 10 tablets billed at 5.00 and costing 2.00 each.
        self.assertEqual(activity["product_sales_value"], Decimal("50.00"))
        self.assertEqual(activity["cost_of_goods_dispensed"], Decimal("20.00"))
        self.assertEqual(activity["product_gross_margin"], Decimal("30.00"))

    def test_owner_dashboard_keeps_to_six_kpi_cards(self):
        self.client.login(username=self.owner.username, password=self.password)
        html = self.client.get(reverse("dashboard")).content.decode()
        cards = html.count('class="kpi"') + html.count('class="kpi warning"')
        self.assertLessEqual(cards, 6, "The brief allows a maximum of six owner KPI cards.")

    def test_owner_can_still_reach_the_eye_clinic_without_its_kpi_card(self):
        self.client.login(username=self.owner.username, password=self.password)
        self.assertEqual(self.client.get(reverse("eye_clinic")).status_code, 200)
        self.assertContains(self.client.get(reverse("dashboard")), reverse("eye_clinic"))



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CustodyTests(HospitalFixtureMixin, TestCase):
    """Stock leaving the pharmacy for a ward.

    Before this workflow the transfer, consumption and return movement types
    were declared and never written by any code path, so medicine issued to a
    ward simply left no trace at all.
    """

    def issue(self, quantity=Decimal("20"), actor=None, **kwargs):
        return issue_to_department(
            actor=actor or self.pharmacist,
            department=kwargs.pop("department", "Maternity"),
            received_by_name=kwargs.pop("received_by_name", "Sister Achieng"),
            lines=[{"batch": self.batch, "quantity": quantity}],
            **kwargs,
        )

    def test_issuing_moves_custody_out_of_the_pharmacy(self):
        issue = self.issue(Decimal("20"))
        self.assertEqual(self.batch.quantity_on_hand, Decimal("180"), "Issued stock leaves the pharmacy balance.")
        movement = StockMovement.objects.get(movement_type=StockMovement.MovementType.TRANSFER)
        self.assertEqual(movement.quantity_delta, Decimal("-20.000"))
        self.assertEqual(movement.to_location, "Maternity")
        self.assertEqual(issue.outstanding_quantity, Decimal("20.000"))
        self.assertEqual(issue.status, DepartmentIssue.Status.OUTSTANDING)

    def test_administering_does_not_deduct_the_stock_a_second_time(self):
        issue = self.issue(Decimal("20"))
        line = issue.lines.get()
        account_for_issue(actor=self.nurse, issue_id=issue.pk, outcomes={line.pk: {"consumed": Decimal("20")}})
        issue.refresh_from_db()
        self.assertEqual(
            self.batch.quantity_on_hand, Decimal("180"),
            "Administration must not deduct stock that already left the pharmacy.",
        )
        self.assertEqual(issue.status, DepartmentIssue.Status.SETTLED)
        self.assertEqual(issue.outstanding_quantity, Decimal("0.000"))

    def test_returned_stock_comes_back_quarantined_not_sellable(self):
        issue = self.issue(Decimal("20"))
        line = issue.lines.get()
        account_for_issue(actor=self.nurse, issue_id=issue.pk, outcomes={line.pk: {"returned": Decimal("20")}})
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"), "Returned units re-enter the ledger.")
        self.assertEqual(self.batch.status, StockBatch.Status.QUARANTINE)
        self.assertFalse(self.batch.can_dispense, "Medicine that has been off the shelf is not silently sellable again.")
        self.assertTrue(StockMovement.objects.filter(movement_type=StockMovement.MovementType.RETURN).exists())

    def test_waste_is_raised_for_review(self):
        issue = self.issue(Decimal("20"))
        line = issue.lines.get()
        account_for_issue(actor=self.nurse, issue_id=issue.pk, outcomes={line.pk: {"wasted": Decimal("5")}})
        self.assertTrue(ExceptionRecord.objects.filter(category="departmental_waste").exists())
        issue.refresh_from_db()
        self.assertEqual(issue.outstanding_quantity, Decimal("15.000"))

    def test_partial_accounting_leaves_the_remainder_outstanding(self):
        issue = self.issue(Decimal("20"))
        line = issue.lines.get()
        account_for_issue(actor=self.nurse, issue_id=issue.pk, outcomes={line.pk: {"consumed": Decimal("8")}})
        issue.refresh_from_db()
        self.assertEqual(issue.outstanding_quantity, Decimal("12.000"))
        self.assertEqual(issue.status, DepartmentIssue.Status.OUTSTANDING)
        account_for_issue(actor=self.nurse, issue_id=issue.pk, outcomes={line.pk: {"consumed": Decimal("12")}})
        issue.refresh_from_db()
        self.assertEqual(issue.status, DepartmentIssue.Status.SETTLED)

    def test_cannot_account_for_more_than_is_outstanding(self):
        issue = self.issue(Decimal("20"))
        line = issue.lines.get()
        with self.assertRaisesMessage(ValidationError, "remain outstanding"):
            account_for_issue(actor=self.nurse, issue_id=issue.pk, outcomes={line.pk: {"consumed": Decimal("25")}})

    def test_cannot_issue_more_than_is_on_hand(self):
        with self.assertRaisesMessage(ValidationError, "are available"):
            self.issue(Decimal("500"))

    def test_cannot_issue_quarantined_stock(self):
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "cannot be issued"):
            self.issue(Decimal("5"))

    def test_only_pharmacy_may_issue(self):
        with self.assertRaisesMessage(ValidationError, "Only pharmacy staff"):
            self.issue(Decimal("5"), actor=self.nurse)

    def test_patient_specific_issue_must_name_the_patient(self):
        with self.assertRaisesMessage(ValidationError, "must name the patient"):
            self.issue(Decimal("5"), kind=DepartmentIssue.Kind.PATIENT)

    def test_hospital_stock_reconciles_across_custody_locations(self):
        self.issue(Decimal("20"))
        custody = departmental_custody()
        on_shelf = stock_position()["stock_value_cost"]
        # 180 on the shelf at 2.00 plus 20 in the ward at 2.00 is the original 200.
        self.assertEqual(custody["outstanding_value"], Decimal("40.00"))
        self.assertEqual(on_shelf + custody["outstanding_value"], Decimal("400.00"))



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WriteOffTests(HospitalFixtureMixin, TestCase):
    """Taking unusable stock off the balance, with somebody accountable for it."""

    def request(self, quantity=Decimal("10"), actor=None):
        return request_write_off(
            actor=actor or self.pharmacist, batch_id=self.batch.pk, quantity=quantity,
            reason=StockWriteOff.Reason.DAMAGED, narrative="Carton crushed in the store room.",
        )

    def test_requesting_alone_changes_no_balance(self):
        self.request(Decimal("10"))
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))

    def test_approval_removes_the_stock_and_records_who_authorised_it(self):
        write_off = self.request(Decimal("10"))
        review_write_off(actor=self.reviewer, write_off_id=write_off.pk, approve=True, review_notes="Damage seen.")
        write_off.refresh_from_db()
        self.assertEqual(write_off.status, StockWriteOff.Status.APPROVED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("190"))
        movement = StockMovement.objects.get(reference_type="StockWriteOff")
        self.assertEqual(movement.quantity_delta, Decimal("-10.000"))
        self.assertEqual(movement.entered_by, self.reviewer)
        self.assertTrue(ExceptionRecord.objects.filter(category="stock_write_off").exists())

    def test_rejection_changes_no_balance(self):
        write_off = self.request(Decimal("10"))
        review_write_off(actor=self.reviewer, write_off_id=write_off.pk, approve=False)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))
        self.assertFalse(StockMovement.objects.filter(reference_type="StockWriteOff").exists())

    def test_requester_cannot_approve_their_own_write_off(self):
        self.reviewer.staff_profile.role = Role.REVIEWER
        self.reviewer.staff_profile.save(update_fields=["role"])
        write_off = self.request(Decimal("10"), actor=self.pharmacist)
        self.pharmacist.staff_profile.role = Role.REVIEWER
        self.pharmacist.staff_profile.save(update_fields=["role"])
        with self.assertRaisesMessage(ValidationError, "cannot approve your own"):
            review_write_off(actor=self.pharmacist, write_off_id=write_off.pk, approve=True)

    def test_pending_requests_cannot_overdraw_the_batch(self):
        self.request(Decimal("150"))
        with self.assertRaisesMessage(ValidationError, "can still be written off"):
            self.request(Decimal("100"))

    def test_write_off_needs_an_explanation(self):
        with self.assertRaisesMessage(ValidationError, "Describe what happened"):
            request_write_off(
                actor=self.pharmacist, batch_id=self.batch.pk, quantity=Decimal("1"),
                reason=StockWriteOff.Reason.OTHER, narrative="   ",
            )

    def test_expired_stock_cannot_be_released_back_to_sellable(self):
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["expiry_date", "status"])
        with self.assertRaisesMessage(ValidationError, "Expired stock cannot be released"):
            set_batch_disposition(
                actor=self.reviewer, batch_id=self.batch.pk,
                status=StockBatch.Status.ACTIVE, reason="Looks fine",
            )

    def test_reviewer_can_release_a_quarantined_batch_with_a_reason(self):
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["status"])
        set_batch_disposition(
            actor=self.reviewer, batch_id=self.batch.pk,
            status=StockBatch.Status.ACTIVE, reason="Seal intact, pharmacist inspected.",
        )
        self.batch.refresh_from_db()
        self.assertTrue(self.batch.can_dispense)
        self.assertTrue(AuditEvent.objects.filter(action="stock_batch.disposition").exists())

    def test_shrinkage_separates_unexplained_loss_from_authorised_write_offs(self):
        write_off = self.request(Decimal("10"))
        review_write_off(actor=self.reviewer, write_off_id=write_off.pk, approve=True)
        count = open_stock_count(actor=self.pharmacist)
        line = count.lines.get(batch=self.batch)
        submit_stock_count(actor=self.pharmacist, count_id=count.pk, counted={line.pk: line.expected_quantity - Decimal("6")})
        review_stock_count(actor=self.reviewer, count_id=count.pk, approve=True)
        result = shrinkage(90)
        self.assertEqual(result["loss_value"], Decimal("12.00"), "Six units at 2.00 is the unexplained loss.")
        self.assertEqual(result["written_off_value"], Decimal("20.00"), "The authorised write-off is not shrinkage.")
        self.assertTrue(result["measured"])
