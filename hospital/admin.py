from django.contrib import admin

from . import models


@admin.register(models.Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("patient_number", "first_name", "last_name", "phone", "created_at")
    search_fields = ("patient_number", "first_name", "last_name", "phone")


@admin.register(models.CatalogueItem)
class CatalogueItemAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "department", "active")
    list_filter = ("kind", "department", "active")
    search_fields = ("code", "name")


@admin.register(models.StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ("item", "batch_number", "expiry_date", "status", "quantity_on_hand")
    list_filter = ("status",)


@admin.register(models.Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "patient", "customer_name", "status", "posted_at")
    readonly_fields = ("invoice_number", "posted_at")


@admin.register(models.AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "effective_role", "action", "entity_type", "entity_id")
    list_filter = ("effective_role", "action", "entity_type")
    search_fields = ("entity_id", "reason")
    readonly_fields = [field.name for field in models.AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in [
    models.StaffProfile, models.Setting, models.Encounter, models.Observation, models.ClinicalNote,
    models.PriceVersion, models.StockMovement, models.InvoiceLine, models.Payment,
    models.PaymentAllocation, models.CreditNote, models.CashShift, models.Prescription,
    models.PrescriptionItem, models.PharmacyOrder, models.PharmacyOrderItem, models.Ward,
    models.Bed, models.Admission, models.MedicationAdministration, models.ServiceOrder,
    models.EyeSession, models.EyeCase, models.ClinicianPayable, models.Supplier,
    models.PurchaseOrder, models.ExceptionRecord, models.ImportJob, models.DowntimeEntry,
    models.NursingHandover, models.BedTransfer, models.EyePackageItem,
    models.PurchaseOrderLine, models.GoodsReceipt, models.GoodsReceiptLine,
    models.StockCount, models.StockCountLine, models.TheatreCase, models.DentalRecord,
    models.MaternityRecord, models.NewbornLink, models.ClinicalAttachment, models.Refund,
]:
    admin.site.register(model)

admin.site.site_header = "KFB Hospital configuration"
admin.site.site_title = "KFB HMS"
