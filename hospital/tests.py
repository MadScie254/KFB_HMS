import os
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .analytics import (
    control_adoption,
    departmental_custody,
    shrinkage,
    stock_activity,
    stock_position,
    supplier_price_history,
)
from .models import (
    AuditEvent,
    CashShift,
    CatalogueItem,
    ClinicalNote,
    CreditNote,
    DepartmentIssue,
    Encounter,
    ExceptionRecord,
    EyeCase,
    GoodsReceipt,
    ImportJob,
    Invoice,
    InvoiceLine,
    Patient,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    PriceVersion,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    ServiceOrder,
    StockBatch,
    StockCount,
    StockMovement,
    StockWriteOff,
    Supplier,
)
from .permissions import user_role
from .services import (
    account_for_issue,
    approve_credit_note,
    approve_purchase_order,
    check_delivery,
    complete_eye_case,
    dispense_order,
    issue_to_department,
    open_stock_count,
    prepare_pharmacy_order,
    receive_delivery,
    record_payment,
    request_write_off,
    review_stock_count,
    review_write_off,
    set_batch_disposition,
    submit_stock_count,
)
from .views import owner_brief_context


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


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkflowTests(HospitalFixtureMixin, TestCase):

    def test_walk_in_payment_and_dispense_reconcile(self):
        order = self.prepare(15)
        self.assertEqual(order.status, PharmacyOrder.Status.PREPARED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))
        payment = record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=75, method=Payment.Method.CASH, reference="", idempotency_key="pay-1")
        order.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.CLEARED)
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="dispense-1")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="dispense-1")
        order.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.DISPENSED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("185"))
        self.assertEqual(payment.amount, order.invoice.total)
        self.assertEqual(StockMovement.objects.filter(reference_id=str(order.pk), movement_type="dispense").count(), 1)

    def test_cashier_cannot_dispense_and_pharmacy_cannot_collect(self):
        order = self.prepare(1)
        with self.assertRaisesMessage(ValidationError, "reception/cashier"):
            record_payment(actor=self.pharmacist, invoice_id=order.invoice_id, amount=5, method="cash", reference="", idempotency_key="wrong-role-pay")
        with self.assertRaisesMessage(ValidationError, "pharmacy staff"):
            dispense_order(actor=self.reception, order_id=order.pk, idempotency_key="wrong-role-dispense")

    def test_two_orders_cannot_both_use_last_stock(self):
        first = self.prepare(200)
        record_payment(actor=self.reception, invoice_id=first.invoice_id, amount=1000, method="cash", reference="", idempotency_key="pay-first")
        dispense_order(actor=self.pharmacist, order_id=first.pk, idempotency_key="disp-first")
        second = self.prepare(1)
        record_payment(actor=self.reception, invoice_id=second.invoice_id, amount=5, method="cash", reference="", idempotency_key="pay-second")
        with self.assertRaisesMessage(ValidationError, "Insufficient valid stock"):
            dispense_order(actor=self.pharmacist, order_id=second.pk, idempotency_key="disp-second")

    def test_expired_and_quarantined_batches_are_not_dispensed(self):
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.save(update_fields=["expiry_date"])
        order = self.prepare(1)
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=5, method="cash", reference="", idempotency_key="pay-expired")
        with self.assertRaises(ValidationError):
            dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="disp-expired")
        self.batch.expiry_date = timezone.localdate() + timedelta(days=10)
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["expiry_date", "status"])
        with self.assertRaises(ValidationError):
            dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="disp-quarantine")

    def test_partial_invoice_balance_and_unapplied_deposit(self):
        invoice = Invoice.objects.create(patient=self.patient, status=Invoice.Status.POSTED, posted_at=timezone.now(), created_by=self.reception)
        InvoiceLine.objects.create(invoice=invoice, item=self.product, description="Inpatient charge", department="Inpatient", quantity=1, unit_price=10000)
        payment = Payment.objects.create(amount=5000, method="cash", received_by=self.reception, shift=self.shift, idempotency_key="deposit-test")
        PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=4000, allocated_by=self.reception)
        self.assertEqual(invoice.balance, Decimal("6000"))
        self.assertEqual(payment.unapplied_amount, Decimal("1000"))

    def test_cash_shift_equation_excludes_mpesa(self):
        Payment.objects.create(amount=8000, method="cash", received_by=self.reception, shift=self.shift, idempotency_key="cash-shift-test")
        Payment.objects.create(amount=5000, method="mpesa", reference="MPESA-UNIQUE", verification_status="unverified", received_by=self.reception, shift=self.shift, idempotency_key="mpesa-shift-test")
        self.shift.cash_refunds = 500
        self.shift.transfers_out = 6000
        self.shift.actual_cash = 2300
        self.shift.closed_at = timezone.now()
        self.shift.save()
        self.assertEqual(self.shift.expected_cash, Decimal("2500"))
        self.assertEqual(self.shift.variance, Decimal("-200"))

    def test_duplicate_mpesa_reference_is_rejected(self):
        order1 = self.prepare(1)
        record_payment(actor=self.reception, invoice_id=order1.invoice_id, amount=5, method="mpesa", reference="QAA123", idempotency_key="mpesa-1")
        order2 = self.prepare(1)
        with self.assertRaises(ValidationError):
            record_payment(actor=self.reception, invoice_id=order2.invoice_id, amount=5, method="mpesa", reference="QAA123", idempotency_key="mpesa-2")

    def test_refund_credit_requires_independent_reviewer(self):
        order = self.prepare(2)
        note = CreditNote.objects.create(invoice=order.invoice, amount=5, reason="Test correction", requested_by=self.reception)
        with self.assertRaises(ValidationError):
            approve_credit_note(actor=self.reception, credit_note_id=note.pk, approve=True)
        approve_credit_note(actor=self.reviewer, credit_note_id=note.pk, approve=True)
        note.refresh_from_db()
        self.assertEqual(note.status, CreditNote.Status.APPROVED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"), "Financial credit must not return stock")

    def test_bilateral_case_accrues_one_case_fee(self):
        case = EyeCase.objects.create(patient=self.patient, proposed_procedure="Unspecified eye procedure", eye="both", readiness="ready", package_price=24000)
        complete_eye_case(actor=self.clinician, case_id=case.pk)
        complete_eye_case(actor=self.clinician, case_id=case.pk)
        self.assertEqual(case.payable.amount, Decimal("2000"))
        self.assertEqual(type(case).objects.get(pk=case.pk).payable.amount, Decimal("2000"))

    def test_purchase_requester_cannot_self_approve(self):
        supplier = Supplier.objects.create(name="Demo Supplier")
        po = PurchaseOrder.objects.create(supplier=supplier, requested_by=self.procurement)
        with self.assertRaises(ValidationError):
            approve_purchase_order(actor=self.procurement, order_id=po.pk)
        approve_purchase_order(actor=self.reviewer, order_id=po.pk)
        po.refresh_from_db()
        self.assertEqual(po.status, "approved")

    def test_signed_note_cannot_be_signed_twice(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        note = ClinicalNote.objects.create(encounter=encounter, author=self.clinician, assessment="Test")
        note.sign()
        with self.assertRaises(ValidationError):
            note.sign()

    def test_cashier_cannot_open_clinical_note_endpoint(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        self.client.login(username=self.reception.username, password=self.password)
        response = self.client.get(reverse("clinical_note", kwargs={"encounter_id": encounter.pk}))
        self.assertEqual(response.status_code, 403)

    def test_product_csv_dry_run_and_commit_are_idempotent(self):
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"code,name,department,base_unit,sale_unit,units_per_sale_unit,sale_price,reorder_level,prescription_required\n"
            b"CSV-001,CSV test product,Pharmacy,tablet,box,100,7.50,20,false\n"
        )
        response = self.client.post(reverse("csv_import"), {"import_kind": "products", "csv_file": SimpleUploadedFile("products.csv", csv_bytes, content_type="text/csv")})
        self.assertEqual(response.status_code, 200)
        job = ImportJob.objects.get(filename="products.csv")
        self.assertEqual(job.error_count, 0)
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        self.assertEqual(CatalogueItem.objects.filter(code="CSV-001").count(), 1)

    def test_service_result_requires_content_before_release(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        service = CatalogueItem.objects.create(code="LAB-T", name="Lab test", kind="service", department="Laboratory", base_unit="service", sale_unit="service", units_per_sale_unit=1)
        order = ServiceOrder.objects.create(encounter=encounter, service=service, requested_by=self.clinician)
        lab = self.make_user("lab", Role.LAB)
        self.client.login(username=lab.username, password=self.password)
        response = self.client.post(reverse("service_order_update", kwargs={"pk": order.pk}), {"status": "released", "result": ""})
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.Status.REQUESTED)
        response = self.client.post(reverse("service_order_update", kwargs={"pk": order.pk}), {"status": "released", "result": "Fictional demonstration result"})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.Status.RELEASED)
        self.assertIsNotNone(order.released_at)

    def test_outpatient_prescription_prices_then_dispenses_actual_quantity(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception, status=Encounter.Status.CLINICIAN)
        self.client.login(username=self.clinician.username, password=self.password)
        response = self.client.post(reverse("prescription_create", kwargs={"encounter_id": encounter.pk}), {
            "product": self.product.pk, "strength": "As labelled", "dose": "Clinician-entered dose",
            "route": "Clinician-entered route", "frequency": "Clinician-entered frequency",
            "duration": "Clinician-entered duration", "quantity_base_units": "3", "instructions": "Fictional test only",
        })
        self.assertEqual(response.status_code, 302)
        prescription = encounter.prescriptions.get()
        self.client.logout()
        self.client.login(username=self.pharmacist.username, password=self.password)
        response = self.client.post(reverse("pharmacy_prepare_prescription", kwargs={"prescription_id": prescription.pk}))
        self.assertEqual(response.status_code, 302)
        order = PharmacyOrder.objects.get(prescription=prescription)
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=15, method="cash", reference="", idempotency_key="rx-payment")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="rx-dispense")
        prescription.items.get().refresh_from_db()
        self.assertEqual(prescription.items.get().dispensed_quantity, Decimal("3"))
        self.assertEqual(self.batch.quantity_on_hand, Decimal("197"))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RegressionTests(HospitalFixtureMixin, TestCase):
    """Regressions for defects found in the September 2026 audit.

    Each test fails against the pre-audit code; see docs/AUDIT_2026-09-18.md.
    """

    # C1 — the lock screen was a no-op: the middleware read resolver_match
    # before URL resolution, and unlocked_required was applied to no view.
    def test_locked_session_cannot_reach_clinical_screens(self):
        self.client.login(username=self.clinician.username, password=self.password)
        profile = self.clinician.staff_profile
        profile.locked_at = timezone.now()
        profile.save(update_fields=["locked_at"])
        response = self.client.get(reverse("queue"))
        self.assertRedirects(response, reverse("screen_unlock"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("screen_unlock")).status_code, 200)

    def test_unlocking_restores_access(self):
        self.client.login(username=self.clinician.username, password=self.password)
        profile = self.clinician.staff_profile
        profile.locked_at = timezone.now()
        profile.save(update_fields=["locked_at"])
        self.client.post(reverse("screen_unlock"), {"password": self.password})
        self.assertEqual(self.client.get(reverse("queue")).status_code, 200)

    # C2 — two lines for the same product each read the untouched batch
    # balance, so one order could dispense more than the hospital held.
    def test_repeated_product_lines_cannot_overdraw_a_batch(self):
        order = prepare_pharmacy_order(
            actor=self.pharmacist, customer_name="Walk-in", patient=None,
            items=[(self.product, Decimal("150")), (self.product, Decimal("150"))])
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=Decimal("1500"),
                       method="cash", reference="", idempotency_key="overdraw-pay")
        with self.assertRaisesMessage(ValidationError, "Insufficient valid stock"):
            dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="overdraw-disp")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))

    def test_repeated_product_lines_within_stock_still_dispense(self):
        order = prepare_pharmacy_order(
            actor=self.pharmacist, customer_name="Walk-in", patient=None,
            items=[(self.product, Decimal("60")), (self.product, Decimal("60"))])
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=Decimal("600"),
                       method="cash", reference="", idempotency_key="split-pay")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="split-disp")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, Decimal("80"))

    # C3 — order_by("urgency") sorts alphabetically, placing urgent after routine.
    def test_queue_ranks_urgent_above_routine(self):
        for urgency in ["routine", "urgent", "emergency"]:
            Encounter.objects.create(
                patient=self.patient, started_by=self.reception, urgency=urgency,
                emergency_override_reason="Documented" if urgency == "emergency" else "")
        self.client.login(username=self.clinician.username, password=self.password)
        listed = self.client.get(reverse("queue")).context["encounters"]
        self.assertEqual([e.urgency for e in listed], ["emergency", "urgent", "routine"])

    # C4 — save() guarded the audit trail; the ORM bulk paths did not.
    def test_audit_events_reject_bulk_update_and_delete(self):
        from .models import AuditEvent
        event = AuditEvent.objects.create(actor=self.owner, action="probe", entity_type="Test", entity_id="1")
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            event.delete()
        event.refresh_from_db()
        self.assertEqual(event.action, "probe")

    # C5 — unlimited password attempts against an unauthenticated endpoint.
    def test_repeated_failed_logins_lock_the_username(self):
        from .models import LoginAttempt
        for _ in range(LoginAttempt.LOCKOUT_THRESHOLD):
            self.client.post(reverse("login"), {"username": "clinician", "password": "wrong"})
        blocked = self.client.post(reverse("login"), {"username": "clinician", "password": "wrong"})
        self.assertEqual(blocked.status_code, 429)
        correct = self.client.post(reverse("login"), {"username": "clinician", "password": self.password})
        self.assertEqual(correct.status_code, 429, "A locked username must not fall through on a correct password")

    def test_successful_login_clears_earlier_failures(self):
        from .models import LoginAttempt
        self.client.post(reverse("login"), {"username": "clinician", "password": "wrong"})
        self.client.post(reverse("login"), {"username": "clinician", "password": self.password})
        self.assertEqual(LoginAttempt.recent_failures("clinician"), 0)

    # C6 — reception could not find a patient by the identifier on their ID card.
    def test_patient_search_matches_national_id(self):
        Patient.objects.create(first_name="Aisha", last_name="Wafula", estimated_age_years=41,
                               id_number="24681012", registered_by=self.reception)
        self.client.login(username=self.reception.username, password=self.password)
        response = self.client.get(reverse("patients"), {"q": "24681012"})
        self.assertContains(response, "Aisha")

    # C7 — one aggregate query per batch and two per open invoice, so the
    # owner dashboard and stock ledger got slower as the hospital used them.
    def _seed_ledger(self, batches, orders, tag):
        for index in range(batches):
            batch = StockBatch.objects.create(
                item=self.product, batch_number=f"{tag}-{index}",
                expiry_date=timezone.localdate() + timedelta(days=200),
                purchase_cost_per_base_unit=Decimal("1.00"))
            StockMovement.objects.create(
                batch=batch, movement_type="receipt", quantity_delta=5, reference_type="Opening",
                reference_id="1", idempotency_key=f"{tag}-{index}", entered_by=self.pharmacist)
        for index in range(orders):
            prepare_pharmacy_order(actor=self.pharmacist, customer_name=f"{tag} {index}",
                                   patient=None, items=[(self.product, Decimal("1"))])

    def _count_queries(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        return len(captured)

    def test_owner_dashboard_cost_does_not_grow_with_the_ledger(self):
        self.client.login(username=self.owner.username, password=self.password)
        self._seed_ledger(5, 3, "small")
        small = self._count_queries(reverse("dashboard"))
        self._seed_ledger(40, 25, "large")
        large = self._count_queries(reverse("dashboard"))
        self.assertEqual(small, large, "Dashboard query count must not scale with rows")
        self.assertLess(large, 30)

    def test_stock_page_cost_does_not_grow_with_the_ledger(self):
        self.client.login(username=self.pharmacist.username, password=self.password)
        self._seed_ledger(5, 0, "stock-small")
        small = self._count_queries(reverse("stock"))
        self._seed_ledger(40, 0, "stock-large")
        large = self._count_queries(reverse("stock"))
        self.assertEqual(small, large, "Stock ledger query count must not scale with batches")
        self.assertLess(large, 15)

    def _seed_orders(self, count, tag):
        supplier, _ = Supplier.objects.get_or_create(name="Query Supplies")
        for index in range(count):
            order = PurchaseOrder.objects.create(
                supplier=supplier, requested_by=self.procurement, reference=f"{tag}-{index}"
            )
            PurchaseOrderLine.objects.create(
                order=order, item=self.product,
                quantity_base_units=Decimal("10"), quoted_unit_cost=Decimal("1.00"),
            )

    def test_purchasing_page_cost_does_not_grow_with_the_order_book(self):
        # The page prints the requester's and approver's staff profiles per row.
        # Neither was on the select_related chain, so every purchase order cost
        # two extra queries.
        self.client.login(username=self.owner.username, password=self.password)
        self._seed_orders(3, "small")
        small = self._count_queries(reverse("purchasing"))
        self._seed_orders(25, "large")
        large = self._count_queries(reverse("purchasing"))
        self.assertEqual(small, large, "Purchasing query count must not scale with orders")
        self.assertLess(large, 15)

    def test_deliveries_page_cost_does_not_grow_with_the_receipt_history(self):
        self.client.login(username=self.procurement.username, password=self.password)
        self._seed_orders(3, "deliveries-small")
        small = self._count_queries(reverse("deliveries"))
        self._seed_orders(25, "deliveries-large")
        large = self._count_queries(reverse("deliveries"))
        self.assertEqual(small, large, "Deliveries query count must not scale with orders")
        self.assertLess(large, 20)


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
        return SimpleUploadedFile(name, b"fake-jpeg-bytes", content_type="image/jpeg")

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

    def post_delivery(self, reference="SUP-INV-100"):
        return self.client.post(
            reverse("goods_receipt_create", args=[self.order.pk]),
            {
                "supplier_invoice_reference": reference,
                "invoice_amount": "200.00",
                "invoice_date": timezone.localdate().isoformat(),
                "delivered_on": timezone.localdate().isoformat(),
                "invoice_photo": SimpleUploadedFile("invoice.jpg", b"fake-jpeg-bytes", content_type="image/jpeg"),
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
class DemoSeedTests(TestCase):
    """The documented quick start must actually run.

    ``seed_demo`` assigned roles to a freshly fetched StaffProfile while the
    User instance it kept still carried the default-role profile the post_save
    signal had attached. Every later role check read the stale copy, so the
    command aborted partway through and the demo environment could not be
    created at all.
    """

    def test_demo_seed_completes_and_assigns_roles(self):
        call_command("seed_demo")
        pharmacist = User.objects.get(username="pharmacy.demo")
        self.assertEqual(user_role(pharmacist), Role.PHARMACY)
        self.assertEqual(user_role(User.objects.get(username="owner.demo")), Role.OWNER)
        self.assertTrue(PharmacyOrder.objects.filter(customer_name="Demo Walk-In Customer").exists())

    def test_demo_seed_is_idempotent(self):
        call_command("seed_demo")
        call_command("seed_demo")
        self.assertEqual(User.objects.filter(username="pharmacy.demo").count(), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(reference_id="DEMO-WITNESSED-001").count(), 3)

    def test_demo_seed_leaves_an_approved_order_ready_to_receive(self):
        call_command("seed_demo")
        order = PurchaseOrder.objects.get()
        self.assertEqual(order.status, "approved")
        self.assertNotEqual(order.approved_by_id, order.requested_by_id)
        self.assertEqual(order.lines.count(), 2)


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
        with self.assertRaisesMessage(ValidationError, "are on hand"):
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
                invoice_photo=SimpleUploadedFile("i.jpg", b"x", content_type="image/jpeg"),
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


class ContinuousIntegrationTests(SimpleTestCase):
    """The local check script has to stay honest about what CI runs.

    Every GitHub Actions run in this repository has failed within seconds
    without a runner, because the account is billing-locked; no commit can
    clear that. While it holds, scripts/checks.sh is the only way anyone can
    verify a change, so it must not drift away from the workflow it stands in
    for.
    """

    workflow = Path(settings.BASE_DIR) / ".github" / "workflows" / "quality.yml"
    script = Path(settings.BASE_DIR) / "scripts" / "checks.sh"

    def workflow_commands(self):
        commands = []
        for line in self.workflow.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("- run:"):
                command = stripped[len("- run:"):].strip()
                if command.startswith("python -m pip install"):
                    continue  # Dependency installation, not a check.
                commands.append(command)
        return commands

    def test_the_workflow_still_runs_the_checks_we_think_it_does(self):
        commands = self.workflow_commands()
        self.assertEqual(len(commands), 4, f"Unexpected CI step count: {commands}")

    def test_every_ci_check_is_reproducible_locally(self):
        script = self.script.read_text()
        for command in self.workflow_commands():
            # Compare the distinguishing part; the script sets env vars its own way.
            core = command.split("python manage.py ")[-1] if "manage.py" in command else command
            core = core.replace("ruff check", "ruff check")
            needle = core.split(" ")[0] if core else command
            self.assertIn(
                needle, script,
                f"CI runs {command!r} but scripts/checks.sh has no matching step; the two have drifted.",
            )

    def test_the_check_script_is_executable(self):
        self.assertTrue(self.script.exists(), "scripts/checks.sh is missing.")
        self.assertTrue(os.access(self.script, os.X_OK), "scripts/checks.sh must be executable.")
