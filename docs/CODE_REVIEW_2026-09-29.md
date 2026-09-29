# Code review — 29 September 2026

## 1. Executive Summary

The codebase has a sound server-rendered structure, role checks on most routes, ledger-backed stock, and useful workflow tests; Ruff, all 148 Django tests, and the migration check pass. The largest risks are gaps between the guarded UI and alternate paths: reception can export clinical notes, a production backup can leave a plaintext archive, and concurrent balance-changing actions can duplicate or overstate records. Stock counts have two independent ways to post incorrect adjustments, while several clinical and finance flows never reach a reliable terminal state. Performance is generally reasonable for a small demonstration but several owner, stock, and order pages scale with full historical data or one query per displayed row; some polished UI controls also report false state or lead to inaccessible actions.

**Scope and validation.** Reviewed all 125 tracked files across Django models, services, views, forms, analytics, reports, migrations, tests, templates, assets, scripts, deployment, and documentation. Ran `ruff check .` (pass), `manage.py test hospital` (148 pass), `makemigrations --check --dry-run` (no changes), and `manage.py check --deploy` in the default demo environment (six expected demo-mode security warnings). The existing suite does not establish the PostgreSQL concurrency and production backup behaviors described below; those findings follow directly from the transaction boundaries and file operations. Priorities use the requested definitions: **P0** data loss, security breach, or production crash; **P1** likely bug or meaningful UX/performance degradation; **P2** compounding technical debt or inconsistent patterns; **P3** polish and minor developer experience.

## 2. Prioritised Action Items

### P0 — data integrity or confidentiality

#### P0.1 — Reception can export clinical notes it cannot view

**Affected:** `hospital/views.py:347-360,397-411`; `hospital/pdf_reports.py:263-346`; `hospital/urls.py:15`.

**Problem:** `patient_detail` withholds notes from reception, but `patient_access_pdf` admits reception and `build_patient_access_pdf` includes signed assessments and plans. This bypasses the intended clinical boundary and the specification's requirement that a cashier cannot retrieve clinical-note contents.

**Fix prompt:** In `hospital/views.py` and `hospital/pdf_reports.py`, make the patient access PDF obey the same clinical authorization as the chart. A reception user currently receives a PDF containing signed notes although the patient page hides them. Restrict the full export to clinical/owner roles, or implement an explicitly limited reception export with a separate route and named approval flow. Add a role test that creates a signed note, verifies reception cannot obtain its text through any export, and verifies authorized access remains audited.

#### P0.2 — Production backup stages plaintext patient data in the destination

**Affected:** `hospital/management/commands/backup_kfb.py:22-30,59-85`.

**Problem:** The command writes a ZIP containing the database and attachments directly to the configured backup directory before finding `age` or encrypting it. Missing `age`, an invalid recipient, or encryption failure leaves that plaintext archive and checksum behind; a successful run still exposes plaintext on the backup medium while it runs.

**Fix prompt:** Refactor `hospital/management/commands/backup_kfb.py` so production validates `age` and the recipient first, builds the plaintext archive only in a restricted temporary directory, encrypts to a temporary destination file, verifies the encrypted output, and atomically publishes the encrypted archive plus checksum. Ensure all failure paths remove partial and plaintext files. Test missing executable, invalid recipient, encryption failure, and success while asserting no plaintext ZIP ever appears in the destination.

#### P0.3 — Concurrent or stale CSV commits can duplicate patient and receivable identities

**Affected:** `hospital/views.py:1317-1435,1438-1525`; `hospital/models.py:122-125,366-369,1093-1103`.

**Problem:** Commit checks the `ImportJob` state before entering `transaction.atomic()` and does not lock the job or revalidate its rows. `Patient.external_reference` and `Invoice.external_reference` have indexes but no uniqueness constraint. Two commits of the same validated job, or a commit after another upload changes the database, can create duplicate patients or opening receivables, splitting a chart or doubling a financial balance. Product CSV validation also omits an in-file duplicate-code check, so repeated codes pass dry run and fail with a server error at commit.

**Fix prompt:** In `hospital/views.py` and `hospital/models.py`, serialize CSV commits by locking the `ImportJob` inside the transaction, checking its state after the lock, and revalidating all cross-record references immediately before writes. Add database uniqueness for nonblank patient and invoice external references after safely reconciling existing duplicates, and check duplicate product codes within a CSV during dry run. Return row-level validation errors rather than an `IntegrityError` page. Test two concurrent commits, a stale preview, and duplicate codes within one file on PostgreSQL.

#### P0.4 — Stock movements during a count are posted twice as discrepancies

**Affected:** `hospital/services.py:571-619,623-703`; `hospital/models.py:927-941`; `hospital/views.py:950-980`.

**Problem:** Opening a sheet freezes the ledger balance at `cutoff_at`, but the physical quantity is entered later. `StockCountLine.variance` subtracts the old expected balance, and approval posts that difference without accounting for legitimate movements between cutoff and observation. For example, expected 10, two units dispensed, then eight counted results in an extra −2 adjustment and a false balance of six. The current test checks that the frozen number remains unchanged, not that subsequent approval reconciles correctly.

**Fix prompt:** Correct the stock-count lifecycle in `hospital/services.py`, `hospital/models.py`, and `hospital/views.py` so physical and ledger quantities refer to the same time. Either suspend and enforce movement isolation during a count or record physical observation times and reconcile all intervening movements before variance approval. Preserve the original cutoff as evidence and reject an unreconcilable count. Add a PostgreSQL test for a dispense and a receipt between sheet opening and physical counting; neither should create a spurious adjustment.

