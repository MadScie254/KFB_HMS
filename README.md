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
- Delivery receiving against an approved order, with a mandatory photograph of the supplier invoice, per-batch expiry and actual cost, partial deliveries, and an independent check the receiver cannot perform.
- Stock counts frozen at a cutoff, with a blind-count option and an approved adjustment movement for each variance; no screen anywhere edits a quantity directly.
- Ward and departmental custody: stock issued to a named person stays visible as outstanding until it is administered, returned or wasted, and is never deducted twice.
- Write-offs and disposition: expired or damaged stock is proposed with a reason and removed only on a second person's approval.
- Stock statistics: sellable valuation at cost and at selling price, products below reorder level, near-expiry and expired batches, cost of goods dispensed, product sales and gross margin, and most-dispensed products.
- Stock intelligence: unexplained loss valued and set against cost of goods, supplier unit-cost movement between deliveries, and whether the controls are actually being followed.
- An owner's brief that ranks what needs attention today. It is a page in the application; nothing is messaged anywhere.
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

## Demonstrate the stock replenishment and balancing workflow

Stock leaving the shelf is only half the ledger. To see the other half:

1. Sign in as `procurement.demo` and open Deliveries. The seeded, independently approved restock order is waiting to be received.
2. Choose Receive. Enter the supplier invoice number and total, attach a photograph of the invoice (any JPG, PNG, HEIC or PDF under 10 MB — the form will not post without one), and record a batch number, expiry date, actual quantity and actual unit cost for each line.
3. Post the delivery. Stock rises immediately as receipt movements. If the delivered cost differs from the approved quote, or the invoice total does not match the goods counted in, the delivery is still posted — the difference is raised in the exception centre rather than blocking the record of what arrived. An already-expired batch is refused.
4. Sign in as `pharmacy.demo` and open the same delivery. A second member of staff confirms it; the person who received it cannot.
5. Open Stock to see the valuation, reorder position and expiry standing update, then Stock counts to freeze a count sheet, enter what is physically on the shelf, and submit it.
6. Sign in as `reviewer.demo` and approve the count. Only that approval posts an adjustment movement for each variance; rejecting it changes no balance at all.

For the outpatient slice, reception registers/finds a patient and starts a visit; the clinician opens the queue, saves/signs a note, and can request departmental work. Pharmacy and reception then use the same settlement/dispense controls.

## Production-style local server

1. Copy `.env.example` to `.env` and load those values with the host's service manager. Django deliberately does not silently read `.env`; production secrets should be injected by the service account.
2. Set `KFB_ENV=production`, a long random `KFB_SECRET_KEY`, host names, secure-cookie settings, and a PostgreSQL URL.
3. Run migrations and create named users with the admin command. Never run `seed_demo`.
4. Run `.\.venv\Scripts\python.exe manage.py check --deploy` and `.\.venv\Scripts\python.exe manage.py check_readiness`.
5. Configure Caddy from `deploy\Caddyfile`, install its internal CA certificate on authorised workstations, and start Waitress with `scripts\start-server.ps1` as a restricted Windows service. Production Waitress binds only to loopback and refuses startup unless HTTPS redirect is enabled.

The LAN design is one application server and one PostgreSQL database. Caddy terminates internal HTTPS, while Waitress and the database stay off the workstation-facing interface. Firewall scope, automatic service start and private remote owner access must still be configured by the local implementer.

After the real internal HTTPS URL works, create the workstation shortcut with `scripts\create-desktop-shortcut.ps1 -ApplicationUrl "https://hospital.internal"`. Complete `docs/HOST_READINESS.md` before the pilot.

## Checks

Run everything CI runs, on any machine:

```bash
./scripts/checks.sh
```

That is ruff, the test suite, a check that migrations match the models, and Django's deployment check. A test keeps the script and `.github/workflows/quality.yml` from drifting apart.

The hosted workflow is currently red for a reason no commit can fix: every GitHub Actions run in this repository, including the ones predating this work, fails within seconds without a runner because the account is billing-locked. Clear the billing hold under GitHub → Settings → Billing and plans and the same checks will run there.

Tests alone:

```powershell
.\.venv\Scripts\python.exe manage.py test hospital
```

The suite covers PDF exports, protected attachments, multi-line prescriptions and purchasing, M-PESA and credit-note review, requester segregation, CSV migrations, outpatient prescription pricing/payment/dispense, stock arithmetic, duplicate references, expiry/quarantine controls, partial allocations, cash-shift math, bilateral case fees, signed-note immutability and role denial.

It also covers the stock control loop: a delivery posting receipt movements and balancing against its invoice, refusal without an invoice photograph, refusal of expired batches, per-order supplier-invoice uniqueness, partial deliveries, price and invoice-total differences raised as exceptions without blocking the post, the receiver being unable to check their own delivery, count snapshots frozen at their cutoff, approved counts posting adjustments, reviewer segregation, and the demo seed command completing with the correct roles.

And the custody and measurement work: issuing to a ward moving custody without a sale, administration never deducting stock twice, returns coming back quarantined, write-offs needing a second approver, expired stock never being released back to sellable, hospital stock reconciling as pharmacy balance plus outstanding custody, unexplained loss being kept separate from authorised write-offs, and every figure reporting as unavailable rather than as zero when there is nothing to measure.

## Backup

Demo backup:

```powershell
.\scripts\backup.ps1 -OutputDirectory "E:\KFB-HMS-Backups"
```

Production requires `KFB_BACKUP_ENCRYPTION_RECIPIENT` and the `age` executable. The command creates a database/attachment archive, manifest and SHA-256 checksum. Copy it to separately stored media. A backup left on the server disk is not sufficient. Follow `docs/BACKUP_AND_RECOVERY.md` for restore rehearsal and recovery-point limits.

`scripts\restore-backup.ps1` verifies the checksum and refuses a non-empty target directory so recovery starts in an isolated location.
