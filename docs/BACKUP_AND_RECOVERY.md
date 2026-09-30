# Backup and recovery

## Daily backup

Run `scripts/backup.ps1` from Task Scheduler under the restricted application service account. Set `KFB_BACKUP_DIRECTORY` to mounted/removable media and `KFB_BACKUP_ENCRYPTION_RECIPIENT` to the owner-controlled `age` recipient. The production command refuses an unencrypted backup.

Each archive includes a consistent database snapshot, protected attachment files and a manifest with the UTC snapshot window, database checksum and SHA-256 of each media file. Application uploads and backup creation use the same cross-process media lock, so a referenced attachment is fully written before it can enter the database snapshot. Backups temporarily defer uploads; schedule them outside busy clinical hours. The sidecar checksum verifies the final archive. Store backups outside `MEDIA_ROOT`, copy the encrypted archive off the server, and monitor Task Scheduler exit status for failures or overdue jobs.

## Restore rehearsal

1. Use a separate isolated test host—never overwrite the active hospital database.
2. Verify the archive SHA-256 sidecar, decrypt with the separately held recovery key and inspect `manifest.json`. The restore script decrypts in a restricted temporary directory and removes the plaintext ZIP after extraction, including on failure.
3. For PostgreSQL, create an empty test database and restore with `pg_restore --clean --if-exists --no-owner` using a restricted test account. For demo SQLite, copy `database.sqlite3` to a separate demo checkout.
4. Verify restored `media/` files against `media_sha256` in the manifest, then place them in the test media directory with access restricted to the application account. Stop and investigate any missing or mismatched file.
5. Run migrations only after recording the restored schema version, then sign in with a test administrator.
6. Compare counts and totals for patients, invoices, valid payments, payment allocations, stock movements and current batch balances. Open representative protected attachments through an authenticated account.
7. Record date, operator, archive name, checksum, results, differences and corrective action.

The database recovery point lies within the UTC snapshot window recorded in the last successful manifest. The media lock spans that snapshot and archive creation, so application uploads cannot change the media set during the backup. A file uploaded immediately before the snapshot but whose database transaction commits later may be present as an unreferenced file; it does not change restored clinical records. Transactions after the database snapshot are not included and may require controlled back-entry from numbered paper records. Manual changes to `MEDIA_ROOT` outside the application are prohibited during backup.