#### P0.5 — A free-text count location can alter the pharmacy ledger with a ward count

**Affected:** `hospital/forms.py:420-430`; `hospital/services.py:571-612,664-679`; `hospital/models.py:319-352`.

**Problem:** Staff may enter any counting location, but expected quantities are the sum of all batch `quantity_delta` values, which represent pharmacy shelf balance after custody transfers. Reviewing a sheet labelled for another location posts its variance to the same batch ledger and falsely changes stock. The `from_location`/`to_location` strings on adjustment movements do not make the underlying balance location aware.

**Fix prompt:** In `StockCountOpenForm` and `open_stock_count`, restrict counts to the Pharmacy location until the ledger can calculate a per-location balance; reject direct service calls with other locations. If ward counts are required, introduce location-aware balances and count only the selected location's movements. Test that a non-Pharmacy location cannot post an adjustment against pharmacy stock.

#### P0.6 — Concurrent credit reviews and payments can overcredit an invoice

**Affected:** `hospital/services.py:175-214,307-324`; `hospital/views.py:415-429,1092-1107`; `hospital/models.py:377-395`.

**Problem:** Payment locks the invoice row, but `approve_credit_note` locks only its note and reads `note.invoice.balance` without locking the invoice. Two pending credits, or one credit and one payment, can independently pass the same balance check and leave a negative balance or a paid invoice with excess credit.

**Fix prompt:** Serialize every invoice balance mutation in `hospital/services.py` on the same `Invoice.select_for_update()` row, including credit approval and payment. Recompute the balance after acquiring the lock and reject overpayment or overcredit before saving. Add PostgreSQL transaction tests where two credits and a credit plus a payment race against a KES 100 balance.

### P1 — likely bugs or meaningful UX/performance degradation

#### P1.1 — Unverified M-PESA makes invoices look paid before reconciliation

**Affected:** `hospital/services.py:175-241`; `hospital/models.py:377-395,414-485`; `templates/hospital/payment_form.html`; `templates/hospital/reports.html`.

**Problem:** Recording an M-PESA reference immediately creates a valid allocation, so `paid_amount`, `balance`, and `refresh_status` treat it as settled even while reports call it unverified. The order is not cleared for dispensing until review, but receivables and invoice status can already show zero/paid, and no rejection flow exists for an invalid reference.

**Fix prompt:** In the payment models/services and related templates, represent an unverified M-PESA record as pending settlement. Keep its amount visible separately without reducing verified paid amount or receivables; apply the allocation and clear the order only after independent verification, and provide a documented rejection/reversal path. Test reporting, invoice status, balance, duplicate reference, and later verification or rejection.

#### P1.2 — Payment retry is not safely idempotent

**Affected:** `hospital/services.py:175-214`; `hospital/views.py:630-650`.

**Problem:** `record_payment` checks the now-reduced invoice balance before looking up an existing idempotency key, so a successful retry can fail. If the same key is reused on a different invoice and passes earlier checks, the `IntegrityError` handler returns the first receipt without verifying invoice, amount, method, or reference.

**Fix prompt:** In `record_payment`, look up the idempotency key before new-payment validation, confirm that any existing payment matches the original invoice and all immutable request fields, and return that receipt only for an exact retry. Reject key reuse for different requests with a clear error. Keep the invoice row lock for new writes and test retries after full payment and cross-invoice key reuse.

#### P1.3 — Draft note collision check cannot detect a later edit

**Affected:** `hospital/views.py:473-515`; `hospital/models.py:203-232`; `templates/hospital/clinical_note_form.html`.

**Problem:** Two tabs can open the same draft with the same hidden `expected_version`. Saving a draft never increments `version`, so the second tab passes the equality check and silently overwrites the first tab's changes despite the collision warning code.

**Fix prompt:** Change the draft edit token in `hospital/views.py` and `hospital/models.py` so it advances on every successful draft save, not only at note creation. Under the existing row lock, compare the submitted token to the current one and reject stale writes while preserving the user's submitted text. Test two-tab save order and signing after a stale edit.

#### P1.4 — Purchase orders and eye cases accept invalid terminal transitions

**Affected:** `hospital/services.py:327-353`; `hospital/models.py:669-689,709-737`.

**Problem:** `approve_purchase_order` has no requested-state check and can change a cancelled or received order back to approved. `complete_eye_case` checks readiness but not current status or payment, so a cancelled or already completed case can be completed and accrue a payable. Both conflicts matter to purchasing and clinical finance.

**Fix prompt:** Add explicit transition guards in `approve_purchase_order` and `complete_eye_case`: only a pending purchase request may be approved; only an eligible, active, clinically ready and financially cleared eye case may be completed, with a documented exception path if emergency care requires it. Make repeated completion idempotent without rewriting timestamps or payables. Add tests for cancelled, received, unpaid, and already completed records.

#### P1.5 — Reused batch numbers keep the first purchase cost forever

**Affected:** `hospital/services.py:450-475`; `hospital/models.py:271-313,319-352`; `hospital/analytics.py:214-279,339-395`.

**Problem:** A later receipt with the same item and batch number reuses `StockBatch` without changing `purchase_cost_per_base_unit`. Stock value, cost of goods, shrinkage, and margin then price later units at the first receipt's cost even when `GoodsReceiptLine.actual_unit_cost` differs.

**Fix prompt:** Choose and document a consistent batch costing method in `hospital/models.py`, `hospital/services.py`, and `hospital/analytics.py`. Preserve each receipt's actual cost and associate outward units with the applicable cost layer or apply an explicit weighted-average calculation; never silently retain the first price for later receipts. Test two deliveries of one batch at different costs followed by partial dispense, valuation, and margin reporting.

