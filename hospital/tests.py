import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError, OutputWrapper
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .analytics import (
    stock_position,
)
from .management.commands.check_readiness import REQUIRED_SETTING_KEYS
from .management.commands.check_readiness import Command as ReadinessCommand
from .models import (
    Admission,
    AuditEvent,
    Bed,
    CashShift,
    CatalogueItem,
    ClinicalAttachment,
    ClinicalNote,
    ClinicianPayable,
    CreditNote,
    DepartmentIssue,
    DowntimeEntry,
    Encounter,
    ExceptionRecord,
    EyeCase,
    GoodsReceipt,
    ImportJob,
    Invoice,
    InvoiceLine,
    LoginAttempt,
    Patient,
    Payment,
    PaymentAllocation,
    PharmacyOrder,
    PrescriptionItem,
    PriceVersion,
    PurchaseOrder,
    PurchaseOrderLine,
    Refund,
    Role,
    ServiceOrder,
    Setting,
    StockBatch,
    StockMovement,
    StockWriteOff,
    Supplier,
    SupplierChangeRequest,
    Ward,
)
from .permissions import ROLE_NAVIGATION, user_role
from .services import (
    approve_credit_note,
    approve_purchase_order,
    check_delivery,
    close_cash_shift,
    close_encounter,
    complete_eye_case,
    discharge_admission,
    dispense_order,
    issue_to_department,
    open_cash_shift,
    open_stock_count,
    pay_refund,
    prepare_pharmacy_order,
    receive_delivery,
    record_payment,
    request_refund,
    request_supplier_change,
    request_write_off,
    review_cash_shift,
    review_mpesa,
    review_refund,
    review_supplier_change,
    review_write_off,
    update_service_order,
)
from .test_support import JPEG_BYTES, HospitalFixtureMixin
from .views import outstanding_receivables, verified_collections_since


class EnvironmentStartupTests(SimpleTestCase):
    def settings_import(self, environment):
        child_env = os.environ.copy()
        child_env.pop("KFB_ENV", None)
        child_env.pop("KFB_DATABASE_URL", None)
        child_env.pop("KFB_SECRET_KEY", None)
        if environment is not None:
            child_env["KFB_ENV"] = environment
        if environment == "demo":
            child_env["KFB_DEBUG"] = "1"
        if environment == "production":
            child_env.update({
                "KFB_SECRET_KEY": "ci-only-not-a-real-secret-0123456789abcdefghijklmnopqrstuvwxyz",
                "KFB_DATABASE_URL": "postgresql://ci:ci@127.0.0.1:5432/ci",
                "KFB_ALLOWED_HOSTS": "hospital.example.test",
                "KFB_DEBUG": "0",
            })
        return subprocess.run(
            [sys.executable, "-c", "from kfb_hms import settings; print(settings.ENVIRONMENT, settings.DEMO_MODE, settings.DEBUG)"],
            cwd=settings.BASE_DIR, env=child_env, capture_output=True, text=True, check=False,
        )

    def test_missing_and_unknown_environment_fail_before_django_starts(self):
        for environment in (None, "staging"):
            with self.subTest(environment=environment):
                result = self.settings_import(environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("KFB_ENV must be set explicitly", result.stderr)

    def test_explicit_demo_and_production_have_expected_security_modes(self):
        demo = self.settings_import("demo")
        self.assertEqual(demo.returncode, 0, demo.stderr)
        self.assertIn("demo True True", demo.stdout)
        production = self.settings_import("production")
        self.assertEqual(production.returncode, 0, production.stderr)
        self.assertIn("production False False", production.stdout)

    @skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell launcher test")
    def test_service_launcher_rejects_missing_and_demo_environment(self):
        if not (settings.BASE_DIR / ".venv" / "Scripts" / "python.exe").exists():
            self.skipTest("Local virtual environment is required for the launcher probe")
        script = settings.BASE_DIR / "scripts" / "start-server.ps1"
        for environment, expected in (
            (None, "KFB_ENV must be set explicitly"),
            ("demo", "KFB_ENV must be production"),
            ("production", "KFB_SECURE_SSL_REDIRECT must be 1"),
        ):
            child_env = os.environ.copy()
            child_env.pop("KFB_ENV", None)
            child_env["KFB_SECURE_SSL_REDIRECT"] = "0"
            if environment:
                child_env["KFB_ENV"] = environment
            with self.subTest(environment=environment):
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-File", str(script)],
                    cwd=settings.BASE_DIR, env=child_env, capture_output=True,
                    text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)


