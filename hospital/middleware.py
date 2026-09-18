from django.shortcuts import redirect
from django.urls import reverse


class ScreenLockMiddleware:
    EXEMPT_NAMES = {"login", "logout", "screen_unlock", "health"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                locked = bool(request.user.staff_profile.locked_at)
            except Exception:
                locked = False
            if locked and request.resolver_match and request.resolver_match.url_name not in self.EXEMPT_NAMES:
                return redirect(reverse("screen_unlock"))
        return self.get_response(request)


class AuditRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Cache-Control", "no-store, private")
        response.headers.setdefault("Pragma", "no-cache")
        return response

