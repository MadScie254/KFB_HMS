from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied

from .models import Role


def user_role(user):
    if not user.is_authenticated:
        return ""
    if user.is_superuser:
        return Role.OWNER
    try:
        return user.staff_profile.role
    except ObjectDoesNotExist:
        # A user with no staff profile has no role. Any other failure here is a
        # real fault and must not be silently downgraded to "no permissions".
        return ""


def role_required(*allowed_roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if user_role(request.user) not in allowed_roles:
                raise PermissionDenied("Your role is not permitted to perform this action.")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


# Navigation and visible actions use the same role capabilities. Service-layer
# checks still enforce segregation of duties for each transaction.
ROLE_CAPABILITIES = {
    # The owner is deliberately given every navigation entry: they asked to be
    # able to reach any page. Page access is not the same as authority, and the
    # segregation of duties that matters lives in the service layer, where a
    # person still cannot approve their own request no matter which screen they
    # can open.
    Role.OWNER: frozenset({
        "dashboard", "brief", "intelligence", "patients", "queue", "clinical",
        "payments", "pharmacy", "wards", "departments", "eye", "reports",
        "stock", "stock_control", "custody", "purchasing", "exceptions",
        "audit", "shifts", "settings", "view_notes", "view_attachments",
        "view_billing", "download_patient_access", "request_correction",
        "review_write_off", "review_stock_count",
    }),
    Role.RECEPTION: frozenset({
        "dashboard", "patients", "queue", "payments", "shifts", "view_billing",
        "start_visit", "take_payment", "request_correction",
    }),
    Role.CLINICIAN: frozenset({
        "dashboard", "patients", "queue", "clinical", "wards", "departments", "custody",
        "view_notes", "view_attachments", "upload_attachment", "start_visit", "download_patient_access",
    }),
    Role.NURSE: frozenset({
        "dashboard", "patients", "wards", "departments", "custody",
        "view_notes", "view_attachments", "upload_attachment", "download_patient_access",
    }),
    Role.PHARMACY: frozenset({
        "dashboard", "pharmacy", "stock", "stock_control", "custody",
        "start_stock_count", "request_write_off", "receive_delivery",
    }),
    Role.LAB: frozenset({"dashboard", "queue", "departments"}),
    Role.EYE: frozenset({"dashboard", "patients", "eye"}),
    Role.PROCUREMENT: frozenset({
        "dashboard", "intelligence", "stock", "stock_control", "custody", "purchasing",
        "start_stock_count", "request_write_off", "receive_delivery",
    }),
    Role.REVIEWER: frozenset({
        "dashboard", "brief", "intelligence", "exceptions", "audit", "purchasing",
        "reports", "stock", "stock_control", "custody", "review_write_off",
        "review_stock_count",
    }),
}

NAVIGATION_KEYS = frozenset({
    "dashboard", "brief", "intelligence", "patients", "queue", "clinical", "payments",
    "pharmacy", "wards", "departments", "eye", "reports", "stock", "stock_control",
    "custody", "purchasing", "exceptions", "audit", "shifts", "settings",
})
ROLE_NAVIGATION = {
    role: sorted(capabilities & NAVIGATION_KEYS)
    for role, capabilities in ROLE_CAPABILITIES.items()
}


def has_capability(role, capability):
    return capability in ROLE_CAPABILITIES.get(role, frozenset())
