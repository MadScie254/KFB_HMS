from django.conf import settings
from django.utils import timezone

from .permissions import ROLE_NAVIGATION, user_role


def application_context(request):
    role = user_role(request.user)
    return {
        "hospital_name": settings.HOSPITAL_NAME,
        "demo_mode": settings.DEMO_MODE,
        "current_role": role,
        "allowed_navigation": ROLE_NAVIGATION.get(role, []),
        "now_local": timezone.localtime(),
    }