#### P1.6 — “Net billed” KPI does not subtract approved credits

**Affected:** `hospital/views.py:145-164,268-302,1033-1050`; `templates/hospital/dashboard.html:12`; `templates/hospital/reports.html:5`; `hospital/pdf_reports.py:142-147`.

**Problem:** Both dashboard and owner report use `invoiced_total`, which sums posted invoice lines only, yet label it net billed. The build specification defines net billed as posted charges minus approved credit notes in the period, so the displayed number overstates charges after corrections.

**Fix prompt:** Implement one shared date-consistent net-billed query for the dashboard, HTML report, and PDF in `hospital/views.py`/`hospital/pdf_reports.py`. Subtract approved credit notes on their effective approval date from posted charges in the selected interval, state the period basis in the UI, and add an invoice-plus-credit regression test.

#### P1.7 — Clinical work has no close, discharge, or charge completion path

**Affected:** `hospital/views.py:268-302,461-469,1142-1187`; `hospital/models.py:159-188,602-616,646-660`; `templates/hospital/queue.html`; `templates/hospital/wards.html`.

**Problem:** No application writer sets an encounter to `CLOSED` or discharges an admission, so visits stay on the queue and beds stay occupied indefinitely. Releasing a service order does not create an invoice line; the only normal invoice creation is pharmacy preparation. These are visible flows that staff cannot finish or reconcile.

**Fix prompt:** Add role-authorized encounter closure and admission discharge actions with audit events, separate clinical discharge from debt, and update queue/ward screens immediately. Link released service orders to versioned catalogue prices and a posted charge exactly once, with a documented emergency/credit exception where appropriate. Test the complete outpatient and admission flow, repeat submissions, billing, and role denials.

#### P1.8 — Lab result release and signed-note amendment states are not enforced

**Affected:** `hospital/views.py:473-515,1148-1164`; `hospital/models.py:203-232,646-660`; `hospital/forms.py:188-210`.

**Problem:** A service order can jump directly from requested to released without review, and the requester-only prohibition is the sole release gate. The note model defines amended notes and parent linkage, but no UI writer uses them, so signed notes cannot be amended with traceable lineage. Signing a note also forces the encounter to `PHARMACY` even when requested tests remain outstanding (`hospital/views.py:503-507,550-558`).

**Fix prompt:** Define allowed service-order transitions and a separate reviewer/release action in `hospital/views.py` and `hospital/services.py`, retaining independent requester segregation. Add an amendment workflow that creates a new linked `ClinicalNote` instead of editing a signed one, populating `parent_note` and the amended state. Derive encounter work state from pending tests instead of overwriting it on note signing. Test direct-state jumps, reviewer identity, immutable originals, linked amendments, and signing with an outstanding test.

#### P1.9 — Cash shift and refund reconciliation lack transaction controls

**Affected:** `hospital/models.py:503-531,1039-1057`; `hospital/forms.py:168-180`; `hospital/views.py:672-693`; `templates/hospital/shift_form.html`.

**Problem:** The `Refund` model has no application route or service writer, yet a cashier can enter `cash_refunds` directly on the shift form. Expected cash can therefore be lowered without an attributable, reviewed refund tied to a payment. The form says “submit for review”, but no writer sets `CashShift.Status.REVIEWED` or its reviewer; concurrent opening requests also lack a database constraint against two open shifts for one cashier.

**Fix prompt:** Implement an authorized, reviewed refund workflow for `Refund` in `hospital/models.py` and related services/views, linked to the original payment and shift. Derive `CashShift.cash_refunds` from valid refund records instead of editable input; add an independent shift-review action and a partial unique constraint allowing only one open shift per cashier. Test duplicate open requests, review segregation, refund reversal, and expected-cash reconciliation.

#### P1.10 — Mutable admin configuration bypasses workflow audit

**Affected:** `hospital/admin.py:12-63`; `hospital/models.py:89-109,233-270,702-737`; `hospital/services.py:107-121`.

**Problem:** Admin can change roles, operational settings, approved price history, and supplier details without `services.audit` or independent review. The fields `second_factor_required` and `require_password_change` are also stored/displayed without any enforcement, implying protections the sign-in flow does not provide.

**Fix prompt:** Narrow or replace the mutable admin registrations in `hospital/admin.py`. Audit all role and threshold changes with before/after values and actor; make approved historical prices immutable and route new prices through version creation; add independent review for sensitive supplier changes. Either enforce the password-change/second-factor fields in authentication or remove/hide them until implemented. Add admin-path tests that cannot bypass these controls.

#### P1.11 — Unlock screen bypasses the login lockout

**Affected:** `hospital/views.py:1551-1596`; `hospital/models.py:1155-1192`; `hospital/signals.py:18-40`.

**Problem:** `ThrottledLoginView.post` checks `LoginAttempt.is_locked` before `authenticate`; `screen_unlock` authenticates repeatedly without that check. Anyone with a locked but still authenticated workstation session can make unlimited password attempts through the unlock form.

**Fix prompt:** Apply the same per-account and per-address lockout to `screen_unlock` in `hospital/views.py`, including a 429 response and wait message. Keep failure evidence and successful reset behavior consistent with login; test that repeated unlock failures trigger a lock and a correct password does not bypass it until the window expires.

#### P1.12 — Readiness command exits successfully on failed prerequisites

**Affected:** `hospital/management/commands/check_readiness.py:13-45`; `kfb_hms/settings.py:129-143`; `README.md` production steps.

