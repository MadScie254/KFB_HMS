"""Worklist bounds and query behavior with histories larger than one page."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .analytics import departmental_custody
from .models import (
    CatalogueItem,
    ClinicalAttachment,
    ClinicalNote,
    DepartmentIssue,
    DepartmentIssueLine,
    Encounter,
    Invoice,
    PurchaseOrder,
    PurchaseOrderLine,
    ServiceOrder,
    StockBatch,
    Supplier,
)
from .test_support import HospitalFixtureMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorklistPaginationTests(HospitalFixtureMixin, TestCase):
    def measured_get(self, url, params=None):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, params or {})
        self.assertEqual(response.status_code, 200)
        return response, len(queries)

    def add_patient_history(self, count, start=0):
        for index in range(start, start + count):
            encounter = Encounter.objects.create(
                patient=self.patient, started_by=self.reception,
                status=Encounter.Status.CLOSED if index % 2 else Encounter.Status.TRIAGE,
            )
            ClinicalNote.objects.create(
                encounter=encounter, author=self.clinician, assessment=f"Assessment-{index}",
            )
            ClinicalAttachment.objects.create(
                patient=self.patient, encounter=encounter,
                file=f"clinical/example-{index}.txt", original_name=f"Scan-{index}.txt",
                uploaded_by=self.clinician,
            )
            Invoice.objects.create(patient=self.patient, encounter=encounter, created_by=self.reception)

    def test_patient_detail_bounds_every_history_and_preserves_other_page_positions(self):
        self.client.force_login(self.owner)
        self.add_patient_history(48)
        url = reverse("patient_detail", args=[self.patient.pk])
        first, first_queries = self.measured_get(url)
        self.assertEqual(len(first.context["active_encounters"]), 10)
        self.assertEqual(len(first.context["encounter_history"]), 20)
        self.assertEqual(len(first.context["notes"]), 20)
        self.assertEqual(len(first.context["attachments"]), 20)
        self.assertEqual(len(first.context["invoices"]), 20)
        self.assertNotContains(first, "Assessment-0")
        self.assertNotContains(first, "Scan-0.txt")

        later = self.client.get(url, {"active_page": "2", "note_page": "3"})
        self.assertEqual(later.context["active_encounters"].number, 2)
        self.assertEqual(later.context["notes"].number, 3)
        self.assertIn("active_page=2", later.context["note_links"]["previous"])
        self.assertContains(later, "Assessment-0")

        self.add_patient_history(48, start=48)
        grown, grown_queries = self.measured_get(url)
        self.assertLessEqual(grown_queries, first_queries + 1)
        self.assertLess(len(grown.content), len(first.content) + 2000)

    def test_queue_and_department_worklists_page_active_and_released_rows(self):
        self.client.force_login(self.owner)
        service = CatalogueItem.objects.create(
            code="WORK-SVC", name="Work service", kind=CatalogueItem.Kind.SERVICE,
            department="Lab", base_unit="test", sale_unit="test", units_per_sale_unit=1,
        )
        for index in range(55):
            encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
            ServiceOrder.objects.create(
                encounter=encounter, service=service, requested_by=self.clinician,
                status=ServiceOrder.Status.RELEASED if index < 33 else ServiceOrder.Status.REQUESTED,
            )
        queue_url = reverse("queue")
        queue, queue_queries = self.measured_get(queue_url)
        self.assertEqual(len(queue.context["encounters"]), 50)
        self.assertEqual(queue.context["encounters"].paginator.count, 55)
        self.assertEqual(len(self.client.get(queue_url, {"page": 2}).context["encounters"]), 5)
        self.assertLess(queue_queries, 15)

        department_url = reverse("departments")
        work, work_queries = self.measured_get(department_url)
        self.assertEqual(work.context["work"].paginator.count, 22)
        self.assertEqual(work.context["history"].paginator.count, 33)
        self.assertEqual(len(work.context["history"]), 30)
        self.assertEqual(len(self.client.get(department_url, {"history_page": 2}).context["history"]), 3)
        self.assertLess(work_queries, 15)

    def test_purchasing_and_custody_history_remains_reachable(self):
        self.client.force_login(self.owner)
        supplier = Supplier.objects.create(name="Pagination Supplier")
        for index in range(64):
            order = PurchaseOrder.objects.create(
                supplier=supplier, requested_by=self.procurement,
                status="received" if index < 33 else "requested",
            )
            PurchaseOrderLine.objects.create(
                order=order, item=self.product, quantity_base_units=Decimal("1"),
                quoted_unit_cost=Decimal("2.00"),
            )
        purchasing_url = reverse("purchasing")
        purchasing, purchasing_queries = self.measured_get(purchasing_url)
        self.assertEqual(len(purchasing.context["orders"]), 30)
        self.assertEqual(len(purchasing.context["history"]), 30)
        self.assertEqual(len(self.client.get(purchasing_url, {"active_page": 2}).context["orders"]), 1)
        self.assertEqual(len(self.client.get(purchasing_url, {"history_page": 2}).context["history"]), 3)
        self.assertLess(purchasing_queries, 20)

        for index in range(64, 104):
            order = PurchaseOrder.objects.create(
                supplier=supplier, requested_by=self.procurement,
                status="received" if index % 2 else "requested",
            )
            PurchaseOrderLine.objects.create(
                order=order, item=self.product, quantity_base_units=Decimal("1"),
                quoted_unit_cost=Decimal("2.00"),
            )
        grown_purchasing, grown_purchasing_queries = self.measured_get(purchasing_url)
        self.assertEqual(grown_purchasing_queries, purchasing_queries)
        self.assertLess(len(grown_purchasing.content), len(purchasing.content) + 2000)

        for index in range(60):
            issue = DepartmentIssue.objects.create(
                department=f"Ward {index % 4}", received_by_name="Ward nurse",
                issued_by=self.pharmacist,
                status=DepartmentIssue.Status.SETTLED if index < 16 else DepartmentIssue.Status.OUTSTANDING,
            )
            DepartmentIssueLine.objects.create(issue=issue, batch=self.batch, quantity_issued=Decimal("1"))
        custody_url = reverse("custody")
        custody, custody_queries = self.measured_get(custody_url)
        self.assertEqual(len(custody.context["outstanding"]), 40)
        self.assertEqual(len(custody.context["settled"]), 15)
        self.assertEqual(custody.context["position"]["outstanding_units"], Decimal("44"))
        self.assertEqual(len(self.client.get(custody_url, {"outstanding_page": 2}).context["outstanding"]), 4)
        self.assertEqual(len(self.client.get(custody_url, {"settled_page": 2}).context["settled"]), 1)
        self.assertLess(custody_queries, 20)

        for index in range(60, 100):
            issue = DepartmentIssue.objects.create(
                department=f"Ward {index % 4}", received_by_name="Ward nurse",
                issued_by=self.pharmacist,
                status=DepartmentIssue.Status.SETTLED if index % 2 else DepartmentIssue.Status.OUTSTANDING,
            )
            DepartmentIssueLine.objects.create(issue=issue, batch=self.batch, quantity_issued=Decimal("1"))
        grown_custody, grown_custody_queries = self.measured_get(custody_url)
        self.assertEqual(grown_custody_queries, custody_queries)
        self.assertLess(len(grown_custody.content), len(custody.content) + 2000)

    def test_custody_aggregation_preserves_per_line_rounding_and_counts_issues(self):
        tiny = StockBatch.objects.create(
            item=self.product, batch_number="ROUND-005",
            purchase_cost_per_base_unit=Decimal("0.005000"),
        )
        cent = StockBatch.objects.create(
            item=self.product, batch_number="ROUND-015",
            purchase_cost_per_base_unit=Decimal("0.015000"),
        )
        issue = DepartmentIssue.objects.create(
            department="Lab", received_by_name="Lab nurse", issued_by=self.pharmacist,
            issued_at=timezone.now() - timedelta(days=8),
        )
        DepartmentIssueLine.objects.create(issue=issue, batch=tiny, quantity_issued=Decimal("1"))
        DepartmentIssueLine.objects.create(issue=issue, batch=cent, quantity_issued=Decimal("1"))
        custody = departmental_custody()
        self.assertEqual(custody["issue_count"], 1)
        self.assertEqual(custody["stale_count"], 1)
        self.assertEqual(custody["stale_value"], Decimal("0.02"))
        self.assertEqual(custody["outstanding_value"], Decimal("0.02"))
