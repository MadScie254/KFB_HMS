# Production prerequisites

Do not enter real patient data until every applicable item is resolved and signed off by the hospital.

## Hospital configuration

- Appoint named users, a delegated independent reviewer and account-recovery custodians. Remove all demo users/data.
- Import and approve the official service/product price list. Confirm rounding and effective dates.
- Perform a witnessed opening stock count with batches, expiry, base-unit conversions and cost basis.
- Configure the real ward/bed register and explicit bed/day charging start, end and rounding rules.
- Confirm eye package rates, exact included items, bilateral/repeated-session fee treatment and the visiting clinician agreement.
- Confirm medicine restrictions, substitution policy, return/quarantine/write-off policy, emergency overrides and approval thresholds.
- Have qualified staff review maternity, theatre, dental, nursing and clinical templates. The software supplies no diagnosis, dosing, normal ranges or clinical alarms.

## Legal and governance review

- Verify current Kenyan health-data, professional-practice, fiscal/receipt, retention and hosting obligations against authoritative sources at deployment time.
- Approve privacy notices, access matrix, audit review schedule, emergency access, retention/deletion policy and breach response.
- Confirm whether and how SHA/insurance, taxation/fiscal integration and external reporting apply. They are disabled/not claimed here.

## Host and network readiness

- A reliable dedicated host that supports Python 3.14 and PostgreSQL, with restricted service and database accounts.
- Repaired/verified workstations, supported browsers, reliable LAN/router, ordinary/receipt printer testing and a desktop shortcut to the HTTPS application URL.
- UPS for safe shutdown, surge protection and a rehearsed power-loss recovery process. A UPS is not represented as two-day power.
- LAN firewall rules that expose only the HTTPS application; never expose PostgreSQL or an unauthenticated application to the internet.
- Private authenticated remote access for the owner, with additional authentication and honest last-refresh status. No remote-live claim while the hospital link or power is down.

## Secrets and payment integration

- Generate a unique production secret, secure cookies and trusted HTTPS origins. Store secrets outside source control.
- Keep M-PESA in manual/unverified mode until Safaricom account capability, credentials, public callback reachability, signature/event verification and reconciliation have been tested.
- Do not treat screenshots as authoritative payment evidence. Confirm unique references, reversals, delayed events, amount mismatch and split allocations.

## Backup and recovery

- Configure an `age` encryption recipient whose recovery key is held by the owner and not only on the server.
- Schedule daily database + attachment backups, checksum verification, retention and a copy on separately stored media.
- Restore on another environment, compare patient/invoice/payment/stock balances, verify protected attachments and record the rehearsal.
- Accept the recovery-point limitation: transactions after the last successful backup may need paper reconstruction.

## Pilot gate

- Run the automated suite against PostgreSQL and perform supervised role-by-role acceptance with staff.
- Test double-clicks, two-user last-stock contention, printer failure/reprint, overnight shifts, internet loss, server loss, power recovery and downtime back-entry.
- Display official prices and insist on patient receipts; restrict physical stock and use witnessed counts. Software cannot by itself prevent collusion, unrecorded cash sales or physical theft.

