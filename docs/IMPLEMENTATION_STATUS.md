# Implementation status

Updated: 18 September 2026. All included data is fictional.

## Stage 1

| Area | Status | Evidence / boundary |
|---|---|---|
| Design system and responsive shell | Working | Role navigation, desktop/mobile layout, print styles, local assets only. |
| Authentication and permissions | Working core | Named Django accounts, server-side role decorators, session expiry, lock screen. Additional authentication for remote owner access remains production work. |
| Database and migrations | Working | Django migrations; SQLite is demo-only, PostgreSQL selected by `KFB_DATABASE_URL` for production. |
| Patient registry and reception queue | Working | Search, duplicate warning, registration, visit start, emergency override. |
| Catalogue and versioned prices | Working core | Unknown prices block baskets. Product import never silently overwrites an existing code. |
| Invoices/payments/receipts | Working core | Posted lines, partial allocation model, cash and unverified M-PESA, idempotency, printable receipt. Credit/refund records exist; full refund payout UI remains incomplete. |
| Pharmacy and stock ledger | Working primary flows | Walk-in and signed outpatient prescription end-to-end workflows, FEFO batches, expiry/quarantine block, no negative stock, immutable movements. Multi-line prescription editing/replacement and ward issue/return screens remain incomplete. |
| Cashier shifts | Working | Opening float, cash-only expected drawer formula, close count and variance exception. Independent shift-review UI remains incomplete. |
| Owner reports and exceptions | Working core | Reconciled ledger totals and freshness display; no invented operating result. More date ranges/exports/ageing detail remain. |
| Independent approvals | Working core | Purchase and credit-note service rules reject self-approval. Purchase approval UI is included. |

The walk-in sale and outpatient registration/note/service-request/payment/dispense paths use persistent records; they are not dashboard simulations.

## Stage 2

| Area | Status | Evidence / boundary |
|---|---|---|
| Clinical chart | Working core | Draft/sign/version model, role-protected chart, and signed single-item prescription entry. Multi-item editing/replacement, attachment UI and amendment UI remain. |
| Laboratory / imaging | Working core | Request, in-progress/review/release states and authored results. Amendments and charge automation remain. |
| Inpatient | Foundation + admission workflow | Configurable beds, admission, transfers, observations, medication administrations, handover and separate discharge statuses are modelled. Detailed nurse/transfer/discharge screens remain. |
| Maternity / dental / theatre | Data foundation | Accountable records are modelled; clinician-reviewed templates and full guided screens remain. No clinical thresholds or advice are invented. |
| Eye clinic | Working core | Waiting list/session display, unique patients vs eyes, package price snapshot, completion service and one case payable. Package-inclusion configuration and full session edit screens remain. |
| Purchasing / delivery | Working request + approval | Supplier/order/line/receipt/check models and requester segregation. Partial-delivery and invoice-match screens remain. |
| Stock reconciliation | Data foundation | Snapshot/count lines and reviewer fields exist. Guided blind-count and adjustment screens remain. |
| CSV migration | Working product slice | Upload, validation, dry-run, row errors, content-hash idempotency and commit. Patient/opening-stock/opening-receivable templates are supplied but their import handlers remain. |
| Backups | Working command | SQLite/PostgreSQL data plus media, manifest, checksum, production encryption gate. Restore rehearsal must be performed on the chosen host. |
| Downtime | Working forms, partial reconciliation | Four printable forms and unique paper-reference model. Guided back-entry UI remains. |

## Stage 3

Not falsely claimed complete. The code exposes explicit configuration and adapter boundaries, but these external prerequisites are absent:

- private authenticated remote owner access and verified data-freshness transport;
- live M-PESA account capability, credentials, reachable callback infrastructure and reconciliation evidence;
- production PostgreSQL/HTTPS/service account, LAN firewall and device configuration;
- verified encrypted backup destination, owner-held recovery key and a documented restore rehearsal;
- supervised pilot, role assignments, prices, bed register, package inclusions and clinical template sign-off.

## Acceptance checks executed

Automated tests cover the core of scenarios 1–9, 12 and 14, including clinician prescription → pharmacy pricing → reception payment → actual dispense. Scenarios 11 and 15–20 need further workflow screens, infrastructure or supervised operational testing. Desktop and 390 × 844 phone layouts were inspected in the live local browser during delivery; the phone document width remained within its viewport.
