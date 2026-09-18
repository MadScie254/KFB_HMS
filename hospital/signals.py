from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import StaffProfile


@receiver(post_save, sender=User)
def create_staff_profile(sender, instance, created, **kwargs):
    if created:
        StaffProfile.objects.create(user=instance, display_name=instance.get_full_name() or instance.username)

