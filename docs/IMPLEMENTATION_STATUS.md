# Implementation status

Updated: 19 September 2026. All included data is fictional.

Corrections follow the engineering audit in `AUDIT_2026-09-18.md`, which found three rows claiming behaviour the code did not have. The defects are fixed and covered by regression tests; the wording is corrected here so the table matches the software.

The September 2026 stock-control work closed the largest remaining gap: stock could previously only ever decrease. Dispensing deducted from the ledger and nothing but the seed command or a shell could put any back, so the balance drifted away from the shelf the moment the first delivery arrived. Receiving, supplier-invoice evidence, stock counts and stock statistics are now application workflows.

## Stage 1

| Area | Status | Evidence / boundary |
|---|---|---|
| Design system and responsive shell | Working | Role navigation, desktop/mobile layout, print styles, local assets only. The owner and reviewer could not reach the stock ledger until September 2026 although the view already permitted them, and the dashboard computed a low-stock list it never rendered; both are corrected. |
| Authentication and permissions | Working core | Named Django accounts, server-side role decorators, session expiry, enforced lock screen and per-username sign-in throttling. The lock screen was a no-op until the September 2026 audit (audit C1); enforcement now runs in `process_view` and is tested. Second factor and additional authentication for remote owner access remain production work. |
| Database and migrations | Working | Django migrations; SQLite is demo-only, PostgreSQL selected by `KFB_DATABASE_URL` for production. |
| Patient registry and reception queue | Working | Search, duplicate warning, registration, visit start, emergency override. |
| Catalogue and versioned prices | Working core | Unknown prices block baskets. Product import never silently overwrites an existing code. |
| Invoices/payments/receipts | Working core | Posted lines, partial allocation model, cash and unverified M-PESA, idempotency, printable receipt. Credit/refund records exist; full refund payout UI remains incomplete. |
| Pharmacy and stock ledger | Working primary flows | Walk-in and signed outpatient prescription end-to-end workflows, FEFO batches, expiry/quarantine block, immutable movements. Negative stock was reachable through repeated product lines on one order until the September 2026 audit (audit C2); allocation now reserves across lines and is tested. Stock now moves in both directions: deliveries post receipt movements and approved counts post adjustments. Multi-line prescription editing/replacement and ward issue/return screens remain incomplete. |
| Goods receiving and supplier invoice evidence | Working | Delivery entry against an approved order, mandatory supplier-invoice photograph or scan, batch/expiry/actual cost per line, receipt movements posted in one transaction, partial deliveries, per-order invoice uniqueness, and an independent check that the receiver cannot perform. Expired batches are refused; quantity, unit-cost and invoice-total differences are recorded and raised as exceptions rather than blocked, so the ledger follows what physically arrived. Supplier payable and payment recording remain incomplete. |
| Stock counts and adjustments | Working | Count sheets freeze every batch balance at a cutoff by event time, support a blind count, and are submitted for independent review. Approval is the only thing that posts an adjustment movement; rejection changes no balance. There is still no editable current-quantity field anywhere. Guided cycle-count scheduling and write-off disposition screens remain. |
| Stock statistics | Working core | Valuation of sellable stock at batch purchase cost and at approved selling price, with expired and quarantined value reported separately rather than counted as an asset, products at or below reorder level, near-expiry/expired/quarantined batches, period receipts, units dispensed, cost of goods dispensed, product sales and gross margin, and most-dispensed products. Retail valuation states how many held products carry no approved price rather than understating the total silently. Stock discrepancy comes only from an approved count, never from an estimate. |
| Cashier shifts | Working | Opening float, cash-only expected drawer formula, close count and variance exception. Independent shift-review UI remains incomplete. |
| Owner reports and exceptions | Working core | Reconciled ledger totals, stock valuation and shortage position, and freshness display; no invented operating result. The PDF export carries the same stock figures. More date ranges/exports/ageing detail remain. |
| Independent approvals | Working core; credit note has no interface | Purchase and credit-note service rules reject self-approval, and purchase approval has a UI. `approve_credit_note`, `verify_mpesa` and `complete_eye_case` are implemented and tested but have no view or URL — they are reachable only from a shell (audit R3). |

The walk-in sale and outpatient registration/note/service-request/payment/dispense paths use persistent records; they are not dashboard simulations.

## Stage 2

