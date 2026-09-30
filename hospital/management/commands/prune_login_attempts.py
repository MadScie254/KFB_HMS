"""Export old failed-login evidence encrypted before removing operational rows."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from hospital.models import LoginAttempt


class Command(BaseCommand):
    help = "Encrypt and export expired login attempts before pruning them from the live database."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Directory for encrypted evidence archives")
        parser.add_argument("--retention-days", type=int, default=365)

    def handle(self, *args, **options):
        days = options["retention_days"]
        if days < 1:
            raise CommandError("Retention must be at least one day, beyond the active lockout window.")
        recipient = os.getenv("KFB_BACKUP_ENCRYPTION_RECIPIENT", "").strip()
        age = shutil.which("age") if recipient else None
        if not recipient or not age:
            raise CommandError("Configure KFB_BACKUP_ENCRYPTION_RECIPIENT and install age before pruning evidence.")

        destination = Path(options["output"]).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        cutoff = timezone.now() - timedelta(days=days)
        with transaction.atomic():
            eligible = LoginAttempt.objects.filter(attempted_at__lt=cutoff)
            highest_id = eligible.aggregate(last=Max("pk"))["last"]
            if highest_id is None:
                self.stdout.write("No login attempts are older than the retention cutoff.")
                return
            selected = eligible.filter(pk__lte=highest_id)

            with tempfile.TemporaryDirectory(prefix="kfb-login-evidence-") as temporary:
                work = Path(temporary)
                if os.name != "nt":
                    work.chmod(0o700)
                rows_file = work / "login-attempts.jsonl"
                digest = hashlib.sha256()
                count = 0
                with rows_file.open("wb") as output:
                    for pk, username, address, agent, attempted, cleared in selected.order_by("pk").values_list(
                        "pk", "username", "ip_address", "user_agent", "attempted_at", "cleared_at"
                    ).iterator(chunk_size=1000):
                        line = (json.dumps({
                            "id": pk, "username": username, "ip_address": address, "user_agent": agent,
                            "attempted_at": attempted.isoformat(),
                            "cleared_at": cleared.isoformat() if cleared else None,
                        }, separators=(",", ":")) + "\n").encode("utf-8")
                        output.write(line)
                        digest.update(line)
                        count += 1

                manifest = {
                    "created_utc": timezone.now().isoformat(),
                    "retention_days": days,
                    "cutoff_utc": cutoff.isoformat(),
                    "row_count": count,
                    "login_attempts_sha256": digest.hexdigest(),
                }
                (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                archive = work / "login-evidence.zip"
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                    zipped.write(rows_file, rows_file.name)
                    zipped.write(work / "manifest.json", "manifest.json")

                name = f"kfb-login-evidence-{timezone.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.zip.age"
                final = destination / name
                with tempfile.TemporaryDirectory(prefix=".kfb-login-publish-", dir=destination) as publishing:
                    staged = Path(publishing) / name
                    result = subprocess.run(
                        [age, "--recipient", recipient, "--output", str(staged), str(archive)],
                        capture_output=True, text=True,
                    )
                    if result.returncode:
                        raise CommandError(f"Login evidence encryption failed: {result.stderr.strip()}")
                    encrypted_hash = self._sha256(staged)
                    checksum = Path(publishing) / f"{name}.sha256"
                    checksum.write_text(f"{encrypted_hash}  {name}\n", encoding="ascii")
                    try:
                        os.replace(staged, final)
                        os.replace(checksum, destination / checksum.name)
                    except OSError:
                        final.unlink(missing_ok=True)
                        raise

            # Only rows included in the exported ID range can be removed. A
            # failure before this point keeps every row in the live database.
            deleted, _ = selected.delete()

        self.stdout.write(self.style.SUCCESS(f"Exported {count} attempts to {final}; pruned {deleted} live rows."))

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
