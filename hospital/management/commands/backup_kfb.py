import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Create a checksummed database + attachment backup. Production requires configured encryption."

    def add_arguments(self, parser):
        parser.add_argument("--output", help="Backup directory; overrides KFB_BACKUP_DIRECTORY")

    def handle(self, *args, **options):
        destination = Path(options.get("output") or os.getenv("KFB_BACKUP_DIRECTORY", settings.BASE_DIR / "backups")).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recipient = os.getenv("KFB_BACKUP_ENCRYPTION_RECIPIENT", "").strip()
        if not settings.DEMO_MODE and not recipient:
            raise CommandError("KFB_BACKUP_ENCRYPTION_RECIPIENT is required for production backups.")

        with tempfile.TemporaryDirectory(prefix="kfb-backup-") as temp_dir:
            work = Path(temp_dir)
            database_file = work / ("database.sqlite3" if connection.vendor == "sqlite" else "database.dump")
            if connection.vendor == "sqlite":
                connection.close()
                shutil.copy2(Path(settings.DATABASES["default"]["NAME"]), database_file)
            elif connection.vendor == "postgresql":
                config = settings.DATABASES["default"]
                env = os.environ.copy()
                env["PGPASSWORD"] = config["PASSWORD"]
                command = ["pg_dump", "--format=custom", "--no-owner", "--file", str(database_file), "--host", config["HOST"], "--port", str(config["PORT"]), "--username", config["USER"], config["NAME"]]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                if result.returncode:
                    raise CommandError(f"pg_dump failed: {result.stderr.strip()}")
            else:
                raise CommandError(f"Unsupported database backend: {connection.vendor}")

            manifest = {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "environment": settings.ENVIRONMENT,
                "database_vendor": connection.vendor,
                "database_sha256": self._sha256(database_file),
                "recovery_point": "Database state at backup command start; changes after that time are not included.",
            }
            (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            archive = destination / f"kfb-hms-{timestamp}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
                output.write(database_file, database_file.name)
                output.write(work / "manifest.json", "manifest.json")
                media_root = Path(settings.MEDIA_ROOT)
                if media_root.exists():
                    for file in media_root.rglob("*"):
                        if file.is_file():
                            output.write(file, Path("media") / file.relative_to(media_root))

            checksum = self._sha256(archive)
            checksum_path = archive.with_suffix(archive.suffix + ".sha256")
            checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
            final_path = archive
            if recipient:
                age = shutil.which("age")
                if not age:
                    raise CommandError("Encryption recipient is configured but the 'age' executable is not installed.")
                encrypted = archive.with_suffix(".zip.age")
                result = subprocess.run([age, "--recipient", recipient, "--output", str(encrypted), str(archive)], capture_output=True, text=True)
                if result.returncode:
                    raise CommandError(f"age encryption failed: {result.stderr.strip()}")
                archive.unlink()
                checksum_path.unlink(missing_ok=True)
                encrypted_checksum = self._sha256(encrypted)
                encrypted.with_suffix(encrypted.suffix + ".sha256").write_text(f"{encrypted_checksum}  {encrypted.name}\n", encoding="ascii")
                final_path = encrypted

        self.stdout.write(self.style.SUCCESS(f"Backup created: {final_path}"))
        self.stdout.write("Copy it to separately stored media and test restoration on another environment.")

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

