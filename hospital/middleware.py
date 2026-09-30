from datetime import timedelta

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin


class SessionActivityMiddleware(MiddlewareMixin):
    """Extend sessions for real activity, never for background status checks."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if request.method == "HEAD" or (match and match.url_name == "session_status"):
            return None
        if request.user.is_authenticated:
            request.session["_kfb_expires_at"] = (
                timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE)
            ).isoformat()
        return None


class ScreenLockMiddleware:
    """Force a locked user back to the unlock screen on every protected view.

    The check runs in process_view rather than __call__: during __call__ the URL
    has not been resolved yet, so request.resolver_match is still None and the
    guard silently passes for every request.
    """

    EXEMPT_NAMES = {"login", "logout", "screen_unlock", "health", "session_status"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return None
        profile = getattr(user, "staff_profile", None)
        if profile is None or not profile.locked_at:
            return None
        match = request.resolver_match
        if match and match.url_name in self.EXEMPT_NAMES:
            return None
        if request.path.startswith("/admin/"):
            # The Django admin has its own session; locking must not strand a
            # superuser in a redirect loop it cannot exit.
            return None
        return redirect(reverse("screen_unlock"))


class AuditRequestMiddleware:
    """Keep patient data out of caches.

    Every response was marked no-store indiscriminately. Static assets are now
    served by WhiteNoise, which returns before this middleware is reached, so
    in practice that is settled upstream; the exemption stays for any path under
    STATIC_URL or MEDIA_URL that is later routed through Django, because those
    carry no patient data and are cheap to cache. Everything else is no-store.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.static_prefixes = tuple(
            prefix for prefix in (settings.STATIC_URL, getattr(settings, "MEDIA_URL", None)) if prefix
        )

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(self.static_prefixes):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
            return response
        response.headers.setdefault("Cache-Control", "no-store, private")
        response.headers.setdefault("Pragma", "no-cache")
        return response
