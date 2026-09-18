# Backup and recovery

## Daily backup

Run `scripts/backup.ps1` from Task Scheduler under the restricted application service account. Set `KFB_BACKUP_DIRECTORY` to mounted/removable media and `KFB_BACKUP_ENCRYPTION_RECIPIENT` to the owner-controlled `age` recipient. The production command refuses an unencrypted backup.

Each archive includes the database dump, protected attachment files and a manifest with UTC creation time, backend and database checksum. The sidecar checksum verifies the final archive. Copy the encrypted archive off the server. Monitor Task Scheduler exit status and raise an exception when the job fails or becomes overdue.

## Restore rehearsal

1. Use a separate isolated test host—never overwrite the active hospital database.
2. Verify the archive SHA-256 sidecar, decrypt with the separately held recovery key and inspect `manifest.json`.
3. For PostgreSQL, create an empty test database and restore with `pg_restore --clean --if-exists --no-owner` using a restricted test account. For demo SQLite, copy `database.sqlite3` to a separate demo checkout.
4. Restore `media/` to the test media directory with access restricted to the application account.
5. Run migrations only after recording the restored schema version, then sign in with a test administrator.
6. Compare counts and totals for patients, invoices, valid payments, payment allocations, stock movements and current batch balances. Open representative protected attachments through an authenticated account.
7. Record date, operator, archive name, checksum, results, differences and corrective action.

The recovery point is the start of the last successful backup. Later transactions are not included and may require controlled back-entry from numbered paper records.