class DemoLauncherFailureTests(SimpleTestCase):
    @skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows launcher test")
    def test_windows_launchers_stop_before_migration_when_pip_fails(self):
        python = settings.BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            self.skipTest("Project virtual environment is required")
        with TemporaryDirectory() as root:
            fake_pip = Path(root) / "pip"
            fake_pip.mkdir()
            (fake_pip / "__init__.py").write_text("", encoding="utf-8")
            (fake_pip / "__main__.py").write_text("raise SystemExit(37)\n", encoding="utf-8")
            child_env = os.environ.copy()
            child_env["KFB_ENV"] = "demo"
            child_env["PYTHONPATH"] = root + os.pathsep + child_env.get("PYTHONPATH", "")
            for command in (
                ["powershell", "-NoProfile", "-File", str(settings.BASE_DIR / "scripts" / "run-demo.ps1")],
                ["cmd", "/c", str(settings.BASE_DIR / "scripts" / "run-demo.cmd")],
            ):
                with self.subTest(command=command[0]):
                    result = subprocess.run(
                        command, cwd=settings.BASE_DIR, env=child_env,
                        capture_output=True, text=True, check=False, timeout=30,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Installing locked dependencies", result.stdout)
                    self.assertNotIn("Applying migrations", result.stdout)

    @skipUnless(os.name != "nt" and shutil.which("bash"), "Unix launcher test")
    def test_unix_launcher_stops_before_migration_when_pip_fails(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            scripts = root_path / "scripts"
            scripts.mkdir()
            shutil.copy2(settings.BASE_DIR / "scripts" / "run-demo.sh", scripts / "run-demo.sh")
            python = root_path / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 37\n", encoding="utf-8")
            python.chmod(0o755)
            result = subprocess.run(
                ["bash", str(scripts / "run-demo.sh")], cwd=root,
                env={**os.environ, "KFB_ENV": "demo"}, capture_output=True,
                text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 37)
            self.assertIn("Installing locked dependencies", result.stdout)
            self.assertNotIn("Applying migrations", result.stdout)


class ReadinessTests(TestCase):
    def test_demo_and_empty_operational_settings_fail_the_command(self):
        output = StringIO()
        with override_settings(DEMO_MODE=True):
            with self.assertRaises(CommandError):
                call_command("check_readiness", stdout=output)
        self.assertIn("NEEDS ACTION  Environment is not demo", output.getvalue())
        self.assertIn("Required operational setting missing", output.getvalue())

    def test_every_required_setting_needs_value_and_confirmation(self):
        self.assertEqual(len(ReadinessCommand.operational_setting_issues()), len(REQUIRED_SETTING_KEYS))
        numeric_values = {
            "near_expiry_days": "90",
            "purchase_cost_variance_fraction": "0.10",
            "supplier_invoice_tolerance": "1.00",
            "stock_variance_review_value": "500.00",
        }
        Setting.objects.bulk_create([
            Setting(key=key, value=numeric_values.get(key, "confirmed"), production_confirmed=True)
            for key in REQUIRED_SETTING_KEYS
        ])
        self.assertEqual(ReadinessCommand.operational_setting_issues(), [])
        Setting.objects.filter(key="near_expiry_days").update(production_confirmed=False)
        self.assertIn("near_expiry_days", " ".join(ReadinessCommand.operational_setting_issues()))

    def test_numeric_settings_reject_invalid_values_and_readiness_reports_legacy_rows(self):
        invalid = {
            "near_expiry_days": ("", "not-a-number", "NaN", "Infinity", "-1", "3651", "1.5"),
            "purchase_cost_variance_fraction": ("NaN", "Infinity", "-0.1", "1.1"),
            "supplier_invoice_tolerance": ("NaN", "Infinity", "-1", "100001"),
            "stock_variance_review_value": ("NaN", "Infinity", "-1", "1000001"),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    with self.assertRaisesMessage(ValidationError, key):
                        Setting.objects.create(key=key, value=value)
            Setting.objects.create(key=key, value="0", production_confirmed=True)
        Setting.objects.filter(key="near_expiry_days").update(value="NaN")
        self.assertIn("Invalid operational setting: Setting near_expiry_days", " ".join(
            ReadinessCommand.operational_setting_issues()
        ))

    def test_partial_static_manifest_is_not_readiness(self):
        with TemporaryDirectory() as output:
            root = Path(output)
            (root / "css").mkdir()
            (root / "css" / "app.123.css").write_text("body{}", encoding="utf-8")
            (root / "staticfiles.json").write_text(json.dumps({
                "paths": {"css/app.css": "css/app.123.css", "js/app.js": "js/app.123.js"},
            }), encoding="utf-8")
            with override_settings(DEMO_MODE=False, STATIC_ROOT=root):
                issues = ReadinessCommand.static_manifest_issues()
            self.assertIn("Manifest asset missing: js/app.js", issues)
            self.assertTrue(any("Template asset absent from manifest" in issue for issue in issues))

    @override_settings(
        DEMO_MODE=False, SECURE_SSL_REDIRECT=True, SESSION_COOKIE_SECURE=True,
        CSRF_TRUSTED_ORIGINS=["https://hospital.example"], SECURE_HSTS_SECONDS=31536000,
        MPESA_MODE="manual",
    )
    def test_each_production_prerequisite_can_fail_the_command(self):
        command = ReadinessCommand()
        command.stdout = OutputWrapper(StringIO())
        with (
            patch.dict(settings.DATABASES["default"], {"ENGINE": "django.db.backends.postgresql"}),
            patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": "age1test"}),
            patch.object(ReadinessCommand, "static_manifest_issues", return_value=[]),
            patch.object(ReadinessCommand, "operational_setting_issues", return_value=[]),
        ):
            command.handle()
            for name, invalid in (
                ("DEMO_MODE", True), ("SECURE_SSL_REDIRECT", False),
                ("SESSION_COOKIE_SECURE", False), ("CSRF_TRUSTED_ORIGINS", []),
                ("SECURE_HSTS_SECONDS", 0), ("MPESA_MODE", "live"),
            ):
                with self.subTest(name=name), override_settings(**{name: invalid}):
                    with self.assertRaises(CommandError):
                        command.handle()
            with patch.dict(settings.DATABASES["default"], {"ENGINE": "django.db.backends.sqlite3"}):
                with self.assertRaises(CommandError):
                    command.handle()
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": ""}):
                with self.assertRaises(CommandError):
                    command.handle()


class BackupEncryptionTests(TransactionTestCase):
    @override_settings(DEMO_MODE=True, ENVIRONMENT="demo")
    def test_demo_backup_remains_a_valid_zip_with_checksum(self):
        with TemporaryDirectory() as output:
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": ""}):
                call_command("backup_kfb", output=output)
            archives = list(Path(output).glob("*.zip"))
            self.assertEqual(len(archives), 1)
            self.assertTrue((Path(f"{archives[0]}.sha256")).exists())
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertIn("database.sqlite3", archive.namelist())
                self.assertIn("manifest.json", archive.namelist())

    @override_settings(DEMO_MODE=False, ENVIRONMENT="production")
    def test_missing_age_leaves_no_plaintext_backup(self):
        with TemporaryDirectory() as output:
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": "age1test"}):
                with patch("hospital.management.commands.backup_kfb.shutil.which", return_value=None):
                    with self.assertRaises(CommandError):
                        call_command("backup_kfb", output=output)
            self.assertEqual(list(Path(output).iterdir()), [])

    @override_settings(DEMO_MODE=False, ENVIRONMENT="production")
    def test_failed_encryption_leaves_no_plaintext_backup(self):
        with TemporaryDirectory() as output:
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": "age1test"}):
                with patch("hospital.management.commands.backup_kfb.shutil.which", return_value="age"):
                    with patch(
                        "hospital.management.commands.backup_kfb.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1, stderr="invalid recipient"),
                    ):
                        with self.assertRaises(CommandError):
                            call_command("backup_kfb", output=output)
            self.assertEqual(list(Path(output).iterdir()), [])

    @override_settings(DEMO_MODE=False, ENVIRONMENT="production")
    def test_success_publishes_only_encrypted_archive_and_checksum(self):
        def encrypt(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"age-encrypted-test")
            return subprocess.CompletedProcess(command, 0)

        with TemporaryDirectory() as output:
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": "age1test"}):
                with patch("hospital.management.commands.backup_kfb.shutil.which", return_value="age"):
                    with patch("hospital.management.commands.backup_kfb.subprocess.run", side_effect=encrypt):
                        call_command("backup_kfb", output=output)
            files = list(Path(output).iterdir())
            self.assertEqual(len(files), 2)
            self.assertEqual(len(list(Path(output).glob("*.zip.age"))), 1)
            self.assertEqual(len(list(Path(output).glob("*.zip"))), 0)
            self.assertEqual(len(list(Path(output).glob("*.zip.age.sha256"))), 1)

    @override_settings(DEMO_MODE=True, ENVIRONMENT="demo")
    def test_backup_excludes_upload_that_starts_during_snapshot(self):
        from hospital.media_storage import BackupSafeFileSystemStorage

        with TemporaryDirectory() as output, TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root), patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": ""}):
                storage = BackupSafeFileSystemStorage(location=media_root)
                storage.save("clinical/existing.pdf", ContentFile(b"existing evidence"))
                started, finished = Event(), Event()
                failures = []

                def upload():
                    started.set()
                    try:
                        storage.save("clinical/concurrent.pdf", ContentFile(b"later evidence"))
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        finished.set()

                original_write = zipfile.ZipFile.write
                worker = None

                def write_while_upload_waits(archive, filename, arcname=None, *args, **kwargs):
                    nonlocal worker
                    if arcname == "database.sqlite3":
                        worker = Thread(target=upload)
                        worker.start()
                        self.assertTrue(started.wait(2))
                        self.assertFalse(finished.wait(0.2), "Upload passed the backup lock")
                    return original_write(archive, filename, arcname, *args, **kwargs)

                with patch.object(zipfile.ZipFile, "write", write_while_upload_waits):
                    call_command("backup_kfb", output=output)
                worker.join(5)
                self.assertTrue(finished.is_set())
                self.assertEqual(failures, [])
                archive_path = next(Path(output).glob("*.zip"))
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertIn("media/clinical/existing.pdf", archive.namelist())
                    self.assertNotIn("media/clinical/concurrent.pdf", archive.namelist())
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(
                        manifest["media_sha256"],
                        {"clinical/existing.pdf": hashlib.sha256(b"existing evidence").hexdigest()},
                    )

    @skipUnless(os.name == "nt", "Restore script uses Windows PowerShell and icacls")
    def test_encrypted_restore_removes_plaintext_zip_on_success_and_failure(self):
        restore_script = Path(settings.BASE_DIR) / "scripts" / "restore-backup.ps1"
        with TemporaryDirectory() as root:
            root = Path(root)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "age.cmd").write_text(
                '@echo off\r\ncopy /Y "%~6" "%~5" >NUL\r\nexit /b %ERRORLEVEL%\r\n', encoding="ascii"
            )
            temporary_root = root / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            environment["TEMP"] = str(temporary_root)
            environment["TMP"] = str(temporary_root)
            for valid in (True, False):
                with self.subTest(valid=valid):
                    archive_path = root / ("valid.zip.age" if valid else "invalid.zip.age")
                    if valid:
                        with zipfile.ZipFile(archive_path, "w") as archive:
                            archive.writestr("manifest.json", "{}")
                    else:
                        archive_path.write_bytes(b"invalid zip")
                    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                    Path(f"{archive_path}.sha256").write_text(f"{checksum}  {archive_path.name}\n")
                    target = root / ("valid-target" if valid else "invalid-target")
                    result = subprocess.run(
                        ["pwsh", "-NoProfile", "-File", str(restore_script), "-Archive", str(archive_path),
                         "-TargetDirectory", str(target), "-AgeIdentity", "dummy"],
                        env=environment, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode == 0, valid, result.stderr)
                    self.assertFalse((target / "decrypted-backup.zip").exists())
                    self.assertEqual(list(temporary_root.iterdir()), [])
                    if valid:
                        self.assertTrue((target / "manifest.json").exists())


class LoginAttemptRetentionTests(TestCase):
    def test_successful_login_clears_account_counter_but_keeps_address_evidence(self):
        LoginAttempt.objects.bulk_create([
            LoginAttempt(username="staff", ip_address="127.0.0.1") for _ in range(LoginAttempt.LOCKOUT_THRESHOLD)
        ] + [
            LoginAttempt(username=f"other-{index}", ip_address="127.0.0.1") for index in range(1000)
        ])
        self.assertTrue(LoginAttempt.is_locked("staff", "127.0.0.1"))
        LoginAttempt.clear("staff")
        self.assertEqual(LoginAttempt.recent_failures("staff"), 0)
        self.assertEqual(LoginAttempt.recent_failures_from("127.0.0.1"), 1000 + LoginAttempt.LOCKOUT_THRESHOLD)
        self.assertEqual(LoginAttempt.objects.filter(username="staff", cleared_at__isnull=False).count(), 8)

    def test_retention_exports_before_pruning_and_keeps_active_window(self):
        old = LoginAttempt.objects.create(username="old", ip_address="192.0.2.1")
        LoginAttempt.objects.filter(pk=old.pk).update(attempted_at=timezone.now() - timedelta(days=2))
        active = LoginAttempt.objects.create(username="active", ip_address="192.0.2.2")
        exported = {}

        def encrypt(command, **kwargs):
            with zipfile.ZipFile(command[-1]) as archive:
                exported["manifest"] = json.loads(archive.read("manifest.json"))
                exported["rows"] = [json.loads(row) for row in archive.read("login-attempts.jsonl").splitlines()]
            Path(command[command.index("--output") + 1]).write_bytes(b"encrypted evidence")
            return subprocess.CompletedProcess(command, 0)

        with TemporaryDirectory() as output:
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": "age1test"}):
                with patch("hospital.management.commands.prune_login_attempts.shutil.which", return_value="age"):
                    with patch("hospital.management.commands.prune_login_attempts.subprocess.run", side_effect=encrypt):
                        call_command("prune_login_attempts", output=output, retention_days=1, stdout=StringIO())
            self.assertEqual(exported["manifest"]["row_count"], 1)
            self.assertEqual([row["id"] for row in exported["rows"]], [old.pk])
            self.assertFalse(LoginAttempt.objects.filter(pk=old.pk).exists())
            self.assertTrue(LoginAttempt.objects.filter(pk=active.pk).exists())
            self.assertEqual(len(list(Path(output).glob("*.zip.age"))), 1)
            self.assertEqual(len(list(Path(output).glob("*.zip.age.sha256"))), 1)

    def test_retention_never_deletes_when_encryption_fails(self):
        old = LoginAttempt.objects.create(username="old", ip_address="192.0.2.1")
        LoginAttempt.objects.filter(pk=old.pk).update(attempted_at=timezone.now() - timedelta(days=2))
        with TemporaryDirectory() as output:
            with patch.dict(os.environ, {"KFB_BACKUP_ENCRYPTION_RECIPIENT": "age1test"}):
                with patch("hospital.management.commands.prune_login_attempts.shutil.which", return_value="age"):
                    with patch(
                        "hospital.management.commands.prune_login_attempts.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1, stderr="bad recipient"),
                    ):
                        with self.assertRaisesMessage(CommandError, "encryption failed"):
                            call_command("prune_login_attempts", output=output, retention_days=1)
            self.assertTrue(LoginAttempt.objects.filter(pk=old.pk).exists())
            self.assertEqual(list(Path(output).iterdir()), [])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkflowTests(HospitalFixtureMixin, TestCase):

    def test_receipt_reopen_and_explicit_reprint_are_marked_duplicate(self):
        order = self.prepare(1)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=Decimal("5.00"),
            method=Payment.Method.CASH, reference="", idempotency_key="receipt-first-issue",
        )
        url = reverse("receipt", args=[payment.pk])
        self.client.force_login(self.reception)
        self.assertEqual(self.client.head(url).status_code, 200)
        payment.refresh_from_db()
        self.assertIsNone(payment.receipt_issued_at)
        self.assertContains(self.client.get(url), "PAYMENT RECEIPT")
        payment.refresh_from_db()
        self.assertIsNotNone(payment.receipt_issued_at)
        self.assertContains(self.client.get(url), "DUPLICATE RECEIPT")
        self.assertContains(self.client.get(f"{url}?reprint=1"), "DUPLICATE RECEIPT")
        self.assertEqual(AuditEvent.objects.filter(action="receipt.issued", entity_id=str(payment.pk)).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="receipt.reprinted", entity_id=str(payment.pk)).count(), 2)

    def test_walk_in_payment_and_dispense_reconcile(self):
        order = self.prepare(15)
        self.assertEqual(order.status, PharmacyOrder.Status.PREPARED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))
        payment = record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=75, method=Payment.Method.CASH, reference="", idempotency_key="pay-1")
        order.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.CLEARED)
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="dispense-1")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="dispense-1")
        order.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.DISPENSED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("185"))
        self.assertEqual(payment.amount, order.invoice.total)
        self.assertEqual(StockMovement.objects.filter(reference_id=str(order.pk), movement_type="dispense").count(), 1)

    def test_cashier_cannot_dispense_and_pharmacy_cannot_collect(self):
        order = self.prepare(1)
        with self.assertRaisesMessage(ValidationError, "reception/cashier"):
            record_payment(actor=self.pharmacist, invoice_id=order.invoice_id, amount=5, method="cash", reference="", idempotency_key="wrong-role-pay")
        with self.assertRaisesMessage(ValidationError, "pharmacy staff"):
            dispense_order(actor=self.reception, order_id=order.pk, idempotency_key="wrong-role-dispense")

    def test_two_orders_cannot_both_use_last_stock(self):
        first = self.prepare(200)
        record_payment(actor=self.reception, invoice_id=first.invoice_id, amount=1000, method="cash", reference="", idempotency_key="pay-first")
        dispense_order(actor=self.pharmacist, order_id=first.pk, idempotency_key="disp-first")
        second = self.prepare(1)
        record_payment(actor=self.reception, invoice_id=second.invoice_id, amount=5, method="cash", reference="", idempotency_key="pay-second")
        with self.assertRaisesMessage(ValidationError, "Insufficient valid stock"):
            dispense_order(actor=self.pharmacist, order_id=second.pk, idempotency_key="disp-second")

    def test_expired_and_quarantined_batches_are_not_dispensed(self):
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.save(update_fields=["expiry_date"])
        order = self.prepare(1)
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=5, method="cash", reference="", idempotency_key="pay-expired")
        with self.assertRaises(ValidationError):
            dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="disp-expired")
        self.batch.expiry_date = timezone.localdate() + timedelta(days=10)
        self.batch.status = StockBatch.Status.QUARANTINE
        self.batch.save(update_fields=["expiry_date", "status"])
        with self.assertRaises(ValidationError):
            dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="disp-quarantine")

    def test_partial_invoice_balance_and_unapplied_deposit(self):
        invoice = Invoice.objects.create(patient=self.patient, status=Invoice.Status.POSTED, posted_at=timezone.now(), created_by=self.reception)
        InvoiceLine.objects.create(invoice=invoice, item=self.product, description="Inpatient charge", department="Inpatient", quantity=1, unit_price=10000)
        payment = Payment.objects.create(amount=5000, method="cash", received_by=self.reception, shift=self.shift, idempotency_key="deposit-test")
        PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=4000, allocated_by=self.reception)
        self.assertEqual(invoice.balance, Decimal("6000"))
        self.assertEqual(payment.unapplied_amount, Decimal("1000"))

    def test_cash_shift_equation_excludes_mpesa(self):
        Payment.objects.create(amount=8000, method="cash", received_by=self.reception, shift=self.shift, idempotency_key="cash-shift-test")
        Payment.objects.create(amount=5000, method="mpesa", reference="MPESA-UNIQUE", verification_status="unverified", received_by=self.reception, shift=self.shift, idempotency_key="mpesa-shift-test")
        self.shift.legacy_cash_refunds = 500
        self.shift.transfers_out = 6000
        self.shift.actual_cash = 2300
        self.shift.closed_at = timezone.now()
        self.shift.save()
        self.assertEqual(self.shift.expected_cash, Decimal("2500"))
        self.assertEqual(self.shift.variance, Decimal("-200"))

    def test_shift_opening_is_unique_and_review_is_independent(self):
        with self.assertRaisesMessage(ValidationError, "already have an open shift"):
            open_cash_shift(actor=self.reception, label="Duplicate", opening_float=Decimal("100"))
        self.assertEqual(CashShift.objects.filter(cashier=self.reception, status=CashShift.Status.OPEN).count(), 1)
        closed = close_cash_shift(
            actor=self.reception, actual_cash=Decimal("1000"),
            transfers_in=Decimal("0"), transfers_out=Decimal("0"), variance_reason="",
        )
        self.assertEqual(closed.status, CashShift.Status.CLOSED)
        owner_shift = CashShift.objects.create(
            cashier=self.owner, label="Owner", opening_float=Decimal("0"),
            closed_at=timezone.now(), status=CashShift.Status.CLOSED,
        )
        with self.assertRaisesMessage(ValidationError, "cannot review your own shift"):
            review_cash_shift(actor=self.owner, shift_id=owner_shift.pk)
        review_cash_shift(actor=self.reviewer, shift_id=closed.pk)
        closed.refresh_from_db()
        self.assertEqual(closed.status, CashShift.Status.REVIEWED)
        self.assertEqual(closed.reviewer, self.reviewer)
        self.assertIsNotNone(closed.reviewed_at)
        with self.assertRaisesMessage(ValidationError, "Only a closed shift"):
            review_cash_shift(actor=self.reviewer, shift_id=closed.pk)

    def test_shift_close_does_not_accept_unrecorded_cash_refunds(self):
        self.client.force_login(self.reception)
        response = self.client.post(reverse("shift_manage"), {
            "actual_cash": "1000.00", "transfers_in": "0", "transfers_out": "0",
            "cash_refunds": "9999.00", "variance_reason": "",
        })
        self.assertEqual(response.status_code, 302)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.status, CashShift.Status.CLOSED)
        self.assertEqual(self.shift.cash_refunds, Decimal("0.00"))

    def test_cash_refund_reopens_invoice_and_reconciles_paying_shift(self):
        invoice = Invoice.objects.create(
            patient=self.patient, status=Invoice.Status.POSTED,
            posted_at=timezone.now(), created_by=self.reception,
        )
        InvoiceLine.objects.create(
            invoice=invoice, item=self.product, description="Cash refund test",
            department="Pharmacy", quantity=1, unit_price=100,
        )
        payment = Payment.objects.create(
            amount=Decimal("100"), method=Payment.Method.CASH, received_by=self.reception,
            shift=self.shift, idempotency_key="cash-refund-test",
        )
        PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=Decimal("100"), allocated_by=self.reception)
        invoice.refresh_status()
        self.assertEqual(invoice.balance, Decimal("0.00"))
        refund = request_refund(
            actor=self.reception, payment_id=payment.pk, amount=Decimal("30"), reason="Duplicate collection",
        )
        with self.assertRaisesMessage(ValidationError, "unrefunded amount"):
            request_refund(actor=self.reception, payment_id=payment.pk, amount=Decimal("80"), reason="Too much")
        with self.assertRaisesMessage(ValidationError, "independent reviewer"):
            review_refund(actor=self.reception, refund_id=refund.pk, approve=True)
        review_refund(actor=self.reviewer, refund_id=refund.pk, approve=True)
        self.assertEqual(invoice.balance, Decimal("0.00"))
        pay_refund(actor=self.reception, refund_id=refund.pk)
        refund.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PAID)
        self.assertEqual(refund.paid_shift, self.shift)
        self.assertEqual(invoice.paid_amount, Decimal("70.00"))
        self.assertEqual(invoice.balance, Decimal("30.00"))
        self.assertEqual(invoice.status, Invoice.Status.PART_PAID)
        self.assertEqual(outstanding_receivables(), Decimal("30.00"))
        self.assertEqual(verified_collections_since(timezone.now() - timedelta(days=1)), Decimal("70.00"))
        self.assertEqual(self.shift.cash_refunds, Decimal("30.00"))
        self.assertEqual(self.shift.expected_cash, Decimal("1070.00"))
        self.client.force_login(self.reception)
        self.assertContains(self.client.get(reverse("receipt", args=[payment.pk])), "Cash refunded against this receipt")
        self.assertContains(self.client.get(reverse("refunds")), "Duplicate collection")
        self.assertEqual(self.client.get(reverse("refund_request", args=[payment.pk])).status_code, 200)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("refunds")).status_code, 200)
        with self.assertRaisesMessage(ValidationError, "Only an approved refund"):
            pay_refund(actor=self.reception, refund_id=refund.pk)

    def test_supplier_change_requires_independent_review_and_current_snapshot(self):
        supplier = Supplier.objects.create(name="Original supplier", phone="111", payment_details="Account A")
        change = request_supplier_change(
            actor=self.owner, supplier_id=supplier.pk, proposed_name="Updated supplier",
            proposed_phone="222", proposed_payment_details="Account B",
            proposed_active=True, reason="Verified new bank details",
        )
        with self.assertRaisesMessage(ValidationError, "cannot approve your own"):
            review_supplier_change(actor=self.owner, change_id=change.pk, approve=True)
        review_supplier_change(actor=self.reviewer, change_id=change.pk, approve=True)
        supplier.refresh_from_db()
        self.assertEqual(supplier.payment_details, "Account B")
        self.assertEqual(supplier.name, "Updated supplier")
        self.assertEqual(SupplierChangeRequest.objects.get(pk=change.pk).status, SupplierChangeRequest.Status.APPROVED)
        self.assertEqual(AuditEvent.objects.filter(action="supplier.changed", entity_id=str(supplier.pk)).count(), 1)
        stale = request_supplier_change(
            actor=self.owner, supplier_id=supplier.pk, proposed_name="Stale name",
            proposed_phone="333", proposed_payment_details="Account C",
            proposed_active=False, reason="Proposed change",
        )
        supplier.phone = "444"
        supplier.save(update_fields=["phone", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "changed after this request"):
            review_supplier_change(actor=self.reviewer, change_id=stale.pk, approve=True)

    def test_supplier_change_screen_submits_and_reviews_request(self):
        supplier = Supplier.objects.create(name="Screen supplier", phone="100")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("supplier_change_request", args=[supplier.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("supplier_change_request", args=[supplier.pk]), {
            "proposed_name": "Screen supplier", "proposed_phone": "200",
            "proposed_payment_details": "Verified bank", "proposed_active": "on",
            "reason": "New verified details",
        }).status_code, 302)
        change = SupplierChangeRequest.objects.get(supplier=supplier)
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(reverse("supplier_changes")), "New verified details")
        self.assertEqual(self.client.post(reverse("supplier_change_review", args=[change.pk]), {
            "decision": "approve",
        }).status_code, 302)
        supplier.refresh_from_db()
        self.assertEqual(supplier.payment_details, "Verified bank")

    def test_admin_configuration_is_audited_and_historical_prices_cannot_change(self):
        self.owner.is_staff = True
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_staff", "is_superuser"])
        self.client.force_login(self.owner)
        profile = self.reception.staff_profile
        profile_url = reverse("admin:hospital_staffprofile_change", args=[profile.pk])
        self.assertNotContains(self.client.get(profile_url), "second_factor_required")
        self.assertEqual(self.client.post(profile_url, {
            "user": self.reception.pk, "role": Role.NURSE,
            "display_name": "Reassigned", "active_shift_label": "",
            "_save": "Save",
        }).status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.role, Role.NURSE)
        self.assertEqual(AuditEvent.objects.filter(action="admin.staffprofile.changed", entity_id=str(profile.pk)).count(), 1)
        setting = Setting.objects.create(key="review_test", value="1")
        self.assertEqual(self.client.post(reverse("admin:hospital_setting_change", args=[setting.pk]), {
            "key": "review_test", "value": "2", "description": "Reviewed threshold",
            "production_confirmed": "on", "_save": "Save",
        }).status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.updated_by, self.owner)
        self.assertEqual(AuditEvent.objects.filter(action="admin.setting.changed", entity_id=str(setting.pk)).count(), 1)
        supplier = Supplier.objects.create(name="Admin supplier")
        self.assertEqual(self.client.post(reverse("admin:hospital_supplier_change", args=[supplier.pk]), {
            "name": "Tampered supplier", "_save": "Save",
        }).status_code, 403)
        price = PriceVersion.objects.create(item=self.product, amount=Decimal("10"), reason="Approved", approved_by=self.owner)
        price_url = reverse("admin:hospital_priceversion_change", args=[price.pk])
        self.assertEqual(self.client.get(price_url).status_code, 200)
        self.assertEqual(self.client.post(price_url, {
            "item": self.product.pk, "amount": "1.00", "reason": "Tamper", "_save": "Save",
        }).status_code, 403)
        price.refresh_from_db()
        self.assertEqual(price.amount, Decimal("10"))

    def test_duplicate_mpesa_reference_is_rejected(self):
        order1 = self.prepare(1)
        record_payment(actor=self.reception, invoice_id=order1.invoice_id, amount=5, method="mpesa", reference="QAA123", idempotency_key="mpesa-1")
        order2 = self.prepare(1)
        with self.assertRaises(ValidationError):
            record_payment(actor=self.reception, invoice_id=order2.invoice_id, amount=5, method="mpesa", reference="QAA123", idempotency_key="mpesa-2")

    def test_exact_payment_retry_after_full_settlement_returns_original_receipt(self):
        order = self.prepare(20)
        first = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=100,
            method=Payment.Method.CASH, reference="", idempotency_key="settled-once",
        )
        retry = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=Decimal("100.00"),
            method=Payment.Method.CASH, reference="", idempotency_key="settled-once",
        )
        self.assertEqual(retry.pk, first.pk)
        self.assertEqual(Payment.objects.filter(idempotency_key="settled-once").count(), 1)
        self.assertEqual(order.invoice.balance, Decimal("0"))

    def test_payment_request_key_cannot_be_reused_for_another_payment(self):
        first_order = self.prepare(20)
        second_order = self.prepare(20)
        record_payment(
            actor=self.reception, invoice_id=first_order.invoice_id, amount=50,
            method=Payment.Method.MPESA, reference="KEY-ORIGINAL", idempotency_key="shared-key",
        )
        variants = (
            (second_order.invoice_id, 50, Payment.Method.MPESA, "KEY-ORIGINAL"),
            (first_order.invoice_id, 40, Payment.Method.MPESA, "KEY-ORIGINAL"),
            (first_order.invoice_id, 50, Payment.Method.CASH, "KEY-ORIGINAL"),
            (first_order.invoice_id, 50, Payment.Method.MPESA, "KEY-CHANGED"),
        )
        for invoice_id, amount, method, reference in variants:
            with self.subTest(invoice_id=invoice_id, amount=amount, method=method, reference=reference):
                with self.assertRaisesMessage(ValidationError, "already used for a different payment"):
                    record_payment(
                        actor=self.reception, invoice_id=invoice_id, amount=amount,
                        method=method, reference=reference, idempotency_key="shared-key",
                    )
        self.assertEqual(Payment.objects.filter(idempotency_key="shared-key").count(), 1)

    def test_unverified_mpesa_does_not_settle_invoice_until_review(self):
        order = self.prepare(20)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=100,
            method=Payment.Method.MPESA, reference="PENDING-100", idempotency_key="pending-100",
        )
        order.invoice.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.invoice.paid_amount, Decimal("0"))
        self.assertEqual(order.invoice.pending_amount, Decimal("100"))
        self.assertEqual(order.invoice.balance, Decimal("100"))
        self.assertEqual(order.invoice.status, Invoice.Status.POSTED)
        self.assertEqual(order.status, PharmacyOrder.Status.PREPARED)
        self.client.force_login(self.reception)
        self.assertContains(self.client.get(reverse("receipt", kwargs={"pk": payment.pk})), "PENDING PAYMENT CLAIM")
        self.client.force_login(self.owner)
        report = self.client.get(reverse("reports"))
        self.assertEqual(report.context["receivables"], Decimal("100"))
        self.assertEqual(report.context["unverified_mpesa"], Decimal("100"))
        self.assertEqual(report.context["verified_collections"], Decimal("0"))

        review_mpesa(
            actor=self.reviewer, payment_id=payment.pk, approve=True,
            review_notes="Matched to the hospital M-PESA statement.",
        )
        order.invoice.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.invoice.paid_amount, Decimal("100"))
        self.assertEqual(order.invoice.pending_amount, Decimal("0"))
        self.assertEqual(order.invoice.balance, Decimal("0"))
        self.assertEqual(order.invoice.status, Invoice.Status.PAID)
        self.assertEqual(order.status, PharmacyOrder.Status.CLEARED)
        payment.refresh_from_db()
        self.assertEqual(payment.verification_status, Payment.Verification.MANUAL)

    def test_rejected_mpesa_remains_visible_without_settling_invoice(self):
        order = self.prepare(20)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=100,
            method=Payment.Method.MPESA, reference="INVALID-100", idempotency_key="invalid-100",
        )
        with self.assertRaisesMessage(ValidationError, "Record why"):
            review_mpesa(actor=self.reviewer, payment_id=payment.pk, approve=False)
        review_mpesa(
            actor=self.reviewer, payment_id=payment.pk, approve=False,
            review_notes="Reference absent from provider statement",
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REJECTED)
        self.assertEqual(payment.reviewed_by, self.reviewer)
        self.assertEqual(order.invoice.balance, Decimal("100"))
        self.assertEqual(order.invoice.pending_amount, Decimal("0"))
        self.client.force_login(self.reception)
        self.assertContains(self.client.get(reverse("receipt", kwargs={"pk": payment.pk})), "REJECTED PAYMENT CLAIM")
        self.assertTrue(ExceptionRecord.objects.filter(
            category="unverified_mpesa", status=ExceptionRecord.Status.RESOLVED,
        ).exists())
        with self.assertRaisesMessage(ValidationError, "already been reviewed"):
            review_mpesa(
                actor=self.reviewer, payment_id=payment.pk, approve=True,
                review_notes="Matched to the hospital M-PESA statement.",
            )

    def test_pending_mpesa_cannot_overpay_after_cash_settlement(self):
        order = self.prepare(20)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=100,
            method=Payment.Method.MPESA, reference="RACED-100", idempotency_key="raced-100",
        )
        record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=100,
            method=Payment.Method.CASH, reference="", idempotency_key="cash-after-pending",
        )
        with self.assertRaisesMessage(ValidationError, "Verification would exceed"):
            review_mpesa(
                actor=self.reviewer, payment_id=payment.pk, approve=True,
                review_notes="Matched to the hospital M-PESA statement.",
            )
        self.assertEqual(order.invoice.balance, Decimal("0"))
        self.assertEqual(order.invoice.paid_amount, Decimal("100"))
        review_mpesa(actor=self.reviewer, payment_id=payment.pk, approve=False, review_notes="Paid in cash instead")

    def test_mpesa_collection_is_reported_when_verified(self):
        order = self.prepare(20)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=100,
            method=Payment.Method.MPESA, reference="OLDER-CLAIM", idempotency_key="older-claim",
        )
        Payment.objects.filter(pk=payment.pk).update(received_at=timezone.now() - timedelta(days=30))
        review_mpesa(
            actor=self.reviewer, payment_id=payment.pk, approve=True,
            review_notes="Matched to the hospital M-PESA statement.",
        )
        self.client.force_login(self.owner)
        report = self.client.get(reverse("reports"), {"days": "7"})
        self.assertEqual(report.context["verified_collections"], Decimal("100"))

    def test_refund_credit_requires_independent_reviewer(self):
        order = self.prepare(2)
        note = CreditNote.objects.create(invoice=order.invoice, amount=5, reason="Test correction", requested_by=self.reception)
        with self.assertRaises(ValidationError):
            approve_credit_note(actor=self.reception, credit_note_id=note.pk, approve=True)
        approve_credit_note(actor=self.reviewer, credit_note_id=note.pk, approve=True)
        note.refresh_from_db()
        self.assertEqual(note.status, CreditNote.Status.APPROVED)
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"), "Financial credit must not return stock")

    def test_two_credits_cannot_exceed_one_invoice_balance(self):
        order = self.prepare(20)
        first = CreditNote.objects.create(invoice=order.invoice, amount=60, reason="First", requested_by=self.reception)
        second = CreditNote.objects.create(invoice=order.invoice, amount=60, reason="Second", requested_by=self.reception)
        approve_credit_note(actor=self.reviewer, credit_note_id=first.pk, approve=True)
        with self.assertRaisesMessage(ValidationError, "exceeds the current invoice balance"):
            approve_credit_note(actor=self.reviewer, credit_note_id=second.pk, approve=True)
        second.refresh_from_db()
        self.assertEqual(second.status, CreditNote.Status.PENDING)
        self.assertEqual(order.invoice.balance, Decimal("40"))

    def test_payment_after_credit_cannot_exceed_reduced_balance(self):
        order = self.prepare(20)
        note = CreditNote.objects.create(invoice=order.invoice, amount=60, reason="Correction", requested_by=self.reception)
        approve_credit_note(actor=self.reviewer, credit_note_id=note.pk, approve=True)
        with self.assertRaisesMessage(ValidationError, "exceeds the outstanding balance"):
            record_payment(
                actor=self.reception, invoice_id=order.invoice_id, amount=60,
                method=Payment.Method.CASH, reference="", idempotency_key="post-credit-overpay",
            )
        self.assertEqual(order.invoice.balance, Decimal("40"))

    def test_net_billed_uses_credit_approval_date_on_dashboard_and_report(self):
        current = self.prepare(20)
        current_credit = CreditNote.objects.create(
            invoice=current.invoice, amount=20, reason="Current correction", requested_by=self.reception,
        )
        approve_credit_note(actor=self.reviewer, credit_note_id=current_credit.pk, approve=True)

        older = self.prepare(10)
        Invoice.objects.filter(pk=older.invoice_id).update(posted_at=timezone.now() - timedelta(days=30))
        older_credit = CreditNote.objects.create(
            invoice=older.invoice, amount=10, reason="Earlier charge correction", requested_by=self.reception,
        )
        approve_credit_note(actor=self.reviewer, credit_note_id=older_credit.pk, approve=True)

        self.client.force_login(self.owner)
        dashboard = self.client.get(reverse("dashboard"))
        report = self.client.get(reverse("reports"), {"days": "7"})
        self.assertEqual(dashboard.context["net_billed"], Decimal("70"))
        self.assertEqual(report.context["net_billed"], Decimal("70"))
        self.assertContains(report, "Charges posted less credits approved in period")

    def test_bilateral_case_accrues_one_case_fee(self):
        case = EyeCase.objects.create(
            patient=self.patient, proposed_procedure="Unspecified eye procedure", eye="both",
            readiness="ready", payment_status="paid", package_price=24000,
        )
        complete_eye_case(actor=self.clinician, case_id=case.pk)
        case.refresh_from_db()
        completed_at = case.completed_at
        complete_eye_case(actor=self.clinician, case_id=case.pk)
        self.assertEqual(case.payable.amount, Decimal("2000"))
        self.assertEqual(type(case).objects.get(pk=case.pk).payable.amount, Decimal("2000"))
        case.refresh_from_db()
        self.assertEqual(case.completed_at, completed_at)
        self.assertEqual(AuditEvent.objects.filter(action="eye_case.completed", entity_id=str(case.pk)).count(), 1)

    def test_unpaid_or_cancelled_eye_case_cannot_accrue_a_payable(self):
        case = EyeCase.objects.create(
            patient=self.patient, proposed_procedure="Eye procedure", eye="left",
            readiness="ready", package_price=24000,
        )
        with self.assertRaisesMessage(ValidationError, "Payment clearance"):
            complete_eye_case(actor=self.clinician, case_id=case.pk)
        self.client.force_login(self.clinician)
        self.assertNotContains(self.client.get(reverse("eye_clinic")), "Complete case")
        case.payment_status = "paid"
        case.save(update_fields=["payment_status"])
        self.assertContains(self.client.get(reverse("eye_clinic")), "Complete case")
        case.status = "cancelled"
        case.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "cancelled eye case"):
            complete_eye_case(actor=self.clinician, case_id=case.pk)
        self.assertFalse(ClinicianPayable.objects.filter(eye_case=case).exists())

    def test_purchase_requester_cannot_self_approve(self):
        supplier = Supplier.objects.create(name="Demo Supplier")
        po = PurchaseOrder.objects.create(supplier=supplier, requested_by=self.procurement)
        with self.assertRaises(ValidationError):
            approve_purchase_order(actor=self.procurement, order_id=po.pk)
        approve_purchase_order(actor=self.reviewer, order_id=po.pk)
        po.refresh_from_db()
        self.assertEqual(po.status, "approved")

    def test_cancelled_or_received_purchase_order_cannot_be_reapproved(self):
        supplier = Supplier.objects.create(name="Transition Supplier")
        order = PurchaseOrder.objects.create(supplier=supplier, requested_by=self.procurement, status="cancelled")
        with self.assertRaisesMessage(ValidationError, "Only a requested purchase order"):
            approve_purchase_order(actor=self.reviewer, order_id=order.pk)
        order.status = "received"
        order.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "Only a requested purchase order"):
            approve_purchase_order(actor=self.reviewer, order_id=order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, "received")

    def test_encounter_closure_clears_queue_without_erasing_debt(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        invoice = Invoice.objects.create(
            patient=self.patient, encounter=encounter, status=Invoice.Status.POSTED,
            posted_at=timezone.now(), created_by=self.reception,
        )
        InvoiceLine.objects.create(
            invoice=invoice, item=self.product, description="Visit charge",
            department="Outpatient", quantity=1, unit_price=100,
        )
        with self.assertRaisesMessage(ValidationError, "Only a clinician"):
            close_encounter(actor=self.reception, encounter_id=encounter.pk, reason="Done")
        self.client.force_login(self.clinician)
        self.assertContains(self.client.get(reverse("queue")), self.patient.full_name)
        response = self.client.post(
            reverse("encounter_close", kwargs={"encounter_id": encounter.pk}),
            {"reason": "Care completed"}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        encounter.refresh_from_db()
        self.assertEqual(encounter.status, Encounter.Status.CLOSED)
        self.assertEqual(encounter.closed_by, self.clinician)
        self.assertNotContains(response, self.patient.full_name)
        self.assertEqual(invoice.balance, Decimal("100"))
        closed_at = encounter.closed_at
        close_encounter(actor=self.clinician, encounter_id=encounter.pk, reason="Repeated request")
        encounter.refresh_from_db()
        self.assertEqual(encounter.closed_at, closed_at)
        self.assertEqual(AuditEvent.objects.filter(action="encounter.closed", entity_id=str(encounter.pk)).count(), 1)

    def test_clinical_discharge_frees_bed_and_keeps_finance_separate(self):
        ward = Ward.objects.create(name="Test ward")
        bed = Bed.objects.create(ward=ward, label="Bed 1")
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        admission = Admission.objects.create(
            patient=self.patient, encounter=encounter, bed=bed, admitted_by=self.clinician,
        )
        with self.assertRaisesMessage(ValidationError, "Only a clinician"):
            discharge_admission(actor=self.reception, admission_id=admission.pk, summary="Stable")
        self.client.force_login(self.clinician)
        self.assertNotContains(self.client.get(reverse("queue")), self.patient.full_name)
        self.assertContains(self.client.get(reverse("wards")), "Discharge clinically")
        response = self.client.post(
            reverse("admission_discharge", kwargs={"pk": admission.pk}),
            {"summary": "Stable for home care"}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        admission.refresh_from_db()
        encounter.refresh_from_db()
        self.assertEqual(admission.clinical_status, "discharged")
        self.assertEqual(admission.financial_status, "open")
        self.assertEqual(admission.discharged_by, self.clinician)
        self.assertEqual(encounter.status, Encounter.Status.CLOSED)
        self.assertContains(response, "Available")
        self.assertNotContains(response, "Discharge clinically")
        discharged_at = admission.discharged_at
        discharge_admission(actor=self.clinician, admission_id=admission.pk, summary="Repeated request")
        admission.refresh_from_db()
        self.assertEqual(admission.discharged_at, discharged_at)
        self.assertEqual(AuditEvent.objects.filter(action="admission.discharged", entity_id=str(admission.pk)).count(), 1)

    def test_signed_note_cannot_be_signed_twice(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        note = ClinicalNote.objects.create(encounter=encounter, author=self.clinician, assessment="Test")
        note.sign()
        with self.assertRaises(ValidationError):
            note.sign()

    def test_cashier_cannot_open_clinical_note_endpoint(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        self.client.login(username=self.reception.username, password=self.password)
        response = self.client.get(reverse("clinical_note", kwargs={"encounter_id": encounter.pk}))
        self.assertEqual(response.status_code, 403)

    def test_product_csv_dry_run_and_commit_are_idempotent(self):
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"code,name,department,base_unit,sale_unit,units_per_sale_unit,sale_price,reorder_level,prescription_required\n"
            b"CSV-001,CSV test product,Pharmacy,tablet,box,100,7.50,20,false\n"
        )
        response = self.client.post(reverse("csv_import"), {"import_kind": "products", "csv_file": SimpleUploadedFile("products.csv", csv_bytes, content_type="text/csv")})
        self.assertEqual(response.status_code, 200)
        job = ImportJob.objects.get(filename="products.csv")
        self.assertEqual(job.error_count, 0)
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        self.assertEqual(CatalogueItem.objects.filter(code="CSV-001").count(), 1)

    def test_product_csv_duplicate_code_fails_dry_run(self):
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"code,name,department,base_unit,sale_unit,units_per_sale_unit,sale_price,reorder_level,prescription_required\n"
            b"CSV-DUP,First,Pharmacy,tablet,box,100,7.50,20,false\n"
            b"CSV-DUP,Second,Pharmacy,tablet,box,100,7.50,20,false\n"
        )
        response = self.client.post(reverse("csv_import"), {
            "import_kind": "products",
            "csv_file": SimpleUploadedFile("duplicate-products.csv", csv_bytes, content_type="text/csv"),
        })
        self.assertEqual(response.status_code, 200)
        job = ImportJob.objects.get(filename="duplicate-products.csv")
        self.assertEqual(job.error_count, 1)
        self.assertIn("duplicated in this file", job.report["errors"][0]["error"])
        self.assertFalse(CatalogueItem.objects.filter(code="CSV-DUP").exists())

    def test_service_result_requires_content_before_release(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        service = CatalogueItem.objects.create(code="LAB-T", name="Lab test", kind="service", department="Laboratory", base_unit="service", sale_unit="service", units_per_sale_unit=1)
        PriceVersion.objects.create(item=service, amount=Decimal("150"), reason="Test", approved_by=self.owner)
        order = ServiceOrder.objects.create(encounter=encounter, service=service, requested_by=self.clinician)
        lab = self.make_user("lab", Role.LAB)
        self.client.login(username=lab.username, password=self.password)
        url = reverse("service_order_update", kwargs={"pk": order.pk})
        response = self.client.post(url, {"status": "in_progress", "result": ""})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(url, {"status": "review", "result": ""})
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.Status.IN_PROGRESS)
        response = self.client.post(url, {"status": "review", "result": "Fictional demonstration result"})
        self.assertEqual(response.status_code, 302)
        reviewer = self.make_user("lab-reviewer", Role.LAB)
        self.client.force_login(reviewer)
        response = self.client.post(url, {"status": "released", "result": "Fictional demonstration result"})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.Status.RELEASED)
        self.assertEqual(order.reviewed_by, reviewer)
        self.assertIsNotNone(order.released_at)
        self.assertEqual(order.charge_line.invoice.balance, Decimal("150"))

    def test_released_service_has_one_versioned_charge_and_keeps_clinical_release_separate_from_debt(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        service = CatalogueItem.objects.create(
            code="LAB-CHARGE", name="Blood panel", kind=CatalogueItem.Kind.SERVICE,
            department="Laboratory", base_unit="service", sale_unit="service", units_per_sale_unit=1,
        )
        price = PriceVersion.objects.create(item=service, amount=Decimal("250"), reason="Approved tariff", approved_by=self.owner)
        order = ServiceOrder.objects.create(encounter=encounter, service=service, requested_by=self.clinician)
        lab = self.make_user("billing-lab", Role.LAB)
        reviewer = self.make_user("billing-reviewer", Role.LAB)
        with self.assertRaisesMessage(ValidationError, "next review step"):
            update_service_order(
                actor=reviewer, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Normal",
            )
        update_service_order(actor=lab, order_id=order.pk, status=ServiceOrder.Status.IN_PROGRESS, result="")
        update_service_order(actor=lab, order_id=order.pk, status=ServiceOrder.Status.REVIEW, result="Normal")
        with self.assertRaisesMessage(ValidationError, "cannot release their own result"):
            update_service_order(
                actor=self.clinician, order_id=order.pk, status=ServiceOrder.Status.RELEASED,
                result="Normal", request=None,
            )
        with self.assertRaisesMessage(ValidationError, "cannot release their own result"):
            update_service_order(actor=lab, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Normal")
        with self.assertRaisesMessage(ValidationError, "without editing"):
            update_service_order(actor=reviewer, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Changed")
        update_service_order(actor=reviewer, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Normal")
        order.refresh_from_db()
        line = order.charge_line
        self.assertEqual(order.reviewed_by, reviewer)
        self.assertEqual(line.price_version, price)
        self.assertEqual(line.unit_price, Decimal("250"))
        self.assertEqual(line.invoice.status, Invoice.Status.POSTED)
        self.assertEqual(line.invoice.balance, Decimal("250"))
        self.assertEqual(line.invoice.patient, self.patient)
        self.assertEqual(line.invoice.encounter, encounter)
        self.client.force_login(self.reception)
        self.assertContains(
            self.client.get(reverse("patient_detail", kwargs={"pk": self.patient.pk})),
            reverse("invoice_payment", kwargs={"pk": line.invoice_id}),
        )
        released_at = order.released_at
        update_service_order(actor=reviewer, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Normal")
        order.refresh_from_db()
        self.assertEqual(order.released_at, released_at)
        self.assertEqual(order.result, "Normal")
        self.assertEqual(InvoiceLine.objects.filter(service_order=order).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="service_order.released", entity_id=str(order.pk)).count(), 1)

    def test_service_without_approved_price_cannot_be_released_unbilled(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        service = CatalogueItem.objects.create(
            code="LAB-UNPRICED", name="Unpriced test", kind=CatalogueItem.Kind.SERVICE,
            department="Laboratory", base_unit="service", sale_unit="service", units_per_sale_unit=1,
        )
        order = ServiceOrder.objects.create(encounter=encounter, service=service, requested_by=self.clinician)
        lab = self.make_user("unpriced-lab", Role.LAB)
        reviewer = self.make_user("unpriced-reviewer", Role.LAB)
        update_service_order(actor=lab, order_id=order.pk, status=ServiceOrder.Status.IN_PROGRESS, result="")
        update_service_order(actor=lab, order_id=order.pk, status=ServiceOrder.Status.REVIEW, result="Result")
        with self.assertRaisesMessage(ValidationError, "no active approved price"):
            update_service_order(actor=reviewer, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Result")
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.Status.REVIEW)
        self.assertFalse(InvoiceLine.objects.filter(service_order=order).exists())

    def test_outpatient_prescription_prices_then_dispenses_actual_quantity(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception, status=Encounter.Status.CLINICIAN)
        self.client.login(username=self.clinician.username, password=self.password)
        response = self.client.post(reverse("prescription_create", kwargs={"encounter_id": encounter.pk}), {
            "product": self.product.pk, "strength": "As labelled", "dose": "Clinician-entered dose",
            "route": "Clinician-entered route", "frequency": "Clinician-entered frequency",
            "duration": "Clinician-entered duration", "quantity_base_units": "3", "instructions": "Fictional test only",
        })
        self.assertEqual(response.status_code, 302)
        prescription = encounter.prescriptions.get()
        self.client.logout()
        self.client.login(username=self.pharmacist.username, password=self.password)
        response = self.client.post(reverse("pharmacy_prepare_prescription", kwargs={"prescription_id": prescription.pk}))
        self.assertEqual(response.status_code, 302)
        order = PharmacyOrder.objects.get(prescription=prescription)
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=15, method="cash", reference="", idempotency_key="rx-payment")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="rx-dispense")
        prescription.items.get().refresh_from_db()
        self.assertEqual(prescription.items.get().dispensed_quantity, Decimal("3"))
        self.assertEqual(self.batch.quantity_on_hand, Decimal("197"))


@skipUnless(connection.vendor == "postgresql", "Row-lock races require PostgreSQL")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InvoiceBalanceRaceTests(HospitalFixtureMixin, TransactionTestCase):
    def run_race(self, first, second):
        start = Barrier(2)

        def run(action):
            close_old_connections()
            try:
                start.wait(timeout=10)
                try:
                    action()
                    return "applied"
                except ValidationError:
                    return "rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (first, second)))
        self.assertCountEqual(results, ["applied", "rejected"])

    def test_two_credit_approvals_race_for_one_balance(self):
        order = self.prepare(20)
        notes = [
            CreditNote.objects.create(invoice=order.invoice, amount=60, reason=f"Correction {index}", requested_by=self.reception)
            for index in range(2)
        ]
        self.run_race(
            lambda: approve_credit_note(actor=self.reviewer, credit_note_id=notes[0].pk, approve=True),
            lambda: approve_credit_note(actor=self.reviewer, credit_note_id=notes[1].pk, approve=True),
        )
        self.assertEqual(order.invoice.balance, Decimal("40"))
        self.assertEqual(CreditNote.objects.filter(invoice=order.invoice, status=CreditNote.Status.APPROVED).count(), 1)

    def test_credit_approval_and_payment_race_for_one_balance(self):
        order = self.prepare(20)
        note = CreditNote.objects.create(invoice=order.invoice, amount=60, reason="Correction", requested_by=self.reception)
        self.run_race(
            lambda: approve_credit_note(actor=self.reviewer, credit_note_id=note.pk, approve=True),
            lambda: record_payment(
                actor=self.reception, invoice_id=order.invoice_id, amount=60,
                method=Payment.Method.CASH, reference="", idempotency_key="raced-cash-payment",
            ),
        )
        self.assertEqual(order.invoice.balance, Decimal("40"))
        self.assertEqual(
            CreditNote.objects.filter(invoice=order.invoice, status=CreditNote.Status.APPROVED).count()
            + PaymentAllocation.objects.filter(invoice=order.invoice).count(), 1,
        )


@skipUnless(connection.vendor == "postgresql", "Concurrent increments require PostgreSQL")
class ExceptionRecurrenceRaceTests(TransactionTestCase):
    def test_simultaneous_recurrences_are_all_counted(self):
        from .services import raise_exception

        record = raise_exception("race", "Recurring issue", "initial")
        start = Barrier(2)

        def recur(index):
            close_old_connections()
            try:
                start.wait(timeout=10)
                raise_exception("race", "Recurring issue", f"recurrence {index}")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(recur, (1, 2)))
        record.refresh_from_db()
        self.assertEqual(record.occurrence_count, 3)


@skipUnless(connection.vendor == "postgresql", "Audit trigger requires PostgreSQL")
class PostgreSQLAuditTriggerTests(TransactionTestCase):
    def test_raw_update_and_delete_are_blocked_by_database_trigger(self):
        event = AuditEvent.objects.create(action="trigger.probe", entity_type="Test", entity_id="1")
        for sql, params in (
            ("UPDATE hospital_auditevent SET action = %s WHERE id = %s", ["tampered", event.pk]),
            ("DELETE FROM hospital_auditevent WHERE id = %s", [event.pk]),
        ):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(sql, params)
        event.refresh_from_db()
        self.assertEqual(event.action, "trigger.probe")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RegressionTests(HospitalFixtureMixin, TestCase):
    """Regressions for defects found in the September 2026 audit.

    Each test fails against the pre-audit code; see docs/AUDIT_2026-09-18.md.
    """

    def test_stale_clinical_draft_cannot_overwrite_or_sign(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        url = reverse("clinical_note", kwargs={"encounter_id": encounter.pk})
        self.client.force_login(self.clinician)
        self.assertEqual(self.client.get(url).context["form"].initial["expected_revision"], 0)
        first = self.client.post(url, {"expected_revision": "0", "assessment": "Initial", "action": "save"})
        self.assertEqual(first.status_code, 302)
        note = ClinicalNote.objects.get(encounter=encounter)
        self.assertEqual(note.revision, 1)

        stale_new_tab = self.client.post(url, {
            "expected_revision": "0", "assessment": "Stale new tab", "action": "save",
        })
        self.assertContains(stale_new_tab, "changed in another tab")
        note.refresh_from_db()
        self.assertEqual(note.assessment, "Initial")

        tab_revision = self.client.get(url).context["form"].initial["expected_revision"]
        self.assertEqual(tab_revision, 1)
        self.assertEqual(self.client.post(url, {
            "expected_revision": str(tab_revision), "assessment": "Saved from first tab", "action": "save",
        }).status_code, 302)
        stale_sign = self.client.post(url, {
            "expected_revision": str(tab_revision), "assessment": "Text from second tab", "action": "sign",
        })
        self.assertContains(stale_sign, "changed in another tab")
        self.assertContains(stale_sign, "Text from second tab")
        note.refresh_from_db()
        self.assertEqual(note.assessment, "Saved from first tab")
        self.assertEqual(note.revision, 2)
        self.assertEqual(note.status, ClinicalNote.Status.DRAFT)

        signed = self.client.post(url, {
            "expected_revision": "2", "assessment": "Final signed text", "action": "sign",
        })
        self.assertEqual(signed.status_code, 302)
        note.refresh_from_db()
        self.assertEqual(note.status, ClinicalNote.Status.SIGNED)
        self.assertEqual(note.revision, 3)

    def test_signed_note_amendment_preserves_original_and_records_reason(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        url = reverse("clinical_note", kwargs={"encounter_id": encounter.pk})
        self.client.force_login(self.clinician)
        self.assertEqual(self.client.post(url, {
            "expected_revision": "0", "assessment": "Original assessment", "action": "sign",
        }).status_code, 302)
        original = ClinicalNote.objects.get(encounter=encounter)
        amendment_form = self.client.get(url).context["form"]
        self.assertEqual(amendment_form.initial["assessment"], "Original assessment")
        self.assertEqual(amendment_form.initial["expected_parent_note_id"], original.pk)
        amendment = {
            "expected_revision": "0", "expected_parent_note_id": str(original.pk),
            "assessment": "Corrected assessment", "action": "sign",
        }
        self.assertContains(self.client.post(url, amendment), "This field is required")
        self.assertEqual(ClinicalNote.objects.filter(encounter=encounter).count(), 1)
        amendment["amendment_reason"] = "Corrected transcription error"
        self.assertEqual(self.client.post(url, amendment).status_code, 302)
        original.refresh_from_db()
        revised = ClinicalNote.objects.get(encounter=encounter, parent_note=original)
        self.assertEqual(original.status, ClinicalNote.Status.AMENDED)
        self.assertEqual(original.assessment, "Original assessment")
        self.assertEqual(revised.status, ClinicalNote.Status.SIGNED)
        self.assertEqual(revised.version, original.version + 1)
        self.assertEqual(revised.amendment_reason, "Corrected transcription error")
        self.assertEqual(AuditEvent.objects.filter(action="clinical_note.amended", entity_id=str(revised.pk)).count(), 1)

    def test_stale_pre_signature_tab_cannot_create_unlinked_amendment(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        url = reverse("clinical_note", kwargs={"encounter_id": encounter.pk})
        self.client.force_login(self.clinician)
        self.assertEqual(self.client.post(url, {
            "expected_revision": "0", "assessment": "Signed", "action": "sign",
        }).status_code, 302)
        response = self.client.post(url, {
            "expected_revision": "0", "expected_parent_note_id": "0", "assessment": "Stale tab",
            "amendment_reason": "Late edit", "action": "sign",
        })
        self.assertContains(response, "signed note changed in another tab")
        self.assertEqual(ClinicalNote.objects.filter(encounter=encounter).count(), 1)

    def test_note_signing_waits_for_pending_service_result(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        service = CatalogueItem.objects.create(
            code="LAB-NOTE", name="Note workflow test", kind=CatalogueItem.Kind.SERVICE,
            department="Laboratory", base_unit="service", sale_unit="service", units_per_sale_unit=1,
        )
        PriceVersion.objects.create(item=service, amount=Decimal("100"), reason="Approved", approved_by=self.owner)
        order = ServiceOrder.objects.create(encounter=encounter, service=service, requested_by=self.clinician)
        self.client.force_login(self.clinician)
        self.assertEqual(self.client.post(reverse("clinical_note", kwargs={"encounter_id": encounter.pk}), {
            "expected_revision": "0", "assessment": "Await laboratory result", "action": "sign",
        }).status_code, 302)
        encounter.refresh_from_db()
        self.assertEqual(encounter.status, Encounter.Status.TESTS)
        performer = self.make_user("note-lab", Role.LAB)
        reviewer = self.make_user("note-reviewer", Role.LAB)
        update_service_order(actor=performer, order_id=order.pk, status=ServiceOrder.Status.IN_PROGRESS, result="")
        update_service_order(actor=performer, order_id=order.pk, status=ServiceOrder.Status.REVIEW, result="Normal")
        update_service_order(actor=reviewer, order_id=order.pk, status=ServiceOrder.Status.RELEASED, result="Normal")
        encounter.refresh_from_db()
        self.assertEqual(encounter.status, Encounter.Status.PHARMACY)

    # C1 — the lock screen was a no-op: the middleware read resolver_match
    # before URL resolution, and unlocked_required was applied to no view.
    def test_locked_session_cannot_reach_clinical_screens(self):
        self.client.login(username=self.clinician.username, password=self.password)
        profile = self.clinician.staff_profile
        profile.locked_at = timezone.now()
        profile.save(update_fields=["locked_at"])
        response = self.client.get(reverse("queue"))
        self.assertRedirects(response, reverse("screen_unlock"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("screen_unlock")).status_code, 200)

    def test_unlocking_restores_access(self):
        self.client.login(username=self.clinician.username, password=self.password)
        profile = self.clinician.staff_profile
        profile.locked_at = timezone.now()
        profile.save(update_fields=["locked_at"])
        self.client.post(reverse("screen_unlock"), {"password": self.password})
        self.assertEqual(self.client.get(reverse("queue")).status_code, 200)

    def test_unlock_failures_obey_account_lockout_even_with_correct_password(self):
        self.client.force_login(self.clinician)
        profile = self.clinician.staff_profile
        profile.locked_at = timezone.now()
        profile.save(update_fields=["locked_at"])
        url = reverse("screen_unlock")
        for _ in range(LoginAttempt.LOCKOUT_THRESHOLD):
            self.client.post(url, {"password": "incorrect"})
        self.assertEqual(LoginAttempt.recent_failures(self.clinician.username), LoginAttempt.LOCKOUT_THRESHOLD)
        response = self.client.post(url, {"password": self.password})
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many failed attempts", status_code=429)
        profile.refresh_from_db()
        self.assertIsNotNone(profile.locked_at)
        LoginAttempt.objects.filter(username=self.clinician.username).update(
            attempted_at=timezone.now() - timedelta(minutes=LoginAttempt.LOCKOUT_WINDOW_MINUTES + 1)
        )
        self.assertEqual(self.client.post(url, {"password": self.password}).status_code, 302)
        profile.refresh_from_db()
        self.assertIsNone(profile.locked_at)

    def test_unlock_obeys_address_lockout(self):
        self.client.force_login(self.clinician)
        for index in range(LoginAttempt.ADDRESS_LOCKOUT_THRESHOLD):
            LoginAttempt.objects.create(username=f"other-{index}", ip_address="127.0.0.1")
        response = self.client.post(reverse("screen_unlock"), {"password": self.password})
        self.assertEqual(response.status_code, 429)

    # C2 — two lines for the same product each read the untouched batch
    # balance, so one order could dispense more than the hospital held.
    def test_repeated_product_lines_cannot_overdraw_a_batch(self):
        order = prepare_pharmacy_order(
            actor=self.pharmacist, customer_name="Walk-in", patient=None,
            items=[(self.product, Decimal("150")), (self.product, Decimal("150"))])
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=Decimal("1500"),
                       method="cash", reference="", idempotency_key="overdraw-pay")
        with self.assertRaisesMessage(ValidationError, "Insufficient valid stock"):
            dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="overdraw-disp")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, Decimal("200"))

    def test_repeated_product_lines_within_stock_still_dispense(self):
        order = prepare_pharmacy_order(
            actor=self.pharmacist, customer_name="Walk-in", patient=None,
            items=[(self.product, Decimal("60")), (self.product, Decimal("60"))])
        record_payment(actor=self.reception, invoice_id=order.invoice_id, amount=Decimal("600"),
                       method="cash", reference="", idempotency_key="split-pay")
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="split-disp")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, Decimal("80"))

    # C3 — order_by("urgency") sorts alphabetically, placing urgent after routine.
    def test_queue_ranks_urgent_above_routine(self):
        for urgency in ["routine", "urgent", "emergency"]:
            Encounter.objects.create(
                patient=self.patient, started_by=self.reception, urgency=urgency,
                emergency_override_reason="Documented" if urgency == "emergency" else "")
        self.client.login(username=self.clinician.username, password=self.password)
        listed = self.client.get(reverse("queue")).context["encounters"]
        self.assertEqual([e.urgency for e in listed], ["emergency", "urgent", "routine"])

    # C4 — save() guarded the audit trail; the ORM bulk paths did not.
    def test_audit_events_reject_bulk_update_and_delete(self):
        from .models import AuditEvent
        event = AuditEvent.objects.create(actor=self.owner, action="probe", entity_type="Test", entity_id="1")
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            event.delete()
        event.refresh_from_db()
        self.assertEqual(event.action, "probe")

    # C5 — unlimited password attempts against an unauthenticated endpoint.
    def test_repeated_failed_logins_lock_the_username(self):
        for _ in range(LoginAttempt.LOCKOUT_THRESHOLD):
            self.client.post(reverse("login"), {"username": "clinician", "password": "wrong"})
        blocked = self.client.post(reverse("login"), {"username": "clinician", "password": "wrong"})
        self.assertEqual(blocked.status_code, 429)
        correct = self.client.post(reverse("login"), {"username": "clinician", "password": self.password})
        self.assertEqual(correct.status_code, 429, "A locked username must not fall through on a correct password")

    def test_successful_login_clears_earlier_failures(self):
        self.client.post(reverse("login"), {"username": "clinician", "password": "wrong"})
        self.client.post(reverse("login"), {"username": "clinician", "password": self.password})
        self.assertEqual(LoginAttempt.recent_failures("clinician"), 0)

    # C6 — reception could not find a patient by the identifier on their ID card.
    def test_patient_search_matches_national_id(self):
        Patient.objects.create(first_name="Aisha", last_name="Wafula", estimated_age_years=41,
                               id_number="24681012", registered_by=self.reception)
        self.client.login(username=self.reception.username, password=self.password)
        response = self.client.get(reverse("patients"), {"q": "24681012"})
        self.assertContains(response, "Aisha")

    # C7 — one aggregate query per batch and two per open invoice, so the
    # owner dashboard and stock ledger got slower as the hospital used them.
    def _seed_ledger(self, batches, orders, tag):
        for index in range(batches):
            batch = StockBatch.objects.create(
                item=self.product, batch_number=f"{tag}-{index}",
                expiry_date=timezone.localdate() + timedelta(days=200),
                purchase_cost_per_base_unit=Decimal("1.00"))
            StockMovement.objects.create(
                batch=batch, movement_type="receipt", quantity_delta=5, reference_type="Opening",
                reference_id="1", idempotency_key=f"{tag}-{index}", entered_by=self.pharmacist)
        for index in range(orders):
            prepare_pharmacy_order(actor=self.pharmacist, customer_name=f"{tag} {index}",
                                   patient=None, items=[(self.product, Decimal("1"))])

    def _count_queries(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        return len(captured)

    def test_owner_dashboard_cost_does_not_grow_with_the_ledger(self):
        self.client.login(username=self.owner.username, password=self.password)
        self._seed_ledger(5, 3, "small")
        small = self._count_queries(reverse("dashboard"))
        self._seed_ledger(40, 25, "large")
        large = self._count_queries(reverse("dashboard"))
        self.assertEqual(small, large, "Dashboard query count must not scale with rows")
        self.assertLess(large, 30)

    def test_stock_page_cost_does_not_grow_with_the_ledger(self):
        self.client.login(username=self.pharmacist.username, password=self.password)
        self._seed_ledger(5, 0, "stock-small")
        small = self._count_queries(reverse("stock"))
        self._seed_ledger(40, 0, "stock-large")
        large = self._count_queries(reverse("stock"))
        self.assertEqual(small, large, "Stock ledger query count must not scale with batches")
        self.assertLess(large, 15)

    def _seed_orders(self, count, tag):
        supplier, _ = Supplier.objects.get_or_create(name="Query Supplies")
        for index in range(count):
            order = PurchaseOrder.objects.create(
                supplier=supplier, requested_by=self.procurement, reference=f"{tag}-{index}"
            )
            PurchaseOrderLine.objects.create(
                order=order, item=self.product,
                quantity_base_units=Decimal("10"), quoted_unit_cost=Decimal("1.00"),
            )

    def test_purchasing_page_cost_does_not_grow_with_the_order_book(self):
        # The page prints the requester's and approver's staff profiles per row.
        # Neither was on the select_related chain, so every purchase order cost
        # two extra queries.
        self.client.login(username=self.owner.username, password=self.password)
        self._seed_orders(3, "small")
        small = self._count_queries(reverse("purchasing"))
        self._seed_orders(25, "large")
        large = self._count_queries(reverse("purchasing"))
        self.assertEqual(small, large, "Purchasing query count must not scale with orders")
        self.assertLess(large, 15)

    def test_deliveries_page_cost_does_not_grow_with_the_receipt_history(self):
        self.client.login(username=self.procurement.username, password=self.password)
        self._seed_orders(3, "deliveries-small")
        small = self._count_queries(reverse("deliveries"))
        self._seed_orders(25, "deliveries-large")
        large = self._count_queries(reverse("deliveries"))
        self.assertEqual(small, large, "Deliveries query count must not scale with orders")
        self.assertLess(large, 20)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DemoSeedTests(TestCase):
    """The documented quick start must actually run.

    ``seed_demo`` assigned roles to a freshly fetched StaffProfile while the
    User instance it kept still carried the default-role profile the post_save
    signal had attached. Every later role check read the stale copy, so the
    command aborted partway through and the demo environment could not be
    created at all.
    """

    def test_demo_seed_completes_and_assigns_roles(self):
        call_command("seed_demo")
        pharmacist = User.objects.get(username="pharmacy.demo")
        self.assertEqual(user_role(pharmacist), Role.PHARMACY)
        self.assertEqual(user_role(User.objects.get(username="owner.demo")), Role.OWNER)
        self.assertTrue(PharmacyOrder.objects.filter(customer_name="Demo Walk-In Customer").exists())

    def test_demo_seed_is_idempotent(self):
        call_command("seed_demo")
        call_command("seed_demo")
        self.assertEqual(User.objects.filter(username="pharmacy.demo").count(), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(reference_id="DEMO-WITNESSED-001").count(), 3)

    def test_demo_seed_leaves_an_approved_order_ready_to_receive(self):
        call_command("seed_demo")
        order = PurchaseOrder.objects.get()
        self.assertEqual(order.status, "approved")
        self.assertNotEqual(order.approved_by_id, order.requested_by_id)
        self.assertEqual(order.lines.count(), 2)


class ContinuousIntegrationTests(SimpleTestCase):
    """Keep the local demo checks and the separate PostgreSQL CI job explicit.

    Every GitHub Actions run in this repository has failed within seconds
    without a runner, because the account is billing-locked; no commit can
    clear that. scripts/checks.sh mirrors the fast demo job; README documents
    how to reproduce the PostgreSQL job with a disposable local database.
    """

    workflow = Path(settings.BASE_DIR) / ".github" / "workflows" / "quality.yml"
    script = Path(settings.BASE_DIR) / "scripts" / "checks.sh"

    def test_demo_and_ci_install_the_same_pinned_dependencies(self):
        workflow = self.workflow.read_text()
        self.assertEqual(workflow.count("python -m pip install -r requirements.lock"), 2)
        self.assertEqual(workflow.count("cache-dependency-path: requirements.lock"), 2)
        for name in ("run-demo.ps1", "run-demo.cmd", "run-demo.sh"):
            with self.subTest(name=name):
                script = (settings.BASE_DIR / "scripts" / name).read_text()
                self.assertIn("-r requirements.lock", script)

    def workflow_commands(self):
        """Every `run:` step, including the folded (`>-`) multi-line ones.

        Reading only the first line treats the YAML fold marker itself as the
        command, so a step written across several lines silently stops being
        compared at all — the drift this test exists to catch would go
        unnoticed precisely when a step grew complicated enough to matter.
        """
        commands = []
        lines = self.workflow.read_text().splitlines()
        index = 0
        while index < len(lines):
            stripped = lines[index].strip()
            if not stripped.startswith("- run:"):
                index += 1
                continue
            command = stripped[len("- run:"):].strip()
            if command in {">-", ">", "|", "|-"}:
                indent = len(lines[index]) - len(lines[index].lstrip())
                parts = []
                index += 1
                while index < len(lines):
                    nxt = lines[index]
                    if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                        break
                    parts.append(nxt.strip())
                    index += 1
                command = " ".join(part for part in parts if part)
            else:
                index += 1
            if command.startswith("python -m pip install"):
                continue  # Dependency installation, not a check.
            commands.append(command)
        return commands

    def test_the_workflow_still_runs_the_checks_we_think_it_does(self):
        commands = self.workflow_commands()
        self.assertEqual(len(commands), 6, f"Unexpected CI step count: {commands}")
        self.assertEqual(shlex.split(commands[4]), ["python", "manage.py", "migrate", "--noinput"])
        self.assertEqual(shlex.split(commands[5])[:3], ["python", "manage.py", "test"])
        self.assertTrue({
            "hospital.tests.PostgreSQLAuditTriggerTests",
            "hospital.tests.InvoiceBalanceRaceTests",
            "hospital.tests.ExceptionRecurrenceRaceTests",
        }.issubset(shlex.split(commands[5])[3:]))
        self.assertIn("services:", self.workflow.read_text())

    def run_local_checks_with_probe(self, failing_command=""):
        """Record what checks.sh executes without running its expensive checks."""
        with TemporaryDirectory() as root:
            probe = Path(root) / "python-probe"
            log = Path(root) / "checks.jsonl"
            probe.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['KFB_CHECK_LOG'], 'a', encoding='utf-8') as log:\n"
                "    log.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
                "'env': {key: os.environ.get(key) for key in "
                "('KFB_ENV', 'KFB_SECRET_KEY', 'KFB_DATABASE_URL', 'KFB_ALLOWED_HOSTS')}}) + '\\n')\n"
                "if ' '.join(sys.argv[1:]) == os.environ['KFB_FAKE_FAILURE']:\n"
                "    sys.exit(7)\n"
            )
            probe.chmod(0o755)
            env = os.environ.copy()
            for key in ("KFB_ENV", "KFB_SECRET_KEY", "KFB_DATABASE_URL", "KFB_ALLOWED_HOSTS"):
                env.pop(key, None)
            env.update(PYTHON=str(probe), KFB_CHECK_LOG=str(log), KFB_FAKE_FAILURE=failing_command)
            result = subprocess.run(
                ["bash", str(self.script)], cwd=settings.BASE_DIR,
                env=env, capture_output=True, text=True, check=False,
            )
            records = [json.loads(line) for line in log.read_text().splitlines()]
        return result, records

    @skipUnless(os.name == "posix" and shutil.which("bash"), "checks.sh needs a POSIX shell")
    def test_local_script_executes_the_same_demo_checks_as_ci(self):
        demo_job = self.workflow.read_text().split("  test:\n", 1)[1].split("\n  postgres:", 1)[0]
        self.assertRegex(demo_job, r"(?m)^      KFB_ENV: demo$")
        commands = self.workflow_commands()[:4]
        result, records = self.run_local_checks_with_probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(records), len(commands))

        for index, (command, record) in enumerate(zip(commands, records)):
            with self.subTest(command=command):
                tokens = shlex.split(command)
                expected_env = {}
                while tokens and tokens[0].startswith("KFB_") and "=" in tokens[0]:
                    key, value = tokens.pop(0).split("=", 1)
                    expected_env[key] = value
                if tokens[0] == "ruff":
                    expected_argv = ["-m", *tokens]
                else:
                    self.assertEqual(tokens.pop(0), "python")
                    expected_argv = tokens
                self.assertEqual(record["argv"], expected_argv)
                self.assertEqual(Path(record["cwd"]), settings.BASE_DIR)
                if index:
                    self.assertEqual(record["env"]["KFB_ENV"], expected_env.get("KFB_ENV", "demo"))
                if index == 3:
                    self.assertEqual(record["env"], {
                        key: expected_env.get(key)
                        for key in ("KFB_ENV", "KFB_SECRET_KEY", "KFB_DATABASE_URL", "KFB_ALLOWED_HOSTS")
                    })

    @skipUnless(os.name == "posix" and shutil.which("bash"), "checks.sh needs a POSIX shell")
    def test_local_script_reports_a_failed_check(self):
        result, records = self.run_local_checks_with_probe("manage.py test")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(records), 4)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("tests", result.stdout)

    def test_the_check_script_is_executable(self):
        self.assertTrue(self.script.exists(), "scripts/checks.sh is missing.")
        self.assertTrue(os.access(self.script, os.X_OK), "scripts/checks.sh must be executable.")