**Problem:** The command prints `NEEDS ACTION` but returns exit code zero. It also accepts an empty `Setting` table as fully confirmed and treats any file in `STATIC_ROOT` as a complete collection. `WHITENOISE_MANIFEST_STRICT=False` then falls back to an unhashed asset path when a referenced production asset is absent, hiding an incomplete deployment until the browser asks for the file. A service script can therefore interpret a failed check as a successful deployment gate.

**Fix prompt:** Make `check_readiness` raise `CommandError` or otherwise exit nonzero when a required check fails. Validate the explicit required operational setting keys and every referenced static manifest asset, not just row or directory existence; fail production deployment for an incomplete manifest rather than silently serving a fallback URL. Keep actionable output and test demo, empty settings, partial collectstatic, and each failed production prerequisite.

#### P1.13 — Production-specific database behavior is untested in CI

**Affected:** `.github/workflows/quality.yml:8-32`; `hospital/migrations/0004_protect_audit_events_postgresql.py`; `hospital/tests.py`.

**Problem:** CI runs only SQLite/demo, while production uses PostgreSQL. The append-only audit trigger, `select_for_update()` concurrency, and database-specific migrations receive no automated production-backend verification; passing local tests therefore misses the highest-risk races in this report.

**Fix prompt:** Add a PostgreSQL service/job to `.github/workflows/quality.yml`, run all migrations and a focused concurrency/permission/ledger suite against it, and assert the audit trigger rejects update/delete at the database level. Keep SQLite demo checks for fast local feedback and document how to reproduce the PostgreSQL job locally.

#### P1.14 — Confirmation cancellation disables irreversible-action buttons

**Affected:** `static/js/app.js:61-73,363-373`; forms using `data-confirm` in `templates/hospital/reports.html`, `stock_count_detail.html`, `write_offs.html`, and `pharmacy_order_detail.html`.

**Problem:** The confirmation listener calls `preventDefault()` when Cancel is chosen, but the other submit listener still schedules the clicked button to be disabled and relabelled “Saving...”. Staff cannot submit that decision again until reloading, and a reload can discard form context.

**Fix prompt:** In `static/js/app.js`, make `data-confirm` handling and submit-button disabling one coordinated submit path. A cancelled confirmation must leave every button enabled and the form untouched; a confirmed submission must preserve the clicked button's name/value and disable only after a real submit. Test Cancel then Approve/Reject on a two-button decision form and Cancel then Confirm on a dispense form.

#### P1.15 — Stock-count validation discards a long physical count

**Affected:** `hospital/views.py:950-989`; `templates/hospital/stock_count_detail.html:15-41`.

**Problem:** On one blank/invalid quantity or a service validation error, the view re-renders the sheet, but quantity and reason inputs are built from stored line values rather than `POST`. Every typed quantity is lost and staff must repeat the whole count.

**Fix prompt:** In `stock_count_detail`, bind submitted counted quantities and reasons back into the form on validation failure, identify errors beside the affected batch row, and preserve all other entered values. Keep the frozen expected values untouched until a successful atomic submit. Test a multi-line count where the final line is invalid and the earlier values remain visible.

#### P1.16 — Dashboard exposes patient queue data and links across role boundaries

**Affected:** `hospital/views.py:268-302`; `templates/hospital/dashboard.html:42-50`; `hospital/permissions.py:11-36,50-75`.

**Problem:** Every authenticated role receives the open queue and its patient names, including roles that cannot open patient detail; several dashboard task and patient links then lead to 403 pages. Eye/reviewer users are pointed at queue, while lab/eye/reviewer users can be pointed at wards despite their route permissions.

**Fix prompt:** Build dashboard context and task cards in `hospital/views.py`/`templates/hospital/dashboard.html` from the caller's permitted workflow. Do not query or render patient names for roles without that chart scope. Point eye to eye clinic, lab to departments, reviewer to review queues, and nurses to wards as appropriate. Add role-matrix tests asserting visible links resolve and restricted names are absent from HTML.

#### P1.17 — Ward cards use historical admissions as present occupancy

**Affected:** `hospital/views.py:1167-1170`; `templates/hospital/wards.html:2`.

**Problem:** The ward page prefetches every bed admission and marks a bed occupied when it has any historical admission. After discharge is implemented, a bed with only discharged admissions will still appear occupied or blank rather than available.

**Fix prompt:** In `hospital/views.py`, prefetch only admissions with `discharged_at__isnull=True` into an explicit current-occupancy attribute, and render each bed from that state in `templates/hospital/wards.html`. Preserve history in a separate detail view if needed. Test a bed with one discharged admission and one with a current admission.

#### P1.18 — Pending review queues hide older actionable records

**Affected:** `hospital/views.py:769-784,1017-1030`; `templates/hospital/deliveries.html:59-99`; `templates/hospital/reports.html:11-14`.

**Problem:** Unverified M-PESA and pending credits are sliced to 50, unchecked deliveries to 20, and delivery history to 50 without pagination or another complete route. Older pending items can become unreachable through normal review screens, while the displayed “awaiting review” count reflects only the slice.

**Fix prompt:** Paginate the pending-payment, pending-credit, and unchecked-delivery querysets in `hospital/views.py`, with filters and total counts that match all outstanding records. Update the templates to expose next/previous navigation and retain filters. Test more than each current slice limit and verify the oldest pending item remains reachable and reviewable.

#### P1.19 — Stock table sorting separates products from their batch details

**Affected:** `templates/hospital/stock.html:103-139`; `static/js/app.js:161-183`.

**Problem:** Each product row is followed by its batch-detail rows, but the generic sorter moves individual `<tr>` elements. Sorting any column can display another product's batches under the wrong product, which is misleading for stock decisions.

