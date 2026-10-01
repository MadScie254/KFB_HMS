"""Keep public route names stable while views move into smaller modules."""

from django.test import SimpleTestCase

from .urls import urlpatterns


class RouteNameSnapshotTests(SimpleTestCase):
    def test_hospital_route_names(self):
        expected = """
            dashboard health session_status quick_search patients patient_create
            patient_detail patient_attachment_upload patient_attachment_download
            patient_access_pdf encounter_create queue clinical_note encounter_close
            prescription_create service_order_create admission_create pharmacy_orders
            pharmacy_order_create pharmacy_prepare_prescription pharmacy_order_detail
            pharmacy_dispense invoice_payment credit_note_create receipt refund_request
            refunds refund_action shift_manage shift_review supplier_changes
            supplier_change_request supplier_change_review stock deliveries
            goods_receipt_create goods_receipt_detail goods_receipt_invoice
            goods_receipt_check stock_intelligence owner_brief custody custody_issue
            custody_account write_offs write_off_request write_off_review
            batch_disposition stock_counts stock_count_open stock_count_detail
            stock_count_review reports report_download_pdf payment_verify
            credit_note_review exceptions audit_review departments
            service_order_update wards admission_discharge eye_clinic
            eye_case_complete settings purchasing purchase_order_create
            purchase_order_approve csv_import screen_lock screen_unlock downtime_forms
        """.split()
        self.assertEqual([route.name for route in urlpatterns], expected)
