import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from hospital.models import Setting


class Command(BaseCommand):
    help = "Report production prerequisites without modifying data."

    @staticmethod
    def static_files_collected():
        """Has collectstatic been run?

        Django serves no static files once DEBUG is off, so a deployment that
        skips this step answers 404 for its own stylesheet and script and
        renders as unstyled HTML. It is silent unless something looks for it.
        """
        if settings.DEMO_MODE:
            return True
        root = Path(settings.STATIC_ROOT)
        return root.is_dir() and any(root.iterdir())

    def handle(self, *args, **options):
        checks = [
            ("Environment is not demo", not settings.DEMO_MODE),
            ("PostgreSQL is configured", settings.DATABASES["default"]["ENGINE"].endswith("postgresql")),
            ("HTTPS redirect enabled", settings.SECURE_SSL_REDIRECT),
            ("Secure session cookie enabled", settings.SESSION_COOKIE_SECURE),
            ("Static files collected", self.static_files_collected()),
            ("CSRF trusted origins set", bool(settings.CSRF_TRUSTED_ORIGINS) or settings.DEMO_MODE),
            ("HSTS enabled", settings.SECURE_HSTS_SECONDS > 0 or settings.DEMO_MODE),
            ("Backup encryption recipient configured", bool(os.getenv("KFB_BACKUP_ENCRYPTION_RECIPIENT"))),
            ("M-PESA not falsely marked live", settings.MPESA_MODE in {"manual", "demo"}),
            ("All operational settings confirmed", not Setting.objects.filter(production_confirmed=False).exists()),
        ]
        for label, ok in checks:
            self.stdout.write(f"{'PASS' if ok else 'NEEDS ACTION'}  {label}")
        if all(ok for _, ok in checks):
            self.stdout.write(self.style.SUCCESS("Automated readiness checks passed. Clinical, legal, hardware and restore reviews are still required."))
        else:
            self.stdout.write(self.style.WARNING("Production prerequisites remain incomplete."))

