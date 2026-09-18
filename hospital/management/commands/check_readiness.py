import os

from django.conf import settings
from django.core.management.base import BaseCommand

from hospital.models import Setting


class Command(BaseCommand):
    help = "Report production prerequisites without modifying data."

    def handle(self, *args, **options):
        checks = [
            ("Environment is not demo", not settings.DEMO_MODE),
            ("PostgreSQL is configured", settings.DATABASES["default"]["ENGINE"].endswith("postgresql")),
            ("HTTPS redirect enabled", settings.SECURE_SSL_REDIRECT),
            ("Secure session cookie enabled", settings.SESSION_COOKIE_SECURE),
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