**Fix prompt:** Change the stock table markup and sorter so a product row and all its batch-detail rows move as one group, or disable client sorting for that grouped table. Keep accessible sorting behavior on ordinary flat tables. Add an interaction test with two products and multiple batches that verifies grouping after each sort direction.

#### P1.20 — Quarantined batch disposition has no normal UI entry point

**Affected:** `hospital/urls.py:44`; `hospital/views.py:1760-1777`; `hospital/services.py:955-970`; `templates/hospital/stock.html:194-202`.

**Problem:** The form and POST route to release or mark a batch exist, but no template links or posts to them; stock merely says it awaits an authorized disposition. Returned ward stock can remain quarantined indefinitely unless staff craft a request outside the UI.

**Fix prompt:** Add a role-appropriate disposition control to `templates/hospital/stock.html` or a batch-detail page, using `BatchDispositionForm`, showing current status and requiring the reason already enforced by `set_batch_disposition`. Preserve independent write-off flow for expired/damaged stock. Test that an authorized user can reach and submit the control through visible navigation and that unauthorized users cannot.

#### P1.21 — Receipt reprints can appear to be original receipts

**Affected:** `hospital/views.py:653-659`; `templates/hospital/receipt.html:2`.

**Problem:** “DUPLICATE RECEIPT” appears only if the URL manually includes `?reprint=1`. Reopening a previously issued receipt URL prints another unmarked “PAYMENT RECEIPT”, undermining the visible distinction promised by the receipt workflow.

**Fix prompt:** Derive original-versus-reprint status server-side in `hospital/views.py`, with an auditable original issuance marker and explicit reprint action. Make every later print of the same payment visibly duplicate while preserving the receipt number and amount. Test initial issue, direct URL reopen, and explicit reprint.

#### P1.22 — Connection and session UI report states the server has not confirmed

**Affected:** `templates/base.html:63,94-101`; `static/js/app.js:323-350`; `kfb_hms/settings.py:159-160`.

**Problem:** `navigator.onLine` only reports browser network state, yet the app says “Server connected”; the offline copy promises typed data will save when the link returns though no queue/retry exists. The session timer uses a fixed 480-minute value because no remaining-time value is supplied, and its HEAD “extend” handler hides the warning even after a failed request. During a server outage, the interface can reassure staff falsely.

**Fix prompt:** In `templates/base.html` and `static/js/app.js`, show server connectivity only after a successful authenticated lightweight check, display an honest failure/downtime message, and never promise automatic save without implementing it. Calculate session expiry from server-provided state and update the warning only after a successful extension response; handle failure visibly. Test offline browser, reachable network with server down, expired session, and failed extension.

#### P1.23 — Reporting and stock pages have row-count and history-dependent latency

**Affected:** `hospital/models.py:377-388`; `hospital/views.py:565-568,625-627,706-765,1033-1050`; `hospital/analytics.py:39-279,397-451`; `templates/hospital/pharmacy_orders.html:6`; `templates/hospital/reports.html:10`; `hospital/pdf_reports.py:179-189`.

**Problem:** Invoice `total`, `paid_amount`, and `balance` each issue aggregates when rendered per row; up to 100 pharmacy orders and 25 report invoices can produce hundreds of queries. Stock pages load all active batches and price versions plus all movements in the selected window into Python on every render, and supplier history builds all entries before slicing to 12. These costs grow with patient/ledger history rather than page size.

**Fix prompt:** Add reusable annotated invoice totals/verified payments/credits for order and report lists in `hospital/views.py`, avoiding per-row property aggregates. Move stock-period totals and supplier top-N selection into grouped database queries or bounded cached summaries while keeping source-ledger traceability and freshness visible. Keep paginated detailed movements. Add query-count and representative-volume benchmarks for 100 orders, 25 invoices, and a multi-year ledger.

#### P1.24 — Manual verification can claim provider confirmation without provider evidence

**Affected:** `templates/hospital/reports.html:11`; `hospital/views.py:1070-1088`; `hospital/services.py:223-241`.

**Problem:** A reviewer can tick “Provider confirmed”, which writes `Payment.Verification.PROVIDER` using only a browser field; there is no authenticated provider event or evidence source. The resulting label asserts stronger verification than the workflow performs.

**Fix prompt:** Remove the `provider_confirmed` checkbox from the manual report review screen and record that action as manual verification with reviewer identity and evidence note. Reserve `PROVIDER` for a verified provider integration/event with authenticated provenance and idempotency. Test that a crafted POST cannot select provider confirmation through the manual endpoint.

#### P1.25 — Missing production environment variable silently starts demo mode

**Affected:** `kfb_hms/settings.py:6-16`; `scripts/start-server.ps1:6-18`; `.env.example`.

**Problem:** Unset `KFB_ENV` defaults to demo, including `DEBUG=True` and a known signing key. The production launcher also treats an unset value as demo, so a service-manager configuration omission can start a weak demo instance instead of failing closed.

**Fix prompt:** Require an explicit environment name in `scripts/start-server.ps1` and fail if it is absent or unrecognized; make production/service startup validate `KFB_ENV=production` before importing Django. Preserve an explicitly selected demo path for `run-demo` wrappers. Add startup tests for unset, demo, and production environments and update `.env.example`/README accordingly.

### P2 — compounding debt and inconsistent patterns

#### P2.1 — CSV dry runs mix one-off validation, stale global reuse, and full-row storage

**Affected:** `hospital/views.py:1268-1548`; `hospital/forms.py:254-269`; `hospital/models.py:1093-1103`.

