from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.shortcuts import redirect

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


def unlocked_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if getattr(request.user.staff_profile, "locked_at", None):
            messages.warning(request, "Unlock your screen to continue.")
            return redirect("screen_unlock")
        return view(request, *args, **kwargs)

    return wrapped


# "stock" shows the ledger; "stock_control" adds the deliveries and stock-count
# screens that post movements, so a reviewer can read the position without
# being offered the buttons that change it.
ROLE_NAVIGATION = {
    # The owner is deliberately given every navigation entry: they asked to be
    # able to reach any page. Page access is not the same as authority, and the
    # segregation of duties that matters lives in the service layer, where a
    # person still cannot approve their own request no matter which screen they
    # can open.
    Role.OWNER: [
        "dashboard", "brief", "intelligence", "patients", "queue", "clinical",
        "payments", "pharmacy", "wards", "departments", "eye", "reports",
        "stock", "stock_control", "custody", "purchasing", "exceptions",
        "audit", "shifts", "settings",
    ],
    Role.RECEPTION: ["dashboard", "patients", "queue", "payments", "pharmacy", "shifts"],
    Role.CLINICIAN: ["dashboard", "patients", "queue", "clinical", "wards", "departments", "custody"],
    Role.NURSE: ["dashboard", "patients", "wards", "departments", "custody"],
    Role.PHARMACY: ["dashboard", "pharmacy", "stock", "stock_control", "custody"],
    Role.LAB: ["dashboard", "queue", "departments"],
    Role.EYE: ["dashboard", "patients", "eye"],
    Role.PROCUREMENT: ["dashboard", "intelligence", "stock", "stock_control", "custody", "purchasing"],
    Role.REVIEWER: ["dashboard", "brief", "intelligence", "exceptions", "audit", "purchasing", "reports", "stock", "stock_control", "custody"],
}
