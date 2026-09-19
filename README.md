# Kingdom Faith Based Hospital Operations System

A local-first, server-rendered hospital operations application for Kingdom Faith Based Hospital in Malaha, Webuye. The repository starts in a clearly labelled fictional demonstration environment. It is not a public marketing website and is not production-ready without the checklist in `docs/PRODUCTION_PREREQUISITES.md`.

## What works now

- Named role accounts with server-side route enforcement, screen lock and no-store browser headers.
- Patient registration, duplicate warning, patient search, longitudinal visits and a role-scoped patient banner.
- Reception queue, explicit triage priority, emergency override record, concurrency-safe clinical drafts and immutable signed-note versions.
- Service orders and independently released laboratory/imaging results; requesters cannot release their own work.
- Walk-in pharmacy basket → reception payment → receipt → server-cleared dispense → batch stock ledger.
- Cash and manually recorded M-PESA with unique references, independent reviewer verification and a visibly unverified state.
- Cashier shift opening/closing calculations and exception creation for variances.
- Versioned catalogue prices, base-unit stock, FEFO batch allocation, quarantine/expiry rejection and idempotent dispensing.
- Configurable ward/bed register and admission records with separate clinical and financial status.
- Eye session/case records with separate patients/eyes and one provisional case fee per completed patient.
- Multi-line prescriptions and purchase requests with independent approval enforcement.
- Owner reports tied to posted invoices/payments, a prioritised exception centre and watermarked KFBH PDF downloads.
- Protected clinical attachment upload/download and patient access-record PDF export.
- Read-only audit review with searchable attribution and PostgreSQL database-level append-only enforcement.
- Product, patient, witnessed opening-stock and reviewed opening-receivable CSV dry runs with row-level errors and idempotent commit.
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
5. Configure Caddy from `deploy\Caddyfile`, install its internal CA certificate on authorised workstations, and start Waitress with `scripts\start-server.ps1` as a restricted Windows service. Production Waitress binds only to loopback and refuses startup unless HTTPS redirect is enabled.

The LAN design is one application server and one PostgreSQL database. Caddy terminates internal HTTPS, while Waitress and the database stay off the workstation-facing interface. Firewall scope, automatic service start and private remote owner access must still be configured by the local implementer.

After the real internal HTTPS URL works, create the workstation shortcut with `scripts\create-desktop-shortcut.ps1 -ApplicationUrl "https://hospital.internal"`. Complete `docs/HOST_READINESS.md` before the pilot.

## Tests

```powershell
.\.venv\Scripts\python.exe manage.py test hospital
```

The suite covers PDF exports, protected attachments, multi-line prescriptions and purchasing, M-PESA and credit-note review, requester segregation, CSV migrations, outpatient prescription pricing/payment/dispense, stock arithmetic, duplicate references, expiry/quarantine controls, partial allocations, cash-shift math, bilateral case fees, signed-note immutability and role denial.

## Backup

Demo backup:

```powershell
.\scripts\backup.ps1 -OutputDirectory "E:\KFB-HMS-Backups"
```

Production requires `KFB_BACKUP_ENCRYPTION_RECIPIENT` and the `age` executable. The command creates a database/attachment archive, manifest and SHA-256 checksum. Copy it to separately stored media. A backup left on the server disk is not sufficient. Follow `docs/BACKUP_AND_RECOVERY.md` for restore rehearsal and recovery-point limits.

`scripts\restore-backup.ps1` verifies the checksum and refuses a non-empty target directory so recovery starts in an isolated location.
