"""Narrow administration surface.

Transactional and clinical records are intentionally absent. They must move
through role-protected application workflows and service-layer rules.
"""

from django.contrib import admin

from . import models


@admin.register(models.StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "role", "second_factor_required")
    list_filter = ("role", "second_factor_required")
    search_fields = ("user__username", "display_name")


@admin.register(models.Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ("key", "production_confirmed", "updated_by", "updated_at")
    list_filter = ("production_confirmed",)
    search_fields = ("key", "description")


@admin.register(models.CatalogueItem)
class CatalogueItemAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "department", "active")
    list_filter = ("kind", "department", "active")
    search_fields = ("code", "name")


@admin.register(models.PriceVersion)
class PriceVersionAdmin(admin.ModelAdmin):
    list_display = ("item", "amount", "effective_from", "effective_to", "approved_by")
    list_filter = ("effective_from",)
    search_fields = ("item__code", "item__name", "reason")


@admin.register(models.Ward)
class WardAdmin(admin.ModelAdmin):
    list_display = ("name", "active")
    list_filter = ("active",)


@admin.register(models.Bed)
class BedAdmin(admin.ModelAdmin):
    list_display = ("label", "ward", "active")
    list_filter = ("ward", "active")


@admin.register(models.Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "active")
    list_filter = ("active",)
    search_fields = ("name", "phone")


@admin.register(models.EyePackageItem)
class EyePackageItemAdmin(admin.ModelAdmin):
    list_display = ("package_code", "item", "quantity", "active")
    list_filter = ("package_code", "active")


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
    list_display = ("attempted_at", "username", "ip_address")
    search_fields = ("username", "ip_address")


admin.site.site_header = "KFB Hospital configuration"
admin.site.site_title = "KFB HMS"
admin.site.index_title = "Configuration only - use hospital workflows for operational records"
