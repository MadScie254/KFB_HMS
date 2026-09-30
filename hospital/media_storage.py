"""Keep media files immutable and exclude in-flight uploads from backups."""

import os
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage

LOCK_FILENAME = ".backup-media.lock"


@contextmanager
def media_write_lock(*, timeout=15):
    """Cross-process lock shared by file writes and the backup snapshot window."""
    media_root = Path(settings.MEDIA_ROOT)
    media_root.mkdir(parents=True, exist_ok=True)
    with (media_root / LOCK_FILENAME).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ValidationError("Media backup is in progress. Try the upload again shortly.") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BackupSafeFileSystemStorage(FileSystemStorage):
    """Uploads finish before their DB reference can enter a backup snapshot."""

    def _save(self, name, content):
        with media_write_lock():
            return super()._save(name, content)

    def delete(self, name):
        raise ValidationError("Uploaded evidence is immutable; do not delete its backing file.")