**Problem:** The four validators repeat decode/header/error handling but differ in row caps; only product import caps at 5,000. `ImportJob.get_or_create` globally keys by kind and file hash, so a second authorized uploader can be shown another user's stale dry run but cannot commit it because commit requires `created_by=request.user`. Every row is stored in the JSON report and validation performs per-row existence queries, which makes a 1 MB import expensive and hard to resume safely.

**Fix prompt:** Extract a shared bounded CSV parser/validator in `hospital/views.py` or a dedicated import service with consistent row limits, duplicate-in-file detection, batched reference lookups, and concise row errors. Scope dry-run ownership deliberately, expire/revalidate stale jobs, and store only the data needed for a safe commit. Test same-file uploads by two authorized users, changed database state after preview, and maximum-row imports.

#### P2.2 — Backup and restore do not share a single consistent recovery point

**Affected:** `hospital/management/commands/backup_kfb.py:30-67`; `scripts/restore-backup.ps1:26-30`; `docs/BACKUP_AND_RECOVERY.md`.

**Problem:** The database snapshot precedes a walk over live media, so an attachment can be added, removed, or changed between the two; the manifest claims a recovery point it cannot guarantee. Restoring an encrypted backup also leaves `decrypted-backup.zip` in the target alongside extracted patient data.

**Fix prompt:** Define and enforce a database/media consistency boundary for `backup_kfb` (quiesced attachment writes or immutable versioned objects with a verified manifest), then state the actual recovery point in `docs/BACKUP_AND_RECOVERY.md`. In `restore-backup.ps1`, decrypt only in a restricted temporary directory and remove the plaintext ZIP in a `finally` block. Test concurrent attachment activity and restore cleanup.

#### P2.3 — Administrative thresholds accept invalid decimal values

**Affected:** `hospital/services.py:92-105,399-400,449-520`; `hospital/admin.py:19-23`; `hospital/models.py:102-110`.

**Problem:** `setting_decimal` accepts `NaN`, infinity, and unreasonable negatives. Converting a malformed near-expiry value with `int()` can crash receiving; a negative cost or variance threshold can suppress or flood exceptions. Settings are mutable through admin with no typed validation.

**Fix prompt:** Define typed, finite, bounded validators per operational setting at the `Setting` save/admin boundary and in `setting_decimal` as defense in depth. Return a clear configuration error for invalid stored values and keep legitimate defaults only when the key is absent. Test NaN, infinity, negative, and overlarge values in receiving and variance checks.

#### P2.4 — Password-failure evidence grows without retention or an IP/time index

**Affected:** `hospital/models.py:1155-1192`; `hospital/signals.py:18-40`; `hospital/admin.py:78-83`.

**Problem:** Every failed login is retained forever. The lockout query by address and recent time has an `attempted_at` index but no combined IP/time index, so high-volume failures still scan/filter many recent rows; storage and admin review grow unbounded.

**Fix prompt:** Add a `(ip_address, attempted_at)` index and an explicit retention/export policy for `LoginAttempt`, preserving legally required evidence while pruning old operational rows on a documented schedule. Test account and address lockout with high recent volume and that retention never removes active-window evidence.

#### P2.5 — Several interactive filters and sorters present misleading results

**Affected:** `hospital/views.py:708-749`; `templates/hospital/stock.html:72-76`; `static/js/app.js:149-159,161-188,264-277`; `templates/hospital/audit_review.html`, `patient_list.html`, `deliveries.html`.

**Problem:** Stock search promises a batch match but filters the product table only by product name/code; a batch-only query can say nothing matches while matching movements appear below. The palette allows an older fetch to overwrite newer results. Client sorting compares displayed dates lexicographically and only the current page, so chronology and whole-list expectations can be wrong.

**Fix prompt:** Include batch number in the stock product filter in `hospital/views.py`; cancel or sequence palette requests in `static/js/app.js`; add ISO/epoch sort values for dates and clearly label page-only sorts or move sorting to the server with pagination. Test rapid typing, a batch-only search, cross-month date order, and sorting on page two.

#### P2.6 — Role-specific actions and anchors do not match reachable destinations

**Affected:** `templates/base.html:38-39`; `templates/hospital/patient_detail.html:3,6,8`; `templates/hospital/owner_brief.html:38`; `hospital/views.py:1599-1736,1796-1889`; `static/js/app.js:295-320`.

**Problem:** Reception gets separate Payments and Pharmacy navigation entries pointing to the same page. Patient tabs link to notes/billing anchors even when those sections are omitted. Owner/reviewer brief cards say “Start a count” or “Write it off” but land on pages where those roles cannot initiate the action; hard-coded keyboard destinations likewise lead some roles to 403.

**Fix prompt:** Derive navigation, brief actions, patient tabs, and keyboard shortcuts from the same role capability map in `hospital/permissions.py`. Give each visible action a reachable destination and accurate verb, hide anchors without sections, and remove duplicate navigation. Add role-by-role HTML/link tests for the brief and patient detail.

#### P2.7 — Forms and downtime copy promise safeguards the workflow lacks

**Affected:** `templates/hospital/payment_form.html`, `service_order_form.html`, `admission_form.html`, `shift_form.html`, `downtime_forms.html`; `static/js/app.js:50-59`; `hospital/models.py:1105-1113`.

**Problem:** Long/high-stakes forms omit the existing unsaved-change warning. “Numbered” downtime sheets show only blank paper-reference underscores, and `DowntimeEntry` has no back-entry route, so the copy implies a controlled reconciliation path not present in the app. These claims raise avoidable data-entry and duplicate-posting risk during outages.

