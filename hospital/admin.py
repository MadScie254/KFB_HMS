"""Narrow administration surface.

Transactional and clinical records are intentionally absent. They must move
through role-protected application workflows and service-layer rules.
"""

from django.contrib import admin

from . import models
from .services import audit


class AuditedConfigurationAdmin(admin.ModelAdmin):
    """Keep configuration edits attributable to the admin actor."""

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        before = self._snapshot(type(obj).objects.get(pk=obj.pk)) if change else None
        if isinstance(obj, models.Setting):
            obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        audit(
            request.user, f"admin.{obj._meta.model_name}.{'changed' if change else 'created'}", obj,
            before=before, after=self._snapshot(obj), request=request,
        )

    @staticmethod
    def _snapshot(obj):
        return {
            field.name: str(getattr(obj, field.attname))
            for field in obj._meta.concrete_fields
            if field.name not in {"id", "created_at", "updated_at"}
        }


@admin.register(models.StaffProfile)
class StaffProfileAdmin(AuditedConfigurationAdmin):
    list_display = ("user", "display_name", "role")
    list_filter = ("role",)
    search_fields = ("user__username", "display_name")
    exclude = ("require_password_change", "second_factor_required", "locked_at")


@admin.register(models.Setting)
class SettingAdmin(AuditedConfigurationAdmin):
    list_display = ("key", "production_confirmed", "updated_by", "updated_at")
    list_filter = ("production_confirmed",)
    search_fields = ("key", "description")
    exclude = ("updated_by",)


@admin.register(models.CatalogueItem)
class CatalogueItemAdmin(AuditedConfigurationAdmin):
    list_display = ("code", "name", "kind", "department", "active")
    list_filter = ("kind", "department", "active")
    search_fields = ("code", "name")


@admin.register(models.PriceVersion)
class PriceVersionAdmin(AuditedConfigurationAdmin):
    list_display = ("item", "amount", "effective_from", "effective_to", "approved_by")
    list_filter = ("effective_from",)
    search_fields = ("item__code", "item__name", "reason")
    exclude = ("approved_by",)

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            raise ValueError("Approved prices are immutable; create a new version.")
        obj.approved_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(models.Ward)
class WardAdmin(AuditedConfigurationAdmin):
    list_display = ("name", "active")
    list_filter = ("active",)


@admin.register(models.Bed)
class BedAdmin(AuditedConfigurationAdmin):
    list_display = ("label", "ward", "active")
    list_filter = ("ward", "active")


@admin.register(models.Supplier)
class SupplierAdmin(AuditedConfigurationAdmin):
    list_display = ("name", "phone", "active")
    list_filter = ("active",)
    search_fields = ("name", "phone")
    exclude = ("payment_details",)

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            obj.payment_details = ""
        super().save_model(request, obj, form, change)


class ReadOnlyEvidenceAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.AuditEvent)
class AuditEventAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("created_at", "actor", "effective_role", "action", "entity_type", "entity_id")
    list_filter = ("effective_role", "action", "entity_type")
    search_fields = ("entity_id", "reason", "actor__username")


@admin.register(models.LoginAttempt)
class LoginAttemptAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("attempted_at", "username", "ip_address", "cleared_at")
    list_filter = ("attempted_at", "cleared_at")
    search_fields = ("username", "ip_address")


admin.site.site_header = "KFB Hospital configuration"
admin.site.site_title = "KFB HMS"
admin.site.index_title = "Configuration only - use hospital workflows for operational records"