| Area | Status | Evidence / boundary |
|---|---|---|
| Clinical chart | Working core | Draft/sign/version model, role-protected chart, and signed single-item prescription entry. Multi-item editing/replacement, attachment UI and amendment UI remain. |
| Laboratory / imaging | Working core | Request, in-progress/review/release states and authored results. Amendments and charge automation remain. |
| Inpatient | Foundation + admission workflow | Configurable beds, admission, transfers, observations, medication administrations, handover and separate discharge statuses are modelled. Detailed nurse/transfer/discharge screens remain. |
| Maternity / dental / theatre | Data foundation | Accountable records are modelled; clinician-reviewed templates and full guided screens remain. No clinical thresholds or advice are invented. |
| Eye clinic | Working core | Waiting list/session display, unique patients vs eyes, package price snapshot, completion service and one case payable. Package-inclusion configuration and full session edit screens remain. |
| Purchasing / delivery | Working request, approval and delivery | Supplier/order/line/receipt/check models, requester segregation, delivery entry with invoice evidence, partial deliveries and invoice matching against goods counted in. Supplier payable/payment bookkeeping and returns to supplier remain. |
| Stock reconciliation | Working | Snapshot at cutoff, blind count option, movement-aware expected quantities, reviewer segregation and approved adjustment movements are implemented and tested. Cycle-count scheduling and returned-stock disposition remain. |
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

| Ward and departmental custody | Working | Pharmacy issues named batches to a named department and person, patient-specific or general. Units leave the pharmacy balance as a transfer and stay outstanding until administered, returned or wasted. Administration posts no second deduction. Returns re-enter quarantined. Waste is raised for review. Hospital stock reconciles as pharmacy balance plus outstanding custody, which is covered by a test. Per-location balance queries and theatre pack kitting remain. |
| Write-offs and disposition | Working | Expired, damaged, contaminated or recalled stock is proposed with a narrative and removed only on a different person's approval, which posts the adjustment. Rejection changes nothing. A reviewer can release a quarantined batch or hold an active one with a recorded reason; expired stock can never be released back to sellable. Disposal evidence and destruction certificates remain. |
| Stock intelligence | Working core | Unexplained loss from approved count variances, valued and expressed against cost of goods, kept separate from authorised write-offs. Supplier unit-cost movement between consecutive deliveries. Control adoption: evidence, independent checking, prompt checking, counts approved, open custody, pending write-offs. Every figure reports as unavailable rather than zero when there is nothing to measure. Trend charts over time remain. |
| Owner brief | Working | A ranked in-app page of what needs the owner today, each line linking to the records behind it. No external messaging, which the specification forbids. |

## Known gaps that remain in stock handling

These are modelled but still have no screen, and are not claimed as working:

- supplier payables, supplier payments and returns to supplier;
- opening-stock and opening-receivable CSV import handlers, whose templates are supplied;
- stock take scheduling and automated near-expiry exception sweeps between deliveries;
- disposal evidence for approved write-offs, and destruction certificates;
- per-location stock balance queries; custody is tracked per issue rather than as a location ledger.

## Continuous integration

The hosted workflow has never completed a run. Every GitHub Actions run in this repository, including those predating the stock-control work, fails within one to five seconds with no runner assigned and empty check output, because the account is billing-locked. No commit can change that; the billing hold has to be cleared by a repository admin.

Until it is, `scripts/checks.sh` runs exactly what the workflow runs — ruff, the test suite, a migrations-match-models check and Django's deployment check — and a test fails if the script and the workflow drift apart.

## Acceptance checks executed

The suite is 93 tests (15 workflow, 13 audit and performance regression, 22 stock control and screens, 3 demo seed, 7 valuation correctness, 20 custody and write-off, 10 intelligence and brief, 3 continuous integration). Automated tests cover the core of scenarios 1–9, 12 and 14, including clinician prescription → pharmacy pricing → reception payment → actual dispense, and now scenario 3 end to end through a recorded delivery. Scenarios 11 and 15–20 need further workflow screens, infrastructure or supervised operational testing. Desktop and 390 × 844 phone layouts were inspected in the live local browser during delivery; the phone document width remained within its viewport.

`seed_demo` aborted partway through on every run before 19 September 2026: it set each role on a freshly fetched `StaffProfile` while the `User` object it kept in memory still held the default-role profile the `post_save` signal had attached, so the first role check read the stale copy and refused. The documented quick start therefore could not create a demo environment at all. The cached relation is now kept in step and the command is covered by regression tests.