**Fix prompt:** Apply the existing unsaved-warning behavior consistently to long forms, preserving POST validation state. Either issue controlled unique downtime references and implement an idempotent back-entry/reconciliation workflow for `DowntimeEntry`, or describe the sheets honestly as manually numbered paper forms with documented reconciliation steps. Test duplicate paper references and interrupted entry.

#### P2.8 — Dormant schema advertises incomplete clinical modules

**Affected:** `hospital/models.py:617-645,692-700,991-1057,1105-1113`; `hospital/admin.py:59-63`; `docs/IMPLEMENTATION_STATUS.md`.

**Problem:** `MedicationAdministration`, `NursingHandover`, `BedTransfer`, `TheatreCase`, `DentalRecord`, `MaternityRecord`, `NewbornLink`, `EyePackageItem`, `Refund`, and `DowntimeEntry` have no normal application writer/reader; an eye package can be configured in admin but is not applied to billing or stock. Some are useful future schema, but the implemented UI does not deliver those workflows. Deleting deployed tables outright could discard data, so this needs an explicit product and migration decision.

**Fix prompt:** Reconcile `hospital/models.py`, `hospital/admin.py`, and `docs/IMPLEMENTATION_STATUS.md` against actual reachable routes. For each dormant module, either implement the minimum end-to-end workflow with permissions and tests or mark it clearly as unavailable and remove unused admin controls; retire tables only through a reviewed data-preserving migration after checking live rows. Prioritize medication administration, refunds, and eye package charging because existing screens imply these records matter.

#### P2.9 — Small repeated queries and helper logic drift across workflows

**Affected:** `hospital/analytics.py:39-108,280-291,339-395,453-488`; `hospital/services.py:123-160,450-526,53-87`; `hospital/views.py:145-164,398-411`; `hospital/pdf_reports.py:263-346`.

**Problem:** `stock_position` fetches the active price map twice; prescription pricing selects a price per line despite the bulk map; delivery calculations revisit related receipt lines; and patient PDF iteration can bypass prefetch when ordering encounters. `raise_exception` increments `occurrence_count` with a read-modify-write pattern that can lose concurrent occurrences. These are inconsistent with nearby bulk aggregation and locking patterns.

**Fix prompt:** Share a current-price query across stock and prescription work, pass already fetched maps to analytics helpers, prefetch ordered PDF relations exactly as consumed, and aggregate delivery totals once per receipt. Change exception increments to an `F()` update or locked row. Add targeted query-budget and concurrent increment tests rather than mirroring every helper implementation.

#### P2.10 — Demo scripts and configuration drift from documented deployment

**Affected:** `scripts/run-demo.ps1:25-37`; `.github/workflows/quality.yml:18`; `requirements.txt`, `requirements.lock`, `requirements-dev.txt`; `.env.example:31`; `README.md:41,71`; `docs/HOST_READINESS.md:4`; `docs/PRODUCTION_PREREQUISITES.md:23`.

**Problem:** PowerShell's `$ErrorActionPreference='Stop'` does not fail on nonzero native process exit codes, so the demo wrapper can continue after failed pip/migration/seed steps. CI installs a different dependency path from demo, README alternates between the files, and the Python version requirements conflict. `KFB_BACKUP_RETENTION_DAYS` is advertised but never read.

**Fix prompt:** Check `$LASTEXITCODE` after every native command in `run-demo.ps1`, matching the `.cmd`/`.sh` wrappers. Choose one lock-file generation/install policy for demo and CI and align README and Python minimums. Implement safe encrypted-backup retention with explicit policy, or remove `KFB_BACKUP_RETENTION_DAYS` until it exists. Test each script's first failure path.

#### P2.11 — Several worklists eagerly load unlimited history

**Affected:** `hospital/views.py:347-360,461-469,1142-1145,1221-1227,1599-1615`; `templates/hospital/patient_detail.html`, `queue.html`, `departments.html`, `purchasing.html`, `custody.html`.

**Problem:** Patient detail loads full visit, note, attachment, and invoice history; queue, department work, purchasing, and custody screens have no pagination or bounded active/history split. Some prefetches pull full related rows even when a card needs only a count or latest status. As records accumulate, each request transfers and renders increasing amounts of patient or ledger data.

**Fix prompt:** Paginate historical lists and separate active from archived work in the listed `hospital/views.py` functions. Use `select_related`/`prefetch_related` only for fields actually rendered, aggregate counts in SQL, and keep latest detail links reachable. Preserve permission and audit behavior, and add representative query-count plus response-size checks for multi-year patient/worklist histories.

#### P2.12 — Workflow code is concentrated in files too large to change safely

**Affected:** `hospital/views.py` (about 1,890 lines); `hospital/services.py` (about 970); `hospital/models.py` (about 1,200); `hospital/tests.py` (about 2,000).

**Problem:** CSV parsing, clinical care, finance, stock, reporting, and authentication are interleaved in one views module, while tests cover all domains in one file. This raises the chance that a shared helper or role change affects an unrelated flow and makes ownership and navigation harder. Several behaviors are validated in views while comparable behaviors use service-layer transactions.

**Fix prompt:** After the correctness fixes above, split views/services/tests by domain (patients/clinical, billing, pharmacy/stock, purchasing/imports, reporting/auth) while retaining stable URLs and service interfaces. Co-locate validation with each transactional service, keep shared ledger/auth helpers small, and run the full suite plus a route-name snapshot to confirm no behavior drift. Avoid introducing a generic repository layer; preserve direct ORM queries where clear.

### P3 — polish and minor developer experience

#### P3.1 — Minor template and accessibility polish

**Affected:** `templates/hospital/csv_import.html:2`; `templates/hospital/patient_detail.html:3`; `static/js/app.js:172-188,279-321`; `static/css/app.css:422`.

