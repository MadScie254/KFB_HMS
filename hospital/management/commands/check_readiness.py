import json
import os
import re
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from hospital.models import Setting
from hospital.setting_validation import NUMERIC_SETTING_RULES, validated_setting_decimal

REQUIRED_SETTING_KEYS = frozenset({
    "official_receipt_header", "delegated_approver", "eye_package_right",
    "eye_package_left", "eye_doctor_case_fee", "eye_package_inclusions",
    "bed_charging_rule", "bed_register", "mpesa_integration",
    "remote_owner_access", "tax_and_fiscal_receipts",
    "backup_encryption_recipient", "clinical_templates", "near_expiry_days",
    "purchase_cost_variance_fraction", "supplier_invoice_tolerance",
    "stock_variance_review_value", "opening_stock_witness",
})
STATIC_REFERENCE = re.compile(r"\{%\s*static\s+['\"]([^'\"]+)['\"]\s*%\}")


class Command(BaseCommand):
    help = "Fail deployment when automated production prerequisites are incomplete."

    @staticmethod
    def static_manifest_issues():
        if settings.DEMO_MODE:
            return []
        root = Path(settings.STATIC_ROOT)
        manifest_file = root / "staticfiles.json"
        if not manifest_file.is_file():
            return ["Run collectstatic; staticfiles.json is missing."]
        try:
            paths = json.loads(manifest_file.read_text(encoding="utf-8"))["paths"]
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            return ["The static manifest is unreadable or incomplete; rerun collectstatic."]
        if not isinstance(paths, dict) or not paths:
            return ["The static manifest has no asset paths; rerun collectstatic."]
        root = root.resolve()
        issues = []
        for source, target in paths.items():
            if not isinstance(target, str) or not target or not (root / target).resolve().is_relative_to(root) or not (root / target).is_file():
                issues.append(f"Manifest asset missing: {source}")
        template_root = Path(settings.BASE_DIR) / "templates"
        for template in template_root.rglob("*.html"):
            for source in STATIC_REFERENCE.findall(template.read_text(encoding="utf-8")):
                if source not in paths:
                    issues.append(f"Template asset absent from manifest: {source}")
        return sorted(set(issues))

    @staticmethod
    def operational_setting_issues():
        rows = {key: (value, confirmed) for key, value, confirmed in Setting.objects.values_list(
            "key", "value", "production_confirmed"
        )}
        issues = []
        for key in sorted(REQUIRED_SETTING_KEYS):
            if key not in rows:
                issues.append(f"Required operational setting missing: {key}")
            elif not rows[key][0].strip() or not rows[key][1]:
                issues.append(f"Operational setting needs a value and confirmation: {key}")
            elif key in NUMERIC_SETTING_RULES:
                try:
                    validated_setting_decimal(key, rows[key][0])
                except ValidationError as exc:
                    issues.append(f"Invalid operational setting: {exc.messages[0]}")
        return issues

    def handle(self, *args, **options):
        static_issues = self.static_manifest_issues()
        setting_issues = self.operational_setting_issues()
        checks = [
            ("Environment is not demo", not settings.DEMO_MODE),
            ("PostgreSQL is configured", settings.DATABASES["default"]["ENGINE"].endswith("postgresql")),
            ("HTTPS redirect enabled", settings.SECURE_SSL_REDIRECT),
            ("Secure session cookie enabled", settings.SESSION_COOKIE_SECURE),
            ("Static manifest and referenced assets are complete", not static_issues),
            ("CSRF trusted origins set", bool(settings.CSRF_TRUSTED_ORIGINS)),
            ("HSTS enabled", settings.SECURE_HSTS_SECONDS > 0),
            ("Backup encryption recipient configured", bool(os.getenv("KFB_BACKUP_ENCRYPTION_RECIPIENT"))),
            ("M-PESA not falsely marked live", settings.MPESA_MODE == "manual"),
            ("Required operational settings confirmed", not setting_issues),
        ]
        for label, ok in checks:
            self.stdout.write(f"{'PASS' if ok else 'NEEDS ACTION'}  {label}")
        for issue in static_issues + setting_issues:
            self.stdout.write(f"  - {issue}")
        if not all(ok for _, ok in checks):
            raise CommandError("Production prerequisites remain incomplete.")
        self.stdout.write(self.style.SUCCESS(
            "Automated readiness checks passed. Clinical, legal, hardware and restore reviews are still required."
        ))
