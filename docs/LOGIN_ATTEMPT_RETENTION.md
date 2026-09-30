# Failed-login evidence retention

Failed sign-ins are stored with account name, network address, browser agent and time. A successful sign-in clears that account's lockout counter by marking its prior failures as cleared; it does not delete the evidence. Address-based lockout continues to count those failures through its 15-minute window.

The operational database keeps attempts for 365 days by default. The owner must confirm the applicable legal and institutional retention period before changing `-RetentionDays`; this default is an operational choice, not a statement of legal sufficiency. The minimum accepted period is one day, so pruning cannot remove evidence in the active lockout window.

After the daily database backup succeeds, schedule `scripts/prune-login-attempts.ps1` under the restricted service account. Set `KFB_ENV=production`, `KFB_BACKUP_DIRECTORY` to separately protected storage, and `KFB_BACKUP_ENCRYPTION_RECIPIENT` to the owner-held `age` recipient. The command writes an encrypted ZIP with a JSONL export and manifest, plus a SHA-256 sidecar, before deleting rows older than the cutoff. If encryption or publication fails, no rows are deleted. Keep those exports under the approved evidence retention and access policy; verify and test decryption periodically. Monitor Task Scheduler failures.

The owner can inspect recent evidence in the read-only Django admin. Archived exports require the separately held `age` identity and should be reviewed only on an isolated, access-controlled machine.
