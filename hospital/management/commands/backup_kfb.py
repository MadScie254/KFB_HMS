import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from hospital.media_storage import LOCK_FILENAME, media_write_lock


class Command(BaseCommand):
    help = "Create a checksummed database + attachment backup. Production requires configured encryption."

    def add_arguments(self, parser):
        parser.add_argument("--output", help="Backup directory; overrides KFB_BACKUP_DIRECTORY")

    def handle(self, *args, **options):
        recipient = os.getenv("KFB_BACKUP_ENCRYPTION_RECIPIENT", "").strip()
        if not settings.DEMO_MODE and not recipient:
            raise CommandError("KFB_BACKUP_ENCRYPTION_RECIPIENT is required for production backups.")
        age = shutil.which("age") if recipient else None
        if recipient and not age:
            raise CommandError("Encryption recipient is configured but the 'age' executable is not installed.")

        destination = Path(options.get("output") or os.getenv("KFB_BACKUP_DIRECTORY", settings.BASE_DIR / "backups")).resolve()
        media_root = Path(settings.MEDIA_ROOT).resolve()
        if destination == media_root or media_root in destination.parents:
            raise CommandError("Backup directory must be outside MEDIA_ROOT.")
        destination.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_name = f"kfb-hms-{timestamp}-{uuid.uuid4().hex[:8]}.zip"

        # File storage takes this same cross-process lock while writing each
        # immutable upload. A DB row cannot reference a partly written file.
        with media_write_lock(timeout=60), tempfile.TemporaryDirectory(prefix="kfb-backup-") as temp_dir:
            work = Path(temp_dir)
            if os.name != "nt":
                work.chmod(0o700)
            database_file = work / ("database.sqlite3" if connection.vendor == "sqlite" else "database.dump")
            snapshot_started = datetime.now(timezone.utc)
            if connection.vendor == "sqlite":
                # VACUUM INTO takes a consistent snapshot through SQLite itself.
                # A plain file copy can capture a torn page set while another
                # Waitress thread is mid-write, and silently omits the -wal
                # sidecar, so the restored copy loses committed transactions.
                with connection.cursor() as cursor:
                    cursor.execute("VACUUM INTO %s", [str(database_file)])
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

            snapshot_finished = datetime.now(timezone.utc)
            media_files = []
            media_hashes = {}
            if media_root.exists():
                for file in sorted(media_root.rglob("*")):
                    if file.name == LOCK_FILENAME and file.parent == media_root:
                        continue
                    if file.is_symlink():
                        raise CommandError(f"Media contains a symlink; inspect before backup: {file}")
                    if file.is_file():
                        relative_name = file.relative_to(media_root).as_posix()
                        media_files.append((file, relative_name))
                        media_hashes[relative_name] = self._sha256(file)
            manifest = {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "environment": settings.ENVIRONMENT,
                "database_vendor": connection.vendor,
                "database_sha256": self._sha256(database_file),
                "database_snapshot_window_utc": [snapshot_started.isoformat(), snapshot_finished.isoformat()],
                "media_sha256": media_hashes,
                "recovery_point": "Database snapshot captured within the stated window. Application media writes were quiesced through archive creation; later database transactions are not included.",
            }
            (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            # Never put a plaintext patient archive on the backup medium.
            archive = work / archive_name
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
                output.write(database_file, database_file.name)
                output.write(work / "manifest.json", "manifest.json")
                for file, relative_name in media_files:
                    output.write(file, Path("media") / relative_name)

            final_name = f"{archive_name}.age" if recipient else archive_name
            final_path = destination / final_name
            with tempfile.TemporaryDirectory(prefix=".kfb-publish-", dir=destination) as publish_dir:
                staged = Path(publish_dir) / final_name
                if recipient:
                    result = subprocess.run(
                        [age, "--recipient", recipient, "--output", str(staged), str(archive)],
                        capture_output=True, text=True,
                    )
                    if result.returncode:
                        raise CommandError(f"age encryption failed: {result.stderr.strip()}")
                else:
                    shutil.copyfile(archive, staged)
                checksum = self._sha256(staged)
                staged_checksum = Path(publish_dir) / f"{final_name}.sha256"
                staged_checksum.write_text(f"{checksum}  {final_name}\n", encoding="ascii")
                try:
                    os.replace(staged, final_path)
                    os.replace(staged_checksum, destination / staged_checksum.name)
                except OSError:
                    final_path.unlink(missing_ok=True)
                    raise

        self.stdout.write(self.style.SUCCESS(f"Backup created: {final_path}"))
        self.stdout.write("Copy it to separately stored media and test restoration on another environment.")

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
