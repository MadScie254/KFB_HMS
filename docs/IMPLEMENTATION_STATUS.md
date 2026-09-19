# Implementation status

Updated: 19 September 2026. All included data is fictional.

Corrections below follow the engineering audit in `AUDIT_2026-09-18.md`, which found three rows claiming behaviour the code did not have. The defects are fixed and covered by regression tests; the wording is corrected here so the table matches the software.

## Stage 1

| Area | Status | Evidence / boundary |
|---|---|---|
| Design system and responsive shell | Working | Icon-led role navigation, desktop/mobile layout, repeatable-line forms, accessible focus states, print styles and local assets only. |
| Authentication and permissions | Working core | Named Django accounts, server-side role decorators, session expiry, enforced lock screen and per-username sign-in throttling. The lock screen was a no-op until the September 2026 audit (audit C1); enforcement now runs in `process_view` and is tested. Second factor and additional authentication for remote owner access remain production work. |
| Database and migrations | Working | Django migrations; SQLite is demo-only, PostgreSQL selected by `KFB_DATABASE_URL` for production. PostgreSQL receives an append-only trigger for audit events. |
| Patient registry and reception queue | Working | Search, duplicate warning, registration, visit start, emergency override. |
| Catalogue and versioned prices | Working core | Unknown prices block baskets. Product import never silently overwrites an existing code. |
| Invoices/payments/receipts | Working core | Posted lines, partial allocation model, cash and independently verified M-PESA, credit-note request/review, idempotency and printable receipt. Full refund payout UI remains incomplete. |
| Pharmacy and stock ledger | Working primary flows | Multi-line outpatient prescriptions, walk-in pricing, FEFO batches, expiry/quarantine block and immutable movements. Negative stock was reachable through repeated product lines on one order until audit C2; allocation now reserves across lines and is tested. Prescription replacement and ward issue/return screens remain incomplete. |
| Cashier shifts | Working | Opening float, cash-only expected drawer formula, close count and variance exception. Independent shift-review UI remains incomplete. |
| Owner reports and exceptions | Working core | Reconciled ledger totals, 1/7/30/90-day filters, watermarked KFBH PDF download and freshness display; no invented operating result. Detailed receivable ageing remains. |
| Independent approvals | Working | Purchase orders, credit notes and M-PESA verification reject self-approval and have complete reviewer interfaces. Ready eye cases have an accountable completion action. |

The walk-in sale and outpatient registration/note/service-request/payment/dispense paths use persistent records; they are not dashboard simulations.

## Stage 2

| Area | Status | Evidence / boundary |
|---|---|---|
| Clinical chart | Working core | Locked note numbering prevents concurrent version collisions, prescriptions accept multiple items, protected attachments are authenticated, and access-record PDFs are available. Replacement and amendment UI remain. |
| Laboratory / imaging | Working core | Request, in-progress/review/release states and authored results. The requester cannot release their own result. Amendments and charge automation remain. |
| Inpatient | Foundation + admission workflow | Configurable beds, admission, transfers, observations, medication administrations, handover and separate discharge statuses are modelled. Detailed nurse/transfer/discharge screens remain. |
| Maternity / dental / theatre | Data foundation | Accountable records are modelled; clinician-reviewed templates and full guided screens remain. No clinical thresholds or advice are invented. |
| Eye clinic | Working core | Waiting list/session display, unique patients vs eyes, package price snapshot, completion service and one case payable. Package-inclusion configuration and full session edit screens remain. |
| Purchasing / delivery | Working request + approval | Multi-line supplier requests, independent approval and requester segregation. Partial-delivery and invoice-match screens remain. |
| Stock reconciliation | Data foundation | Snapshot/count lines and reviewer fields exist. Guided blind-count and adjustment screens remain. |
| CSV migration | Working | Products/prices, patients, witnessed opening stock and reviewed opening receivables support validation, dry-run, row errors, content-hash idempotency and one-time commit. |
| Backups | Working command | SQLite/PostgreSQL data plus media, manifest, checksum, production encryption gate. Restore rehearsal must be performed on the chosen host. |
| Downtime | Working forms, partial reconciliation | Four printable forms and unique paper-reference model. Guided back-entry UI remains. |

## Stage 3

Not falsely claimed complete. The code exposes explicit configuration and adapter boundaries, but these external prerequisites are absent:

- private authenticated remote owner access and verified data-freshness transport;
- live M-PESA account capability, credentials, reachable callback infrastructure and reconciliation evidence;
- production PostgreSQL/service account, LAN firewall and device configuration; a Caddy internal-TLS baseline is supplied but still requires host-specific DNS and certificate deployment;
- verified encrypted backup destination, owner-held recovery key and a documented restore rehearsal;
- supervised pilot, role assignments, prices, bed register, package inclusions and clinical template sign-off.

## Acceptance checks executed

The suite is 38 tests. Automated tests cover the audit regressions plus PDF exports, protected attachments, multi-line forms, reviewer interfaces, requester segregation and all four CSV import paths. Overall application coverage is 84%; `views.py` is 73%, clearing the audit's 70% target. Desktop and 390 x 844 phone layouts were inspected in the live local browser during delivery; the phone document width stayed within its viewport and the browser console remained clean.
