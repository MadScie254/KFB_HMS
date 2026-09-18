from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import LoginAttempt, StaffProfile


@receiver(post_save, sender=User)
def create_staff_profile(sender, instance, created, **kwargs):
    if created:
        StaffProfile.objects.create(user=instance, display_name=instance.get_full_name() or instance.username)


@receiver(user_login_failed)
def record_failed_login(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get("username") or ""
    if not username:
        return
    LoginAttempt.objects.create(
        username=username[:150],
        ip_address=(request.META.get("REMOTE_ADDR") or None) if request else None,
        user_agent=(request.headers.get("User-Agent", "")[:255]) if request else "",
    )


@receiver(user_logged_in)
def clear_failed_logins(sender, user, request=None, **kwargs):
    LoginAttempt.clear(user.get_username())
