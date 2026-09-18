# Kingdom Faith Based Hospital Operations System

A local-first, server-rendered hospital operations application for Kingdom Faith Based Hospital in Malaha, Webuye. The repository starts in a clearly labelled fictional demonstration environment. It is not a public marketing website and is not production-ready without the checklist in `docs/PRODUCTION_PREREQUISITES.md`.

## What works now

- Named role accounts with server-side route enforcement, screen lock and no-store browser headers.
- Patient registration, duplicate warning, patient search, longitudinal visits and a role-scoped patient banner.
- Reception queue, emergency override record, server-held clinical drafts and immutable signed-note versions.
- Service orders and authorised result release workflow for laboratory/imaging work.
- Walk-in pharmacy basket → reception payment → receipt → server-cleared dispense → batch stock ledger.
- Cash and manually recorded M-PESA with unique references and a visibly unverified state.
- Cashier shift opening/closing calculations and exception creation for variances.
- Versioned catalogue prices, base-unit stock, FEFO batch allocation, quarantine/expiry rejection and idempotent dispensing.
- Configurable ward/bed register and admission records with separate clinical and financial status.
- Eye session/case records with separate patients/eyes and one provisional case fee per completed patient.
- Purchase requests with independent approval enforcement.
- Owner reports tied to posted invoices/payments and a prioritised exception centre.
- Product CSV dry run with row-level errors and idempotent commit.
- Printable receipts and four numbered downtime forms.
- Checksummed database/attachment backups; production backups refuse to run without an encryption recipient.

See `docs/IMPLEMENTATION_STATUS.md` for exact scope and known gaps.

## Quick start — fictional demo only

Prerequisites: Windows 10/11 or a supported Linux host, Python 3.14, and a modern browser.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000`. Seeded usernames are `owner.demo`, `reception.demo`, `clinician.demo`, `nurse.demo`, `pharmacy.demo`, `lab.demo`, `eye.demo`, `procurement.demo`, and `reviewer.demo`. The demo-only password is `Demo-Only-2026!`.

Do not reuse these accounts or this password in production. `seed_demo` refuses to run unless `KFB_ENV=demo`.

## Demonstrate the working walk-in workflow

1. Sign in as `pharmacy.demo`, open Pharmacy, and prepare a walk-in basket for a non-prescription demo item.
2. Sign out and sign in as `reception.demo`. Open a cashier shift, open Payments, take cash for the basket, and print the receipt.
3. Sign in again as `pharmacy.demo`. Open the now-cleared order and confirm actual dispense.
4. Open Stock to see the batch movement and reduced balance. A repeated dispense submission will not deduct twice.

For the outpatient slice, reception registers/finds a patient and starts a visit; the clinician opens the queue, saves/signs a note, and can request departmental work. Pharmacy and reception then use the same settlement/dispense controls.

## Production-style local server

1. Copy `.env.example` to `.env` and load those values with the host's service manager. Django deliberately does not silently read `.env`; production secrets should be injected by the service account.
2. Set `KFB_ENV=production`, a long random `KFB_SECRET_KEY`, host names, secure-cookie settings, and a PostgreSQL URL.
3. Run migrations and create named users with the admin command. Never run `seed_demo`.
4. Run `.\.venv\Scripts\python.exe manage.py check --deploy` and `.\.venv\Scripts\python.exe manage.py check_readiness`.
5. Start Waitress with `scripts\start-server.ps1`, or install it as a restricted Windows service using the hospital's approved service manager.

The LAN design is one application server and one PostgreSQL database. Workstations connect in supported browsers; the database file/port is never shared directly. HTTPS for LAN access, firewall scope, automatic service start and private remote owner access must be configured by the local implementer.

After the real internal HTTPS URL works, create the workstation shortcut with `scripts\create-desktop-shortcut.ps1 -ApplicationUrl "https://hospital.internal"`. Complete `docs/HOST_READINESS.md` before the pilot.

## Tests

```powershell
.\.venv\Scripts\python.exe manage.py test hospital
```

The suite covers outpatient prescription pricing/payment/dispense, walk-in reconciliation, base-unit stock arithmetic, duplicate M-PESA references, expiry/quarantine controls, partial allocations and deposits, cash-shift math, independent review, bilateral case fee, signed-note immutability, CSV idempotency and role denial.

## Backup

Demo backup:

```powershell
.\scripts\backup.ps1 -OutputDirectory "E:\KFB-HMS-Backups"
```

Production requires `KFB_BACKUP_ENCRYPTION_RECIPIENT` and the `age` executable. The command creates a database/attachment archive, manifest and SHA-256 checksum. Copy it to separately stored media. A backup left on the server disk is not sufficient. Follow `docs/BACKUP_AND_RECOVERY.md` for restore rehearsal and recovery-point limits.

`scripts\restore-backup.ps1` verifies the checksum and refuses a non-empty target directory so recovery starts in an isolated location.
