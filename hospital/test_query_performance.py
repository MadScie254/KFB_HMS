"""Bound query counts and preserve database-side summaries."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .analytics import stock_activity, stock_position, supplier_price_history, with_invoice_financials
from .models import (
    CatalogueItem,
    ClinicalNote,
    CreditNote,
    Encounter,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    PriceVersion,
    PurchaseOrder,
    PurchaseOrderLine,
    Refund,
    StockMovement,
    Supplier,
)
from .pdf_reports import build_patient_access_pdf
from .services import prepare_pharmacy_order
from .test_support import HospitalFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InvoiceListPerformanceTests(HospitalFixtureMixin, TestCase):
    def test_order_and_report_list_query_counts_do_not_grow_with_rows(self):
        first = Invoice.objects.create(
            patient=self.patient, created_by=self.reception,
            status=Invoice.Status.POSTED, posted_at=timezone.now(),
        )
        InvoiceLine.objects.create(
            invoice=first, item=self.product, description="Supply", department="Pharmacy",
            quantity=1, unit_price=Decimal("5.00"),
        )
        PharmacyOrder.objects.create(
            patient=self.patient, invoice=first, prepared_by=self.pharmacist,
        )
        self.client.force_login(self.pharmacist)
        with CaptureQueriesContext(connection) as one_order_queries:
            self.assertEqual(self.client.get(reverse("pharmacy_orders")).status_code, 200)
        self.client.force_login(self.reviewer)
        with CaptureQueriesContext(connection) as one_report_queries:
            self.assertEqual(self.client.get(reverse("reports")).status_code, 200)

        invoices = [
            Invoice(
                invoice_number=f"INV-VOLUME-{index:03}", patient=self.patient,
                created_by=self.reception, status=Invoice.Status.POSTED, posted_at=timezone.now(),
            ) for index in range(99)
        ]
        Invoice.objects.bulk_create(invoices)
        InvoiceLine.objects.bulk_create([
            InvoiceLine(
                invoice=invoice, item=self.product, description="Supply", department="Pharmacy",
                quantity=1, unit_price=Decimal("5.00"), line_total=Decimal("5.00"),
            ) for invoice in invoices
        ])
        PharmacyOrder.objects.bulk_create([
            PharmacyOrder(
                order_number=f"RX-VOLUME-{index:03}", patient=self.patient,
                invoice=invoice, prepared_by=self.pharmacist,
            ) for index, invoice in enumerate(invoices)
        ])
        self.client.force_login(self.pharmacist)
        with CaptureQueriesContext(connection) as many_order_queries:
            orders_response = self.client.get(reverse("pharmacy_orders"))
        self.assertEqual(orders_response.status_code, 200)
        self.assertContains(orders_response, "RX-VOLUME-098")
        self.assertLessEqual(len(many_order_queries) - len(one_order_queries), 2)
        self.client.force_login(self.reviewer)
        with CaptureQueriesContext(connection) as many_report_queries:
            report_response = self.client.get(reverse("reports"))
        self.assertEqual(report_response.status_code, 200)
        self.assertLessEqual(len(many_report_queries) - len(one_report_queries), 2)

    def test_annotated_totals_match_ledger_and_use_one_query_for_many_invoices(self):
        invoice = Invoice.objects.create(
            patient=self.patient, created_by=self.reception, status=Invoice.Status.POSTED,
            posted_at=timezone.now(),
        )
        InvoiceLine.objects.create(
            invoice=invoice, item=self.product, description="Supply", department="Pharmacy",
            quantity=1, unit_price=Decimal("100.00"),
        )
        cash = Payment.objects.create(
            amount=Decimal("30.00"), method=Payment.Method.CASH,
            received_by=self.reception, idempotency_key="list-cash",
        )
        PaymentAllocation.objects.create(
            invoice=invoice, payment=cash, amount=Decimal("30.00"), allocated_by=self.reception,
        )
        pending = Payment.objects.create(
            amount=Decimal("20.00"), method=Payment.Method.MPESA,
            reference="LIST-MPESA", verification_status=Payment.Verification.UNVERIFIED,
            received_by=self.reception, idempotency_key="list-pending",
        )
        PaymentAllocation.objects.create(
            invoice=invoice, payment=pending, amount=Decimal("20.00"), allocated_by=self.reception,
        )
        CreditNote.objects.create(
            invoice=invoice, amount=Decimal("10.00"), reason="Correction",
            status=CreditNote.Status.APPROVED, requested_by=self.reception,
        )
        Refund.objects.create(
            payment=cash, invoice=invoice, amount=Decimal("5.00"), reason="Return",
            status=Refund.Status.PAID, requested_by=self.reception,
        )
        self.assertEqual((invoice.total, invoice.paid_amount, invoice.balance), (
            Decimal("100.00"), Decimal("25.00"), Decimal("65.00"),
        ))
        Invoice.objects.bulk_create([
            Invoice(
                invoice_number=f"INV-LIST-{index:03}", patient=self.patient,
                created_by=self.reception, status=Invoice.Status.POSTED, posted_at=timezone.now(),
            ) for index in range(100)
        ])
        with self.assertNumQueries(1):
            listed = list(with_invoice_financials(Invoice.objects.all()))
            figures = [(row.total, row.paid_amount, row.balance) for row in listed]
        self.assertEqual(len(figures), 101)
        annotated = next(row for row in listed if row.pk == invoice.pk)
        self.assertEqual((annotated.total, annotated.paid_amount, annotated.balance), (
            Decimal("100.00"), Decimal("25.00"), Decimal("65.00"),
        ))



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RepeatedQueryRegressionTests(HospitalFixtureMixin, TestCase):
    def test_stock_position_reads_effective_prices_once(self):
        with CaptureQueriesContext(connection) as queries:
            position = stock_position()
        price_queries = [
            query["sql"] for query in queries if "hospital_priceversion" in query["sql"].lower()
        ]
        self.assertEqual(len(price_queries), 1)
        self.assertEqual(position["stock_value_retail"], Decimal("1000.00"))

    def test_many_pharmacy_lines_use_one_price_lookup(self):
        products = [self.product]
        for index in range(10):
            item = CatalogueItem.objects.create(
                code=f"BASKET-{index}", name=f"Basket product {index}",
                kind=CatalogueItem.Kind.PRODUCT, department="Pharmacy",
                base_unit="item", sale_unit="item", units_per_sale_unit=1,
                reorder_level=0,
            )
            PriceVersion.objects.create(item=item, amount=Decimal("7.00"), reason="Test", approved_by=self.owner)
            products.append(item)
        with CaptureQueriesContext(connection) as queries:
            order = prepare_pharmacy_order(
                actor=self.pharmacist, customer_name="Many lines", patient=None,
                items=[(item, Decimal("1")) for item in products],
            )
        price_queries = [
            query["sql"] for query in queries if "hospital_priceversion" in query["sql"].lower()
        ]
        self.assertEqual(len(price_queries), 1)
        self.assertEqual(order.invoice.total, Decimal("75.00"))

    def test_patient_access_pdf_fetches_visits_and_signed_notes_in_two_queries(self):
        for index in range(4):
            encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
            ClinicalNote.objects.create(
                encounter=encounter, author=self.clinician, status=ClinicalNote.Status.SIGNED,
                assessment=f"Assessment {index}", plan="Follow up", signed_at=timezone.now(),
            )
        with CaptureQueriesContext(connection) as queries:
            pdf = build_patient_access_pdf(
                patient=self.patient, hospital_name="Test hospital", generated_by="Reviewer",
            )
        related_queries = [
            query["sql"] for query in queries
            if "hospital_encounter" in query["sql"].lower()
            or "hospital_clinicalnote" in query["sql"].lower()
        ]
        self.assertEqual(len(related_queries), 2)
        self.assertTrue(pdf.startswith(b"%PDF"))



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StockActivityPerformanceTests(HospitalFixtureMixin, TestCase):
    def test_period_summary_query_count_stays_flat_with_movement_history(self):
        with CaptureQueriesContext(connection) as small_queries:
            stock_activity(7)
        StockMovement.objects.bulk_create([
            StockMovement(
                batch=self.batch, movement_type=StockMovement.MovementType.RECEIPT,
                quantity_delta=1, unit_cost_at_event=Decimal("0.123456"),
                to_location="Pharmacy", reference_type="Volume", reference_id=str(index),
                idempotency_key=f"activity-volume-{index}", entered_by=self.pharmacist,
            ) for index in range(999)
        ])
        with CaptureQueriesContext(connection) as many_queries:
            summary = stock_activity(7)
        self.assertEqual(summary["movement_count"], 1000)
        self.assertEqual(summary["received_value"], Decimal("519.88"))
        self.assertLessEqual(len(many_queries) - len(small_queries), 1)



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SupplierHistoryPerformanceTests(HospitalFixtureMixin, TestCase):
    def test_database_selects_only_top_price_changes_before_loading_details(self):
        supplier = Supplier.objects.create(name="Price volume supplier")
        order = PurchaseOrder.objects.create(
            supplier=supplier, requested_by=self.procurement,
        )
        products = CatalogueItem.objects.bulk_create([
            CatalogueItem(
                code=f"PRICE-{index:02}", name=f"Price product {index:02}",
                kind=CatalogueItem.Kind.PRODUCT, department="Pharmacy",
                base_unit="item", sale_unit="item", units_per_sale_unit=1,
                reorder_level=1,
            ) for index in range(1, 14)
        ])
        order_lines = PurchaseOrderLine.objects.bulk_create([
            PurchaseOrderLine(
                order=order, item=item, quantity_base_units=2,
                quoted_unit_cost=Decimal("1.00"),
            ) for item in products
        ])
        earlier = GoodsReceipt.objects.create(
            purchase_order=order, supplier_invoice_reference="PRICE-EARLY",
            invoice_amount=Decimal("13.00"), received_by=self.procurement,
            delivered_at=timezone.now() - timedelta(days=2),
        )
        later = GoodsReceipt.objects.create(
            purchase_order=order, supplier_invoice_reference="PRICE-LATE",
            invoice_amount=Decimal("13.00"), received_by=self.procurement,
            delivered_at=timezone.now() - timedelta(days=1),
        )
        GoodsReceiptLine.objects.bulk_create([
            GoodsReceiptLine(
                receipt=receipt, order_line=line, quantity_received=1,
                batch_number=f"{receipt.pk}-{index}",
                actual_unit_cost=Decimal("1.00") if receipt == earlier
                else Decimal("1.00") + Decimal(index) / 100,
            )
            for index, line in enumerate(order_lines, start=1)
            for receipt in (earlier, later)
        ])
        with CaptureQueriesContext(connection) as queries:
            history = supplier_price_history(365, limit=5)
        self.assertEqual(len(history["rows"]), 5)
        self.assertEqual(history["rows"][0]["item"].code, "PRICE-13")
        self.assertEqual(history["rows"][0]["percent_change"], Decimal("13.00"))
        self.assertTrue(any("LIMIT 5" in query["sql"].upper() for query in queries))
