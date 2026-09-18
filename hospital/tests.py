from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    CashShift,
    CatalogueItem,
    ClinicalNote,
    CreditNote,
    Encounter,
    EyeCase,
    ImportJob,
    Invoice,
    InvoiceLine,
    Patient,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    PriceVersion,
    PurchaseOrder,
    Role,
    ServiceOrder,
    StockBatch,
    StockMovement,
    Supplier,
)
from .services import (
    approve_credit_note,
    approve_purchase_order,
    complete_eye_case,
    dispense_order,
    prepare_pharmacy_order,
    record_payment,
)


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
