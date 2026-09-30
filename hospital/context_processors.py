from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .permissions import ROLE_CAPABILITIES, ROLE_NAVIGATION, has_capability, user_role

SHORTCUTS = (
    ("d", "home", "dashboard", "dashboard"),
    ("p", "patients", "patients", "patients"),
    ("q", "queue", "queue", "queue"),
    ("s", "stock", "stock", "stock"),
    ("b", "brief", "owner_brief", "brief"),
    ("r", "reports", "reports", "reports"),
)


def application_context(request):
    role = user_role(request.user)
    return {
        "hospital_name": settings.HOSPITAL_NAME,
        "demo_mode": settings.DEMO_MODE,
        "current_role": role,
        "allowed_navigation": ROLE_NAVIGATION.get(role, []),
        "allowed_capabilities": ROLE_CAPABILITIES.get(role, frozenset()),
        "keyboard_shortcuts": [
            {"key": key, "label": label, "url": reverse(route)}
            for key, label, route, capability in SHORTCUTS
            if has_capability(role, capability)
        ],
        "now_local": timezone.localtime(),
        "session_expires_at": request.session.get("_kfb_expires_at", "") if request.user.is_authenticated else "",
    }