**Problem:** CSV template links hard-code `/static/` rather than using Django's static tag. Sorting clears visual state but leaves stale `aria-sort` on another header. The search dialog has no focus trap and Escape works only while the input has focus. On a phone without JavaScript, wide tables can overflow because the CSS removes horizontal scrolling before JS can stack them.

**Fix prompt:** Use `{% static %}` for CSV template links; keep `aria-sort` synchronized when changing sort columns; trap/restore focus and handle Escape throughout the palette dialog; preserve horizontal scrolling as the no-JS mobile fallback. Check keyboard-only, narrow-screen, custom static-prefix, and no-JS use.

#### P3.2 — UI/CSS contains small obsolete hooks and repeated overrides

**Affected:** `static/js/app.js:260-262,307-309`; `templates/base.html:27`; `static/css/app.css:183-185,209,235-237,333-367,441-445`.

**Problem:** `highlight(0)` runs twice, `?` targets a shortcut-help element that does not exist, `data-sidebar` has no consumer, and several old CSS selectors or `[open]` palette state are unused. Repeated responsive/grid overrides make later style changes harder to reason about.

**Fix prompt:** Remove the confirmed unused hooks and selectors in `static/js/app.js`, `templates/base.html`, and `static/css/app.css`; consolidate responsive rules by component without changing rendered behavior. Verify sidebar, palette, tables, and mobile layouts in one desktop and one narrow viewport.

#### P3.3 — Source comments and historical artifacts need a final consistency pass

**Affected:** `hospital/tests.py:1210-1288`; `.vscode/settings.json`; `output/pdf/KFBH-owner-report-sample.pdf`; `scripts/stop-server.ps1`; `docs/IMPLEMENTATION_STATUS.md`.

**Problem:** Script-sync tests check substrings rather than executable equivalence, VS Code settings point to Conda although the project uses `.venv`, and an unreferenced generated PDF and instruction-only stop script add noise. The implementation-status text claims some safeguards more broadly than current code delivers.

**Fix prompt:** Replace brittle text-substring script checks with behavior-focused script tests where practical, remove or update obsolete workspace artifacts, and align `docs/IMPLEMENTATION_STATUS.md` claims with the completed fixes and known gaps. Keep historical audit documents as historical evidence.

## 3. Quick Wins

The following small changes can be batched after the P0/P1 fixes that touch the same files. They do not require schema/data migration: remove unused `ZERO_MONEY`/`DecimalField` in `hospital/views.py`, the unused invoice search-scope entry, the no-op form `clean`, duplicate `highlight(0)`, dead `?` shortcut, dead `data-sidebar` attribute, duplicate `.gitignore` entry, and obsolete CSS selectors. Also replace hard-coded CSV template URLs with `{% static %}`, correct stale `aria-sort`, and remove dashboard counts that no template reads. **Batch fix prompt:** In `hospital/views.py`, `hospital/forms.py`, `hospital/permissions.py`, `static/js/app.js`, `static/css/app.css`, `templates/base.html`, `templates/hospital/csv_import.html`, and `.gitignore`, remove the confirmed dead symbols listed in Section 4 and make the small static/accessibility corrections in P3.1; keep behavior unchanged, run Ruff and the Django suite, and manually check desktop/mobile navigation and the palette.

## 4. Redundancy Removal Log

- `hospital/views.py:124` — delete unused `ZERO_MONEY` and the `DecimalField` import used only to build it.
- `hospital/views.py:175` — delete `SEARCH_SCOPES["invoices"]`; `quick_search` has no invoice search branch.
- `hospital/views.py:274-276,300` — delete `open_encounters`, `today_patients`, `open_orders`, and `eye_waiting` dashboard context queries; no dashboard template consumes them. Keep the separate deliveries `open_orders` query.
- `hospital/permissions.py:38-47` — delete unused `unlocked_required` and its `messages`/`redirect` imports; the active screen guard is `ScreenLockMiddleware`.
- `hospital/forms.py:79-80` — delete `SeparatorTolerantMixin.clean`, which only returns `super().clean()`.
- `hospital/models.py:302-313` — delete `StockBatch.days_to_expiry` and `balance_at` if there are no external integration callers; there are no in-repository callers.
- `static/js/app.js:261` — delete the second consecutive `highlight(0)`.
- `static/js/app.js:308` — delete the `?` shortcut branch that targets nonexistent `[data-shortcut-help]`, unless a help panel is intentionally added.
- `templates/base.html:27` — delete unused `data-sidebar`; sidebar control uses the `.sidebar` selector.
- `static/css/app.css:183-185,237` — delete orphan `.user-chip`, `.quiet-link`, and `.quiet-button` selectors after confirming no external template injects them.
- `static/css/app.css:445` — delete `.palette-backdrop[open]`; the palette is a `div` controlled by `.is-open`.
- `.gitignore:8` — delete the second `media/` entry; the first already ignores it.
- `.vscode/settings.json` — delete the Conda-only workspace settings; setup and scripts use `.venv`.
- `scripts/stop-server.ps1` — delete the unreferenced instruction-only wrapper; it does not stop a process and duplicates service-manager guidance.
- `output/pdf/KFBH-owner-report-sample.pdf` — delete the unreferenced generated sample; the report can be reproduced through the application.
- `.env.example:31` — delete `KFB_BACKUP_RETENTION_DAYS` if retention is not implemented; nothing reads it today.

**Do not delete blindly:** dormant model tables, historical migrations, and historical audit documents may contain live data or preserve migration history. Resolve P2.8 with a migration and data check before retiring any schema.
