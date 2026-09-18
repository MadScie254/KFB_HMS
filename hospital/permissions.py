from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .models import Role


def user_role(user):
    if not user.is_authenticated:
        return ""
    if user.is_superuser:
        return Role.OWNER
    try:
        return user.staff_profile.role
    except Exception:
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


ROLE_NAVIGATION = {
    Role.OWNER: ["dashboard", "patients", "reports", "exceptions", "audit", "purchasing", "settings"],
    Role.RECEPTION: ["dashboard", "patients", "queue", "payments", "pharmacy", "shifts"],
    Role.CLINICIAN: ["dashboard", "patients", "queue", "clinical", "wards", "departments"],
    Role.NURSE: ["dashboard", "patients", "wards", "departments"],
    Role.PHARMACY: ["dashboard", "pharmacy", "stock"],
    Role.LAB: ["dashboard", "queue", "departments"],
    Role.EYE: ["dashboard", "patients", "eye"],
    Role.PROCUREMENT: ["dashboard", "stock", "purchasing"],
    Role.REVIEWER: ["dashboard", "exceptions", "audit", "purchasing", "reports"],
}