@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class EnhancedWorkflowTests(HospitalFixtureMixin, TestCase):
    def test_dashboard_shows_patient_queue_only_to_chart_roles_and_routes_other_work(self):
        Encounter.objects.create(patient=self.patient, started_by=self.reception)
        lab = self.make_user("dashboard-lab", Role.LAB)
        eye = self.make_user("dashboard-eye", Role.EYE)
        for user, destination in (
            (self.pharmacist, "pharmacy_orders"),
            (self.procurement, "purchasing"),
            (lab, "departments"),
            (eye, "eye_clinic"),
            (self.reviewer, "reports"),
        ):
            with self.subTest(role=user.staff_profile.role):
                self.client.force_login(user)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("queue", response.context)
                self.assertNotContains(response, self.patient.full_name)
                self.assertContains(response, reverse(destination))
                self.assertNotContains(response, "Active patient queue")
        for user in (self.owner, self.reception, self.clinician, self.nurse):
            with self.subTest(role=user.staff_profile.role):
                self.client.force_login(user)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.patient.full_name)
                self.assertContains(response, reverse("patient_detail", args=[self.patient.pk]))

    def test_role_scoped_pages_render_without_template_errors(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        order = self.prepare(1)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=5,
            method="cash", reference="", idempotency_key="smoke-payment",
        )
        service = CatalogueItem.objects.create(
            code="SMOKE-LAB", name="Smoke lab", kind=CatalogueItem.Kind.SERVICE,
            department="Laboratory", base_unit="service", sale_unit="service",
            units_per_sale_unit=1,
        )
        service_order = ServiceOrder.objects.create(
            encounter=encounter, service=service, requested_by=self.clinician,
        )

        role_pages = [
            (self.owner, [
                reverse("dashboard"), reverse("reports"), reverse("exceptions"),
                reverse("audit_review"), reverse("settings"), reverse("csv_import"),
                reverse("patients"), reverse("patient_detail", kwargs={"pk": self.patient.pk}),
                reverse("purchasing"), reverse("downtime_forms"),
            ]),
            (self.reception, [
                reverse("patient_create"), reverse("encounter_create", kwargs={"patient_id": self.patient.pk}),
                reverse("pharmacy_orders"), reverse("shift_manage"),
                reverse("invoice_payment", kwargs={"pk": order.invoice_id}),
                reverse("receipt", kwargs={"pk": payment.pk}),
            ]),
            (self.pharmacist, [
                reverse("pharmacy_orders"), reverse("pharmacy_order_create"),
                reverse("pharmacy_order_detail", kwargs={"pk": order.pk}), reverse("stock"),
            ]),
            (self.clinician, [
                reverse("queue"), reverse("clinical_note", kwargs={"encounter_id": encounter.pk}),
                reverse("prescription_create", kwargs={"encounter_id": encounter.pk}),
                reverse("service_order_create", kwargs={"encounter_id": encounter.pk}),
                reverse("service_order_update", kwargs={"pk": service_order.pk}),
                reverse("departments"), reverse("wards"),
                reverse("admission_create", kwargs={"encounter_id": encounter.pk}),
            ]),
            (self.procurement, [reverse("purchasing"), reverse("purchase_order_create"), reverse("csv_import")]),
        ]
        eye_staff = self.make_user("eye-smoke", Role.EYE)
        role_pages.append((eye_staff, [reverse("eye_clinic")]))

        for user, urls in role_pages:
            self.client.force_login(user)
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)

        self.client.logout()
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

    def test_owner_report_download_is_a_named_pdf(self):
        self.client.login(username=self.owner.username, password=self.password)
        response = self.client.get(reverse("report_download_pdf"), {"days": "30"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("KFBH-owner-report-30-days.pdf", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertGreater(len(response.content), 3000)

    def test_patient_access_record_is_a_watermarked_pdf_flow(self):
        self.client.login(username=self.owner.username, password=self.password)
        response = self.client.get(reverse("patient_access_pdf", kwargs={"pk": self.patient.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(self.patient.patient_number, response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_reception_cannot_export_clinical_notes_through_patient_pdf(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        ClinicalNote.objects.create(
            encounter=encounter, author=self.clinician, status=ClinicalNote.Status.SIGNED,
            assessment="Sensitive assessment", plan="Sensitive plan", signed_at=timezone.now(),
        )
        url = reverse("patient_access_pdf", kwargs={"pk": self.patient.pk})
        self.client.login(username=self.reception.username, password=self.password)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotContains(
            self.client.get(reverse("patient_detail", kwargs={"pk": self.patient.pk})),
            "Access-record PDF",
        )
        self.client.logout()
        self.client.login(username=self.clinician.username, password=self.password)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_reviewer_can_verify_mpesa_from_reports(self):
        order = self.prepare(1)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=5,
            method="mpesa", reference="VERIFY-001", idempotency_key="verify-ui",
        )
        self.client.login(username=self.reviewer.username, password=self.password)
        self.assertNotContains(self.client.get(reverse("reports")), 'name="provider_confirmed"')
        url = reverse("payment_verify", kwargs={"pk": payment.pk})
        self.client.post(url, {"decision": "verify", "provider_confirmed": "1"})
        payment.refresh_from_db()
        self.assertEqual(payment.verification_status, Payment.Verification.UNVERIFIED)
        response = self.client.post(url, {
            "decision": "verify", "provider_confirmed": "1",
            "review_notes": "Matched to the hospital M-PESA statement.",
        })
        self.assertRedirects(response, reverse("reports"))
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.verification_status, Payment.Verification.MANUAL)
        self.assertEqual(payment.review_notes, "Matched to the hospital M-PESA statement.")
        self.assertEqual(order.status, PharmacyOrder.Status.CLEARED)

    def test_reviewer_can_reject_invalid_mpesa_from_reports(self):
        order = self.prepare(1)
        payment = record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=5,
            method="mpesa", reference="INVALID-UI", idempotency_key="invalid-ui",
        )
        self.client.login(username=self.reviewer.username, password=self.password)
        response = self.client.post(
            reverse("payment_verify", kwargs={"pk": payment.pk}),
            {"decision": "reject", "review_notes": "No matching provider transaction"},
        )
        self.assertRedirects(response, reverse("reports"))
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REJECTED)
        self.assertEqual(order.status, PharmacyOrder.Status.PREPARED)
        self.assertEqual(order.invoice.balance, Decimal("5"))

    def test_credit_note_request_and_review_have_complete_ui_flow(self):
        order = self.prepare(2)
        self.client.login(username=self.reception.username, password=self.password)
        response = self.client.post(
            reverse("credit_note_create", kwargs={"pk": order.invoice_id}),
            {"amount": "5.00", "reason": "Duplicate charge correction"},
        )
        self.assertEqual(response.status_code, 302)
        note = CreditNote.objects.get(invoice=order.invoice)
        self.client.logout()
        self.client.login(username=self.reviewer.username, password=self.password)
        self.client.post(
            reverse("credit_note_review", kwargs={"pk": note.pk}),
            {"decision": "approve"},
        )
        note.refresh_from_db()
        self.assertEqual(note.status, CreditNote.Status.APPROVED)

    def test_requester_cannot_release_own_result(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        service = CatalogueItem.objects.create(
            code="XRAY-T", name="X-ray test", kind="service", department="Imaging",
            base_unit="service", sale_unit="service", units_per_sale_unit=1,
        )
        order = ServiceOrder.objects.create(
            encounter=encounter, service=service, requested_by=self.clinician,
        )
        performer = self.make_user("xray-performer", Role.LAB)
        update_service_order(actor=performer, order_id=order.pk, status=ServiceOrder.Status.IN_PROGRESS, result="")
        update_service_order(actor=performer, order_id=order.pk, status=ServiceOrder.Status.REVIEW, result="Demonstration result")
        self.client.login(username=self.clinician.username, password=self.password)
        response = self.client.post(
            reverse("service_order_update", kwargs={"pk": order.pk}),
            {"status": "released", "result": "Demonstration result"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot release their own result")
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.Status.REVIEW)

    def test_multi_item_prescription_creates_one_signed_order(self):
        second = CatalogueItem.objects.create(
            code="TEST-CAP", name="Test capsule", kind=CatalogueItem.Kind.PRODUCT,
            department="Pharmacy", base_unit="capsule", sale_unit="box",
            units_per_sale_unit=10, reorder_level=2,
        )
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        self.client.login(username=self.clinician.username, password=self.password)
        payload = {
            "items-TOTAL_FORMS": "2", "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
        }
        for index, product in enumerate((self.product, second)):
            payload.update({
                f"items-{index}-product": str(product.pk),
                f"items-{index}-strength": "As labelled",
                f"items-{index}-dose": "One",
                f"items-{index}-route": "Oral",
                f"items-{index}-frequency": "Daily",
                f"items-{index}-duration": "Five days",
                f"items-{index}-quantity_base_units": "5",
                f"items-{index}-instructions": "Demonstration only",
            })
        response = self.client.post(reverse("prescription_create", kwargs={"encounter_id": encounter.pk}), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PrescriptionItem.objects.filter(prescription__encounter=encounter).count(), 2)

    def test_multi_line_purchase_request(self):
        supplier = Supplier.objects.create(name="Workflow Supplier")
        self.client.login(username=self.procurement.username, password=self.password)
        payload = {
            "supplier": supplier.pk, "reference": "QUOTE-1", "notes": "Monthly order",
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "1", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk, "lines-0-quantity_base_units": "20",
            "lines-0-quoted_unit_cost": "2.50",
            "lines-1-product": self.product.pk, "lines-1-quantity_base_units": "30",
            "lines-1-quoted_unit_cost": "2.25",
        }
        response = self.client.post(reverse("purchase_order_create"), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PurchaseOrderLine.objects.filter(order__reference="QUOTE-1").count(), 2)

    def test_attachment_download_requires_clinical_role(self):
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.client.login(username=self.clinician.username, password=self.password)
            upload = SimpleUploadedFile("consent.pdf", b"%PDF-1.4 demonstration", content_type="application/pdf")
            response = self.client.post(
                reverse("patient_attachment_upload", kwargs={"pk": self.patient.pk}),
                {"file": upload, "description": "Signed consent"},
            )
            self.assertEqual(response.status_code, 302)
            attachment = ClinicalAttachment.objects.get(patient=self.patient)
            download = self.client.get(reverse("patient_attachment_download", kwargs={"pk": attachment.pk}))
            self.assertEqual(download.status_code, 200)
            download.close()
            self.client.logout()
            self.client.login(username=self.reception.username, password=self.password)
            self.assertEqual(
                self.client.get(reverse("patient_attachment_download", kwargs={"pk": attachment.pk})).status_code,
                403,
            )

    def test_patient_csv_import_commits_external_reference_once(self):
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"external_reference,first_name,last_name,date_of_birth,estimated_age_years,sex,phone,guardian_name,guardian_phone\n"
            b"LEGACY-44,Mary,Wafula,1995-03-02,,F,0700000011,,\n"
        )
        response = self.client.post(reverse("csv_import"), {
            "import_kind": "patients",
            "csv_file": SimpleUploadedFile("patients.csv", csv_bytes, content_type="text/csv"),
        })
        self.assertEqual(response.status_code, 200)
        job = ImportJob.objects.get(filename="patients.csv")
        self.assertEqual(job.error_count, 0)
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        self.assertEqual(Patient.objects.filter(external_reference="LEGACY-44").count(), 1)

    def test_stale_patient_import_does_not_duplicate_external_reference(self):
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"external_reference,first_name,last_name,date_of_birth,estimated_age_years,sex,phone,guardian_name,guardian_phone\n"
            b"STALE-44,Mary,Wafula,1995-03-02,,F,0700000011,,\n"
        )
        self.client.post(reverse("csv_import"), {
            "import_kind": "patients",
            "csv_file": SimpleUploadedFile("stale-patients.csv", csv_bytes, content_type="text/csv"),
        })
        job = ImportJob.objects.get(filename="stale-patients.csv")
        Patient.objects.create(
            external_reference="STALE-44", first_name="Existing", last_name="Patient",
            estimated_age_years=30, registered_by=self.reception,
        )
        response = self.client.post(reverse("csv_import"), {"commit_job": job.pk}, follow=True)
        self.assertContains(response, "dry run is out of date")
        self.assertEqual(Patient.objects.filter(external_reference="STALE-44").count(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "validated")

    def test_same_file_gets_independent_fresh_dry_runs(self):
        second_owner = self.make_user("owner-two", Role.OWNER)
        csv_bytes = (
            b"external_reference,first_name,last_name,date_of_birth,estimated_age_years,sex,phone,guardian_name,guardian_phone\n"
            b"SHARED-44,Mary,Wafula,1995-03-02,,F,0700000011,,\n"
        )
        for owner in (self.owner, second_owner):
            self.client.force_login(owner)
            self.client.post(reverse("csv_import"), {
                "import_kind": "patients",
                "csv_file": SimpleUploadedFile("shared-patients.csv", csv_bytes, content_type="text/csv"),
            })
        jobs = list(ImportJob.objects.filter(filename="shared-patients.csv"))
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job.created_by_id for job in jobs}, {self.owner.id, second_owner.id})
        self.assertNotEqual(jobs[0].idempotency_key, jobs[1].idempotency_key)

    def test_stale_receivable_import_does_not_duplicate_invoice(self):
        self.patient.external_reference = "LEGACY-PATIENT"
        self.patient.save(update_fields=["external_reference", "updated_at"])
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"external_patient_reference,external_invoice_reference,original_invoice_date,description,department,outstanding_amount,review_reference\n"
            b"LEGACY-PATIENT,STALE-INV-1,2026-01-01,Opening balance,Outpatient,1000.00,REVIEW-1\n"
        )
        self.client.post(reverse("csv_import"), {
            "import_kind": "opening_receivables",
            "csv_file": SimpleUploadedFile("stale-receivables.csv", csv_bytes, content_type="text/csv"),
        })
        job = ImportJob.objects.get(filename="stale-receivables.csv")
        Invoice.objects.create(
            external_reference="STALE-INV-1", patient=self.patient, created_by=self.owner,
        )
        response = self.client.post(reverse("csv_import"), {"commit_job": job.pk}, follow=True)
        self.assertContains(response, "dry run is out of date")
        self.assertEqual(Invoice.objects.filter(external_reference="STALE-INV-1").count(), 1)

    def test_opening_stock_import_creates_witnessed_ledger_entry(self):
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"product_code,batch_number,expiry_date,purchase_cost_per_base_unit,physical_count_base_units,witness_name,count_reference\n"
            b"TEST-TAB,IMPORT-BATCH-01,2027-12-31,2.25,75,Independent Witness,COUNT-IMPORT-01\n"
        )
        response = self.client.post(reverse("csv_import"), {
            "import_kind": "opening_stock",
            "csv_file": SimpleUploadedFile("opening-stock.csv", csv_bytes, content_type="text/csv"),
        })
        self.assertEqual(response.status_code, 200)
        job = ImportJob.objects.get(filename="opening-stock.csv")
        self.assertEqual(job.error_count, 0)
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        batch = StockBatch.objects.get(item=self.product, batch_number="IMPORT-BATCH-01")
        self.assertEqual(batch.quantity_on_hand, Decimal("75"))
        movement = batch.movements.get()
        self.assertEqual(movement.reference_id, "COUNT-IMPORT-01")
        self.assertIn("Independent Witness", movement.reason)

    def test_opening_receivable_import_posts_reviewed_patient_balance(self):
        self.patient.external_reference = "LEGACY-PATIENT-1"
        self.patient.save(update_fields=["external_reference"])
        self.client.login(username=self.owner.username, password=self.password)
        csv_bytes = (
            b"external_patient_reference,external_invoice_reference,original_invoice_date,description,department,outstanding_amount,review_reference\n"
            b"LEGACY-PATIENT-1,LEGACY-INV-1,2026-01-15,Opening clinic balance,Outpatient,1250.00,OWNER-REVIEW-1\n"
        )
        response = self.client.post(reverse("csv_import"), {
            "import_kind": "opening_receivables",
            "csv_file": SimpleUploadedFile("receivables.csv", csv_bytes, content_type="text/csv"),
        })
        self.assertEqual(response.status_code, 200)
        job = ImportJob.objects.get(filename="receivables.csv")
        self.assertEqual(job.error_count, 0)
        self.client.post(reverse("csv_import"), {"commit_job": job.pk})
        invoice = Invoice.objects.get(external_reference="LEGACY-INV-1")
        self.assertEqual(invoice.patient, self.patient)
        self.assertEqual(invoice.original_invoice_date.isoformat(), "2026-01-15")
        self.assertEqual(invoice.balance, Decimal("1250.00"))
        self.assertIn("OWNER-REVIEW-1", invoice.lines.get().description)

    def test_every_csv_format_rejects_more_than_5000_rows(self):
        self.client.force_login(self.owner)
        formats = {
            "products": (
                "code,name,department,base_unit,sale_unit,units_per_sale_unit,sale_price,reorder_level,prescription_required",
                "CAP-1,Product,Pharmacy,tablet,box,100,7.50,20,false",
            ),
            "patients": (
                "external_reference,first_name,last_name,date_of_birth,estimated_age_years,sex,phone,guardian_name,guardian_phone",
                "CAP-1,Mary,Wafula,1995-03-02,,F,0700000011,,",
            ),
            "opening_stock": (
                "product_code,batch_number,expiry_date,purchase_cost_per_base_unit,physical_count_base_units,witness_name,count_reference",
                "TEST-TAB,CAP-1,2027-12-31,2.25,75,Witness,COUNT-1",
            ),
            "opening_receivables": (
                "external_patient_reference,external_invoice_reference,original_invoice_date,description,department,outstanding_amount,review_reference",
                "CAP-1,CAP-INV-1,2026-01-01,Opening,Outpatient,1000.00,REVIEW-1",
            ),
        }
        for kind, (header, row) in formats.items():
            with self.subTest(kind=kind):
                payload = (header + "\n" + (row + "\n") * 5001).encode()
                response = self.client.post(reverse("csv_import"), {
                    "import_kind": kind,
                    "csv_file": SimpleUploadedFile(f"{kind}.csv", payload, content_type="text/csv"),
                })
                self.assertContains(response, "Maximum 5,000 rows per import")
                self.assertFalse(ImportJob.objects.filter(filename=f"{kind}.csv").exists())

    def test_csv_reference_validation_queries_stay_flat(self):
        from .views import (
            _validate_opening_receivables_csv,
            _validate_opening_stock_csv,
            _validate_patient_csv,
            _validate_product_csv,
        )

        self.patient.external_reference = "CAP-PATIENT"
        self.patient.save(update_fields=["external_reference", "updated_at"])
        cases = [
            (_validate_product_csv,
             "code,name,department,base_unit,sale_unit,units_per_sale_unit,sale_price,reorder_level,prescription_required",
             lambda i: f"CAP-PRODUCT-{i},Product,Pharmacy,tablet,box,100,7.50,20,false"),
            (_validate_patient_csv,
             "external_reference,first_name,last_name,date_of_birth,estimated_age_years,sex,phone,guardian_name,guardian_phone",
             lambda i: f"CAP-PATIENT-{i},Mary,Wafula,1995-03-02,,F,0700000011,,"),
            (_validate_opening_stock_csv,
             "product_code,batch_number,expiry_date,purchase_cost_per_base_unit,physical_count_base_units,witness_name,count_reference",
             lambda i: f"TEST-TAB,CAP-BATCH-{i},2027-12-31,2.25,75,Witness,COUNT-{i}"),
            (_validate_opening_receivables_csv,
             "external_patient_reference,external_invoice_reference,original_invoice_date,description,department,outstanding_amount,review_reference",
             lambda i: f"CAP-PATIENT,CAP-INV-{i},2026-01-01,Opening,Outpatient,1000.00,REVIEW-{i}"),
        ]
        for validator, header, make_row in cases:
            with self.subTest(validator=validator.__name__):
                row_count = 5000 if validator is _validate_product_csv else 100
                payload = (header + "\n" + "\n".join(make_row(i) for i in range(row_count)) + "\n").encode()
                with CaptureQueriesContext(connection) as queries:
                    _, rows, errors = validator(SimpleUploadedFile("batch.csv", payload))
                self.assertEqual((len(rows), errors), (row_count, []))
                self.assertLessEqual(len(queries), 10 if row_count == 5000 else 2)

    def test_expired_csv_dry_run_requires_fresh_upload(self):
        self.client.force_login(self.owner)
        csv_bytes = (
            b"external_reference,first_name,last_name,date_of_birth,estimated_age_years,sex,phone,guardian_name,guardian_phone\n"
            b"EXPIRED-44,Mary,Wafula,1995-03-02,,F,0700000011,,\n"
        )
        self.client.post(reverse("csv_import"), {
            "import_kind": "patients",
            "csv_file": SimpleUploadedFile("expired.csv", csv_bytes, content_type="text/csv"),
        })
        job = ImportJob.objects.get(filename="expired.csv")
        ImportJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=25))
        response = self.client.post(reverse("csv_import"), {"commit_job": job.pk}, follow=True)
        self.assertContains(response, "dry run expired after 24 hours")
        self.assertContains(response, "Expired; upload again")
        self.assertFalse(Patient.objects.filter(external_reference="EXPIRED-44").exists())

    def test_failed_csv_preview_keeps_only_first_50_errors(self):
        self.client.force_login(self.owner)
        row = "CAP-DUP,Product,Pharmacy,tablet,box,100,7.50,20,false\n"
        payload = (
            "code,name,department,base_unit,sale_unit,units_per_sale_unit,sale_price,reorder_level,prescription_required\n"
            + row * 56
        ).encode()
        self.client.post(reverse("csv_import"), {
            "import_kind": "products",
            "csv_file": SimpleUploadedFile("many-errors.csv", payload, content_type="text/csv"),
        })
        job = ImportJob.objects.get(filename="many-errors.csv")
        self.assertEqual(job.error_count, 55)
        self.assertEqual(len(job.report["errors"]), 50)
        self.assertEqual(job.report["rows"], [])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RoleCapabilityNavigationTests(HospitalFixtureMixin, TestCase):
    def roles(self):
        return {
            Role.OWNER: self.owner,
            Role.RECEPTION: self.reception,
            Role.CLINICIAN: self.clinician,
            Role.NURSE: self.nurse,
            Role.PHARMACY: self.pharmacist,
            Role.PROCUREMENT: self.procurement,
            Role.REVIEWER: self.reviewer,
            Role.LAB: self.make_user("role-lab", Role.LAB),
            Role.EYE: self.make_user("role-eye", Role.EYE),
        }

    def test_patient_tabs_exist_only_when_the_section_is_rendered_for_the_role(self):
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        ClinicalNote.objects.create(encounter=encounter, author=self.clinician, assessment="Review")
        Invoice.objects.create(patient=self.patient, created_by=self.reception)
        for role, user in self.roles().items():
            if role not in {Role.OWNER, Role.RECEPTION, Role.CLINICIAN, Role.NURSE, Role.EYE}:
                continue
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.get(reverse("patient_detail", args=[self.patient.pk]))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                tabs = set(re.findall(r'<a href="#(visits|notes|attachments|billing)"', html))
                sections = set(re.findall(r'<section[^>]* id="(visits|notes|attachments|billing)"', html))
                self.assertEqual(tabs, sections)
                expected = {"visits"}
                if role in {Role.OWNER, Role.CLINICIAN, Role.NURSE}:
                    expected |= {"notes", "attachments"}
                if role in {Role.OWNER, Role.RECEPTION}:
                    expected.add("billing")
                self.assertEqual(tabs, expected)
                visit_url = reverse("encounter_create", args=[self.patient.pk])
                self.assertEqual(f'href="{visit_url}"' in html, role in {Role.RECEPTION, Role.CLINICIAN})

        self.client.force_login(self.clinician)
        ClinicalNote.objects.all().delete()
        response = self.client.get(reverse("patient_detail", args=[self.patient.pk]))
        self.assertNotContains(response, 'href="#notes"')

    def test_shortcuts_and_pharmacy_navigation_match_each_role(self):
        expected = {
            Role.OWNER: set("dpqsbr"),
            Role.RECEPTION: set("dpq"),
            Role.CLINICIAN: set("dpq"),
            Role.NURSE: set("dp"),
            Role.PHARMACY: set("ds"),
            Role.PROCUREMENT: set("ds"),
            Role.REVIEWER: set("dsbr"),
            Role.LAB: set("dq"),
            Role.EYE: set("dp"),
        }
        for role, user in self.roles().items():
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                shortcuts = dict(re.findall(r'data-shortcut-([a-z])="([^"]+)"', html))
                self.assertEqual(set(shortcuts), expected[role])
                for url in shortcuts.values():
                    self.assertEqual(self.client.get(url).status_code, 200, f"{role} shortcut to {url}")
                sidebar = html.split('<nav>', 1)[1].split('</nav>', 1)[0]
                self.assertLessEqual(sidebar.count(f'href="{reverse("pharmacy_orders")}"'), 1)

    def test_owner_and_reviewer_brief_actions_are_reachable_and_accurate(self):
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.save(update_fields=["expiry_date", "updated_at"])
        for role, user in ((Role.OWNER, self.owner), (Role.REVIEWER, self.reviewer)):
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.get(reverse("owner_brief"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Review expired stock")
                self.assertContains(response, "Review count sheets")
                self.assertNotContains(response, ">Start a count</a>")
                self.assertEqual(self.client.get(reverse("write_offs")).status_code, 200)
                self.assertEqual(self.client.get(reverse("stock_counts")).status_code, 200)
                stock = self.client.get(reverse("stock"))
                self.assertNotContains(stock, "Request a write-off")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DowntimePaperFormTests(HospitalFixtureMixin, TestCase):
    def test_sensitive_forms_warn_on_unsaved_or_returned_invalid_input(self):
        invoice = Invoice.objects.create(
            patient=self.patient, status=Invoice.Status.POSTED,
            posted_at=timezone.now(), created_by=self.reception,
        )
        InvoiceLine.objects.create(
            invoice=invoice, item=self.product, description="Medicine",
            quantity=1, unit_price=Decimal("100.00"), department="Pharmacy",
        )
        encounter = Encounter.objects.create(patient=self.patient, started_by=self.reception)
        cases = (
            (self.reception, reverse("invoice_payment", args=[invoice.pk])),
            (self.reception, reverse("shift_manage")),
            (self.clinician, reverse("service_order_create", args=[encounter.pk])),
            (self.clinician, reverse("admission_create", args=[encounter.pk])),
        )
        for user, url in cases:
            with self.subTest(url=url):
                self.client.force_login(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "data-unsaved-warning")
        self.client.force_login(self.reception)
        invalid = self.client.post(reverse("invoice_payment", args=[invoice.pk]), {
            "amount": "-1", "method": Payment.Method.CASH, "reference": "",
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertTrue(invalid.context["form"].is_bound)
        self.assertContains(invalid, "data-unsaved-warning")
        self.assertContains(invalid, "field-error")

    def test_printable_forms_explain_manual_references_and_reconciliation(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("downtime_forms"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "These sheets have no automatic numbering")
        self.assertContains(response, "checks for an existing patient, payment or stock entry")
        self.assertEqual(response.content.decode().count("Manual register reference:"), 4)

    def test_duplicate_paper_references_cannot_be_saved_twice(self):
        details = {
            "paper_reference": "MANUAL-001", "event_type": "payment",
            "event_at": timezone.now(), "entered_by": self.owner,
        }
        DowntimeEntry.objects.create(**details)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                DowntimeEntry.objects.create(**details)
        self.assertEqual(DowntimeEntry.objects.filter(paper_reference="MANUAL-001").count(), 1)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OwnerVisibilityTests(HospitalFixtureMixin, TestCase):
    """The owner can open every screen, which is not the same as doing everything.

    Page access was widened to the owner on request. The segregation of duties
    that actually protects the ledger lives in the service layer, where a person
    still cannot perform another role's action or approve their own request, and
    those rules are deliberately untouched by the wider navigation.
    """

    views_directory = Path(settings.BASE_DIR) / "hospital"

    def role_protected_views(self):
        pattern = re.compile(r"@role_required\(([^)]*)\)\s*\ndef (\w+)\(", re.S)
        protected = {}
        for path in self.views_directory.glob("*views.py"):
            protected.update({fn: roles for roles, fn in pattern.findall(path.read_text())})
        return protected

    def test_no_role_protected_view_excludes_the_owner(self):
        missing = [fn for fn, roles in self.role_protected_views().items() if "Role.OWNER" not in roles]
        self.assertEqual(missing, [], f"These views would shut the owner out: {missing}")

    def test_owner_navigation_covers_every_section_other_roles_have(self):
        owner = set(ROLE_NAVIGATION[Role.OWNER])
        for role, entries in ROLE_NAVIGATION.items():
            self.assertTrue(
                set(entries) <= owner,
                f"{role} can navigate to {set(entries) - owner}, which the owner cannot reach.",
            )

    def test_owner_can_open_every_page_that_needs_no_record(self):
        self.client.login(username=self.owner.username, password=self.password)
        simple = [
            "dashboard", "owner_brief", "stock_intelligence", "patients", "queue",
            "pharmacy_orders", "wards", "departments", "eye_clinic", "reports",
            "stock", "deliveries", "stock_counts", "custody", "write_offs",
            "purchasing", "exceptions", "audit_review", "shift_manage", "settings",
            "csv_import", "downtime_forms", "health",
        ]
        for name in simple:
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_owner_still_cannot_perform_another_role_s_action(self):
        # Seeing the pharmacy's screen is not being the pharmacy.
        with self.assertRaisesMessage(ValidationError, "Only pharmacy staff"):
            issue_to_department(
                actor=self.owner, department="Theatre", received_by_name="Someone",
                lines=[{"batch": self.batch, "quantity": Decimal("1")}],
            )
        with self.assertRaisesMessage(ValidationError, "Only pharmacy or procurement staff"):
            request_write_off(
                actor=self.owner, batch_id=self.batch.pk, quantity=Decimal("1"),
                reason=StockWriteOff.Reason.DAMAGED, narrative="Because I can see the page.",
            )
        with self.assertRaisesMessage(ValidationError, "Only reception/cashier"):
            record_payment(
                actor=self.owner, invoice_id=self.prepare(1).invoice_id, amount=5,
                method=Payment.Method.CASH, reference="", idempotency_key="owner-pay",
            )

    def test_owner_still_cannot_approve_their_own_request(self):
        supplier = Supplier.objects.create(name="Owner Supplies")
        order = PurchaseOrder.objects.create(supplier=supplier, requested_by=self.owner)
        with self.assertRaisesMessage(ValidationError, "cannot approve their own"):
            approve_purchase_order(actor=self.owner, order_id=order.pk)

    def test_owner_still_cannot_check_a_delivery_they_received(self):
        # The receiver check is on the person, not the role, so it holds for the
        # owner exactly as it does for anyone else.
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        supplier = Supplier.objects.create(name="Owner Delivery Supplies")
        order = PurchaseOrder.objects.create(supplier=supplier, requested_by=self.procurement)
        line = PurchaseOrderLine.objects.create(
            order=order, item=self.product, quantity_base_units=Decimal("10"), quoted_unit_cost=Decimal("2.00")
        )
        approve_purchase_order(actor=self.reviewer, order_id=order.pk)
        with override_settings(MEDIA_ROOT=media_root):
            receipt = receive_delivery(
                actor=self.procurement, purchase_order_id=order.pk,
                supplier_invoice_reference="OWN-1", invoice_amount=Decimal("20.00"),
                invoice_date=timezone.localdate(),
                invoice_photo=SimpleUploadedFile("i.jpg", JPEG_BYTES, content_type="image/jpeg"),
                lines=[{"order_line": line, "quantity_received": Decimal("10"), "batch_number": "OWN-B",
                        "expiry_date": timezone.localdate() + timedelta(days=300),
                        "actual_unit_cost": Decimal("2.00")}],
            )
        # The owner did not receive it, so they may check it.
        check_delivery(actor=self.owner, receipt_id=receipt.pk, discrepancy_notes="Counted.")
        receipt.refresh_from_db()
        self.assertEqual(receipt.checked_by, self.owner)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProductionHardeningTests(HospitalFixtureMixin, TestCase):
    """One test per defect found in the production audit.

    Each of these reproduced a real failure before the fix: stock going
    negative, a page 500-ing, a figure the owner acts on being silently absent,
    or a production deployment quietly serving sessions in the clear.
    """

    # ---- stock can never go negative ---------------------------------------

    def test_one_issue_cannot_claim_the_same_batch_twice(self):
        """Two lines naming one batch each used to read the untouched balance."""
        on_hand = self.batch.quantity_on_hand
        with self.assertRaisesMessage(ValidationError, "already claimed by another line"):
            issue_to_department(
                actor=self.pharmacist,
                department="Maternity ward",
                received_by_name="Sister Faith",
                lines=[
                    {"batch": self.batch, "quantity": on_hand},
                    {"batch": self.batch, "quantity": Decimal("1.000")},
                ],
            )
        self.assertEqual(self.batch.quantity_on_hand, on_hand)
        self.assertFalse(DepartmentIssue.objects.exists())

    def test_two_lines_on_one_batch_are_allowed_while_the_total_fits(self):
        on_hand = self.batch.quantity_on_hand
        issue = issue_to_department(
            actor=self.pharmacist,
            department="Maternity ward",
            received_by_name="Sister Faith",
            lines=[
                {"batch": self.batch, "quantity": Decimal("10.000")},
                {"batch": self.batch, "quantity": Decimal("15.000")},
            ],
        )
        self.assertEqual(issue.lines.count(), 2)
        self.assertEqual(self.batch.quantity_on_hand, on_hand - Decimal("25.000"))

    def test_write_off_is_rechecked_against_stock_at_approval(self):
        """Stock keeps moving between proposing a write-off and approving it."""
        write_off = request_write_off(
            actor=self.pharmacist, batch_id=self.batch.pk,
            quantity=self.batch.quantity_on_hand,
            reason=StockWriteOff.Reason.DAMAGED, narrative="Shelf collapsed.",
        )
        issue_to_department(
            actor=self.pharmacist, department="Theatre", received_by_name="Nurse B",
            lines=[{"batch": self.batch, "quantity": self.batch.quantity_on_hand}],
        )
        with self.assertRaisesMessage(ValidationError, "The stock has moved since this was raised"):
            review_write_off(actor=self.reviewer, write_off_id=write_off.pk, approve=True)
        self.assertGreaterEqual(self.batch.quantity_on_hand, Decimal("0.000"))
        write_off.refresh_from_db()
        self.assertEqual(write_off.status, StockWriteOff.Status.PENDING)

    # ---- FEFO means one thing on every database ----------------------------

    def test_fefo_dispenses_the_dated_batch_before_the_undated_one(self):
        """NULLs sort first on SQLite and last on PostgreSQL; FEFO must not.

        Left to the database this picks a different batch off the shelf in the
        demo than it does in production.
        """
        undated = StockBatch.objects.create(
            item=self.product, batch_number="NO-EXPIRY", expiry_date=None,
            purchase_cost_per_base_unit=Decimal("2.00"),
        )
        StockMovement.objects.create(
            batch=undated, movement_type=StockMovement.MovementType.RECEIPT,
            quantity_delta=Decimal("100.000"), to_location="Pharmacy",
            reference_type="Test", reference_id="undated",
            idempotency_key="fefo-undated", entered_by=self.pharmacist,
        )
        order = prepare_pharmacy_order(
            actor=self.pharmacist, customer_name="Walk-in", patient=None,
            items=[(self.product, Decimal("5.000"))],
        )
        record_payment(
            actor=self.reception, invoice_id=order.invoice_id, amount=order.invoice.total,
            method=Payment.Method.CASH, reference="", idempotency_key="fefo-pay",
        )
        dispense_order(actor=self.pharmacist, order_id=order.pk, idempotency_key="fefo-dispense")

        dispensed = StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.DISPENSE, reference_id=str(order.pk)
        )
        self.assertEqual(
            {m.batch_id for m in dispensed}, {self.batch.pk},
            "FEFO must take the batch with the earliest expiry, not the one with none recorded.",
        )

    # ---- raising an exception is idempotent, and recurrence is visible -----

    def test_raising_the_same_exception_twice_updates_one_record(self):
        from .services import raise_exception
        first = raise_exception("test_category", "The same thing happened", "first time")
        second = raise_exception("test_category", "The same thing happened", "second time")
        self.assertEqual(first.pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.occurrence_count, 2)
        self.assertEqual(ExceptionRecord.objects.filter(category="test_category").count(), 1)

    def test_a_resolved_exception_reopens_when_the_problem_recurs(self):
        from .services import raise_exception
        record = raise_exception("test_category", "Recurring problem", "first")
        record.status = ExceptionRecord.Status.RESOLVED
        record.resolved_at = timezone.now()
        record.save(update_fields=["status", "resolved_at"])
        raise_exception("test_category", "Recurring problem", "it came back")
        record.refresh_from_db()
        self.assertEqual(record.status, ExceptionRecord.Status.OPEN)
        self.assertIsNone(record.resolved_at)

    def test_duplicate_legacy_summaries_no_longer_break_raising(self):
        """Two rows sharing a summary used to make every later raise a 500."""
        from .services import raise_exception
        ExceptionRecord.objects.create(category="legacy", summary="Same summary", evidence="a")
        ExceptionRecord.objects.create(category="legacy", summary="Same summary", evidence="b")
        record = raise_exception("legacy", "Same summary", "raised again")
        self.assertIsNotNone(record.pk)

    # ---- opening a count is a bounded amount of work -----------------------

    def test_opening_a_count_does_not_query_once_per_batch(self):
        for index in range(30):
            item = CatalogueItem.objects.create(
                code=f"BULK-{index}", name=f"Bulk product {index}",
                kind=CatalogueItem.Kind.PRODUCT, department="Pharmacy",
                base_unit="tablet", sale_unit="tablet",
                units_per_sale_unit=Decimal("1.000"), reorder_level=Decimal("5.000"),
            )
            StockBatch.objects.create(
                item=item, batch_number=f"BULK-B{index}",
                purchase_cost_per_base_unit=Decimal("1.00"),
            )
        with self.assertNumQueries(FunctionalQueryBudget(16)):
            count = open_stock_count(actor=self.pharmacist, location="Pharmacy")
        self.assertGreater(count.lines.count(), 0)

    def test_a_count_sheet_skips_batches_that_are_empty_and_retired(self):
        retired = StockBatch.objects.create(
            item=self.product, batch_number="RETIRED",
            status=StockBatch.Status.EXPIRED,
            expiry_date=timezone.localdate() - timedelta(days=5),
            purchase_cost_per_base_unit=Decimal("2.00"),
        )
        count = open_stock_count(actor=self.pharmacist, location="Pharmacy")
        counted = set(count.lines.values_list("batch_id", flat=True))
        self.assertIn(self.batch.pk, counted)
        self.assertNotIn(retired.pk, counted, "An empty, expired batch is not on the shelf to be counted.")

    # ---- the reorder list surfaces what it exists to surface ---------------

    def test_a_product_never_stocked_still_appears_on_the_reorder_list(self):
        """The item most urgently needing reorder was the one it could not show."""
        never_stocked = CatalogueItem.objects.create(
            code="NEVER-1", name="Never stocked product",
            kind=CatalogueItem.Kind.PRODUCT, department="Pharmacy",
            base_unit="vial", sale_unit="vial",
            units_per_sale_unit=Decimal("1.000"), reorder_level=Decimal("50.000"),
        )
        position = stock_position()
        self.assertIn(never_stocked.pk, {row["item"].pk for row in position["below_reorder"]})
        self.assertIn(never_stocked.pk, {row["item"].pk for row in position["out_of_stock"]})

    def test_a_product_with_no_reorder_level_is_not_reported_as_short(self):
        CatalogueItem.objects.create(
            code="UNMANAGED-1", name="Unmanaged product",
            kind=CatalogueItem.Kind.PRODUCT, department="Pharmacy",
            base_unit="unit", sale_unit="unit",
            units_per_sale_unit=Decimal("1.000"), reorder_level=Decimal("0.000"),
        )
        position = stock_position()
        codes = {row["item"].code for row in position["below_reorder"]}
        self.assertNotIn("UNMANAGED-1", codes, "Short by nothing is not short.")

    def test_stock_position_ignores_fully_depleted_batches(self):
        depleted = StockBatch.objects.create(
            item=self.product, batch_number="DEPLETED",
            expiry_date=timezone.localdate() + timedelta(days=200),
            purchase_cost_per_base_unit=Decimal("2.00"),
        )
        for index, delta in enumerate([Decimal("10.000"), Decimal("-10.000")]):
            StockMovement.objects.create(
                batch=depleted, movement_type=StockMovement.MovementType.RECEIPT,
                quantity_delta=delta, reference_type="Test", reference_id=f"dep{index}",
                idempotency_key=f"depleted-{index}", entered_by=self.pharmacist,
            )
        self.assertNotIn(depleted.pk, {row["batch"].pk for row in stock_position()["rows"]})

    # ---- generated references survive a collision --------------------------

    def test_a_colliding_reference_is_retried_rather_than_lost(self):
        from unittest import mock
        clash = Patient.objects.create(first_name="First", last_name="Patient", registered_by=self.reception)
        real_uuid = __import__("uuid").uuid4

        calls = {"n": 0}

        def collide_once():
            calls["n"] += 1
            if calls["n"] == 1:
                return mock.Mock(hex=clash.patient_number.rsplit("-", 1)[-1].lower() + "0" * 26)
            return real_uuid()

        with mock.patch("hospital.models.uuid.uuid4", side_effect=collide_once):
            second = Patient.objects.create(first_name="Second", last_name="Patient", registered_by=self.reception)
        self.assertNotEqual(second.patient_number, clash.patient_number)
        self.assertTrue(second.patient_number)

    # ---- money rules are enforced where they can be reported ---------------

    def test_mpesa_without_a_reference_is_a_field_error_not_a_crash(self):
        payment = Payment(
            amount=Decimal("100.00"), method=Payment.Method.MPESA, reference="",
            received_by=self.reception, idempotency_key="mpesa-no-ref",
        )
        with self.assertRaises(ValidationError) as caught:
            payment.full_clean(exclude=["receipt_number"])
        self.assertIn("reference", caught.exception.message_dict)

    def test_the_database_refuses_a_referenceless_mpesa_payment(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                amount=Decimal("100.00"), method=Payment.Method.MPESA, reference="",
                received_by=self.reception, idempotency_key="mpesa-no-ref-db",
            )

    # ---- uploads are what they claim to be ---------------------------------

    def test_a_file_named_jpg_that_is_not_a_jpeg_is_refused(self):
        from .forms import GoodsReceiptForm
        form = GoodsReceiptForm(
            {"supplier_invoice_reference": "INV-1", "invoice_amount": "10.00",
             "delivered_on": timezone.localdate().isoformat()},
            {"invoice_photo": SimpleUploadedFile("payload.jpg", b"<svg onload=alert(1)>", content_type="image/jpeg")},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("invoice_photo", form.errors)

    def test_a_real_jpeg_is_accepted(self):
        from .forms import GoodsReceiptForm
        form = GoodsReceiptForm(
            {"supplier_invoice_reference": "INV-1", "invoice_amount": "10.00",
             "delivered_on": timezone.localdate().isoformat()},
            {"invoice_photo": SimpleUploadedFile("scan.jpg", JPEG_BYTES, content_type="image/jpeg")},
        )
        self.assertTrue(form.is_valid(), form.errors)


class FunctionalQueryBudget(int):
    """An upper bound for assertNumQueries: fail only if the budget is exceeded.

    Pinning an exact query count makes every unrelated optimisation a test
    failure. What matters here is that the number does not scale with the
    number of batches.
    """

    def __eq__(self, actual):
        return actual <= int(self)

    def __ne__(self, actual):
        return not self.__eq__(actual)

    def __hash__(self):
        return int.__hash__(self)

    def __str__(self):
        return f"at most {int(self)}"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SessionStatusTests(HospitalFixtureMixin, TestCase):
    def test_status_check_does_not_extend_session_but_post_does(self):
        self.client.force_login(self.reception)
        page = self.client.get(reverse("dashboard"))
        expires_at = page.context["session_expires_at"]
        self.assertTrue(expires_at)
        session_key = self.client.session.session_key
        saved_expiry = Session.objects.get(pk=session_key).expire_date
        status = self.client.get(reverse("session_status"))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["expires_at"], expires_at)
        self.assertEqual(Session.objects.get(pk=session_key).expire_date, saved_expiry)
        extended = self.client.post(reverse("session_status"))
        self.assertEqual(extended.status_code, 200)
        self.assertGreater(extended.json()["expires_at"], expires_at)

    def test_expired_session_and_unavailable_server_are_reported(self):
        self.assertEqual(self.client.get(reverse("session_status")).status_code, 401)
        self.client.force_login(self.reception)
        self.client.get(reverse("dashboard"))
        with patch("hospital.views.connection") as unavailable:
            unavailable.cursor.side_effect = DatabaseError("database unavailable")
            response = self.client.get(reverse("session_status"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unavailable")
        Session.objects.filter(pk=self.client.session.session_key).delete()
        self.assertEqual(self.client.get(reverse("session_status")).status_code, 401)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewQueuePaginationTests(HospitalFixtureMixin, TestCase):
    def test_reports_keep_older_payment_and_credit_reviews_reachable(self):
        invoice = Invoice.objects.create(patient=self.patient, created_by=self.reception)
        now = timezone.now()
        Payment.objects.bulk_create([
            Payment(
                receipt_number=f"RCT-PAGE-{index:03}", amount=Decimal("1.00"),
                method=Payment.Method.MPESA, reference=f"MP-PAGE-{index:03}",
                verification_status=Payment.Verification.UNVERIFIED,
                received_by=self.reception, received_at=now + timedelta(minutes=index),
                idempotency_key=f"mp-page-{index:03}",
            ) for index in range(51)
        ])
        CreditNote.objects.bulk_create([
            CreditNote(
                invoice=invoice, amount=Decimal("1.00"), reason=f"Credit page {index:03}",
                requested_by=self.reception,
            ) for index in range(51)
        ])
        self.client.force_login(self.reviewer)
        first = self.client.get(reverse("reports"))
        self.assertContains(first, "51 awaiting review")
        self.assertContains(first, "mpesa_page=2")
        self.assertContains(first, "credits_page=2")
        second = self.client.get(reverse("reports"), {"days": "30", "mpesa_page": "2", "credits_page": "2"})
        self.assertContains(second, "MP-PAGE-000")
        self.assertContains(second, "Credit page 000")
        self.assertContains(second, "days=30&amp;mpesa_page=1&amp;credits_page=2")
        self.assertContains(second, "days=30&amp;credits_page=1&amp;mpesa_page=2")

    def test_delivery_check_and_history_pages_keep_older_receipts_reachable(self):
        supplier = Supplier.objects.create(name="Pagination supplier")
        order = PurchaseOrder.objects.create(
            supplier=supplier, requested_by=self.procurement, approved_by=self.reviewer,
            status="received",
        )
        now = timezone.now()
        GoodsReceipt.objects.bulk_create([
            GoodsReceipt(
                receipt_number=f"GRN-PAGE-{index:03}", purchase_order=order,
                supplier_invoice_reference=f"SUP-PAGE-{index:03}", invoice_amount=Decimal("0.00"),
                delivered_at=now + timedelta(minutes=index), received_by=self.procurement,
            ) for index in range(51)
        ])
        self.client.force_login(self.reviewer)
        first = self.client.get(reverse("deliveries"))
        self.assertContains(first, "51 awaiting")
        self.assertContains(first, "51 total")
        self.assertContains(first, "check_page=2")
        self.assertContains(first, "history_page=2")
        last = self.client.get(reverse("deliveries"), {"check_page": "3", "history_page": "2"})
        self.assertContains(last, "GRN-PAGE-000")
        self.assertContains(last, "check_page=2&amp;history_page=2")
        self.assertContains(last, "history_page=1&amp;check_page=3")


class StaticAssetDeliveryTests(SimpleTestCase):
    """The production deployment must be able to serve its own stylesheet.

    Django serves no static files once DEBUG is off, and the supplied Caddy
    configuration reverse-proxies every path to the application. Before
    WhiteNoise was added, a production deployment answered 404 for its CSS,
    its JavaScript and its icon: a hospital system rendered as unstyled HTML.
    """

    def test_whitenoise_is_in_the_middleware_chain(self):
        self.assertTrue(
            any("whitenoise" in entry.lower() for entry in settings.MIDDLEWARE),
            "Nothing in the middleware chain can serve static files with DEBUG off.",
        )

    def test_whitenoise_runs_before_the_session_and_auth_middleware(self):
        chain = [entry.lower() for entry in settings.MIDDLEWARE]
        white = next(i for i, entry in enumerate(chain) if "whitenoise" in entry)
        session = next(i for i, entry in enumerate(chain) if "sessionmiddleware" in entry)
        self.assertLess(white, session, "Static files should not cost a session lookup.")

    def test_favicon_at_the_site_root_is_routed(self):
        """Browsers request /favicon.ico whatever the link tag says."""
        response = self.client.get("/favicon.ico")
        self.assertIn(response.status_code, {301, 302})
        self.assertIn("favicon", response["Location"])

    def test_a_data_page_is_never_cached(self):
        response = self.client.get("/health/")
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class QuickSearchTests(HospitalFixtureMixin, TestCase):
    """Search must not become a way around the page permissions.

    One box that reaches everything is only safe if it reaches exactly what the
    caller's role could already open. A receptionist who cannot open the stock
    ledger must not be able to read batch numbers out of a search box.
    """

    def search(self, user, term):
        self.client.login(username=user.username, password=self.password)
        response = self.client.get(reverse("quick_search"), {"q": term})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_signing_in_is_required(self):
        response = self.client.get(reverse("quick_search"), {"q": "test"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_a_short_term_returns_nothing_rather_than_the_whole_table(self):
        self.assertEqual(self.search(self.reception, "a"), [])

    def test_reception_finds_a_patient(self):
        kinds = {row["kind"] for row in self.search(self.reception, "Test")}
        self.assertIn("Patient", kinds)

    def test_reception_cannot_find_a_stock_batch(self):
        results = self.search(self.reception, "B-001")
        self.assertEqual(
            [row for row in results if row["kind"] == "Batch"], [],
            "Reception cannot open the stock ledger, so search must not hand them batches.",
        )

    def test_pharmacy_finds_a_stock_batch(self):
        kinds = {row["kind"] for row in self.search(self.pharmacist, "B-001")}
        self.assertIn("Batch", kinds)

    def test_pharmacy_cannot_find_a_patient_by_name(self):
        results = self.search(self.pharmacist, "Test")
        self.assertEqual([row for row in results if row["kind"] == "Patient"], [])

    def test_the_owner_reaches_both(self):
        self.assertIn("Patient", {row["kind"] for row in self.search(self.owner, "Test")})
        self.assertIn("Batch", {row["kind"] for row in self.search(self.owner, "B-001")})

    def test_a_result_url_is_safe_to_put_in_an_attribute(self):
        """A batch number with a quote in it must not escape the link."""
        awkward = StockBatch.objects.create(
            item=self.product, batch_number='B"><img src=x>',
            purchase_cost_per_base_unit=Decimal("1.00"),
        )
        StockMovement.objects.create(
            batch=awkward, movement_type=StockMovement.MovementType.RECEIPT,
            quantity_delta=Decimal("5.000"), reference_type="Test", reference_id="x",
            idempotency_key="awkward-batch", entered_by=self.pharmacist,
        )
        results = self.search(self.pharmacist, 'B"><img')
        self.assertTrue(results)
        for row in results:
            self.assertNotIn('"', row["url"])
            self.assertNotIn("<", row["url"])

    def test_search_answers_in_a_bounded_number_of_queries(self):
        """One query per scope, not one per row. Signing in is not measured."""
        self.client.login(username=self.owner.username, password=self.password)
        self.client.get(reverse("quick_search"), {"q": "warm the session"})
        with self.assertNumQueries(FunctionalQueryBudget(12)):
            self.client.get(reverse("quick_search"), {"q": "test"})


class PresentationFilterTests(SimpleTestCase):
    """How long a wait has run has to be visible without being read."""

    def test_a_short_wait_is_not_flagged(self):
        from .templatetags.hospital_extras import wait_class, wait_note, wait_row_class
        recent = timezone.now() - timedelta(minutes=5)
        self.assertEqual(wait_class(recent), "wait")
        self.assertEqual(wait_row_class(recent), "")
        self.assertEqual(wait_note(recent), "")

    def test_an_hour_is_a_warning(self):
        from .templatetags.hospital_extras import wait_class, wait_row_class
        waited = timezone.now() - timedelta(minutes=75)
        self.assertEqual(wait_class(waited), "wait wait-warn")
        self.assertEqual(wait_row_class(waited), "row-warn")

    def test_half_a_day_is_urgent_and_says_so_in_words(self):
        from .templatetags.hospital_extras import wait_class, wait_note, wait_row_class
        waited = timezone.now() - timedelta(hours=12)
        self.assertEqual(wait_class(waited), "wait wait-urgent")
        self.assertEqual(wait_row_class(waited), "row-urgent")
        # Colour alone is not a message; the same fact is available as text.
        self.assertTrue(wait_note(waited))

    def test_a_missing_timestamp_does_not_raise(self):
        from .templatetags.hospital_extras import wait_class, wait_note
        self.assertEqual(wait_class(None), "wait")
        self.assertEqual(wait_note(None), "")


class MoneyPresentationTests(TestCase):
    """A column of figures is read, not parsed."""

    def test_large_amounts_are_grouped(self):
        from django.template import Context, Template
        rendered = Template("{{ v|floatformat:2 }}").render(Context({"v": Decimal("1234567.5")}))
        self.assertEqual(rendered, "1,234,567.50")

    def test_a_figure_copied_off_the_screen_is_accepted_back(self):
        """Displaying 1,540.00 and then rejecting it as input is a dead end."""
        from .forms import PaymentForm
        form = PaymentForm({"amount": "1,540.00", "method": "cash", "reference": ""})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["amount"], Decimal("1540.00"))
