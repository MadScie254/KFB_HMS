from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import ExceptionRecord, Role


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DashboardExceptionVisibilityTests(TestCase):
    def test_exception_summaries_only_appear_for_roles_with_exception_access(self):
        ExceptionRecord.objects.create(
            category="stock_discrepancy", summary="Sensitive stock discrepancy",
        )
        for role, should_see in (
            (Role.RECEPTION, False),
            (Role.PHARMACY, False),
            (Role.OWNER, True),
            (Role.REVIEWER, True),
        ):
            with self.subTest(role=role):
                user = User.objects.create_user(username=f"dashboard-{role}", password="test")
                user.staff_profile.role = role
                user.staff_profile.save(update_fields=["role"])
                self.client.force_login(user)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual("exceptions" in response.context, should_see)
                if should_see:
                    self.assertContains(response, "Sensitive stock discrepancy")
                else:
                    self.assertNotContains(response, "Sensitive stock discrepancy")
                    self.assertNotContains(response, "Open centre")
