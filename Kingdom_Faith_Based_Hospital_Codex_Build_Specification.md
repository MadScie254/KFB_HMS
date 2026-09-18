# Kingdom Faith Based Hospital — Codex build specification

Location: Malaha, Webuye, Kenya. Currency: KES. Local time: Africa/Nairobi.

## Copy this instruction into Codex and attach this file

Build the hospital operations application described in this specification. Implement working, persistent end-to-end workflows with a polished, accessible interface. Treat this document as the product brief and acceptance contract. Start implementation, rather than returning another proposal. Make routine technical decisions independently and document assumptions. Do not ask the owner questions already answered here. Use configurable settings for missing prices, users, beds, and operational policies. Do not invent real credentials, clinical facts, integrations, or production readiness. Complete the agreed modules through staged implementation; track unfinished work explicitly. Use fictional demo data, and keep demonstration and production environments separate. This request is for the hospital application; the public marketing website is a later project.

## 1. Outcomes and operating context

The owner needs to reduce unrecorded services, missing medicines, inconsistent pricing, unexplained cash shortages, and opaque purchasing. Every service, stock movement, financial adjustment, and payment must have an accountable actor and traceable source. Alerts indicate discrepancies requiring investigation, not proof of theft.

Staff have limited computer confidence. Design around recognisable tasks and short guided workflows. Each person has a named account and sees only relevant functions. Reception is the only collection point, including for pharmacy walk-ins. The owner mainly monitors remotely and does not want to become a bottleneck for routine approvals.

Known equipment: reception computer and clinician computer; pharmacy computer with issues; an additional computer in a department described as “ID,” also with issues. Do not assume what ID means. Receipt and ordinary printers exist; barcode scanners do not. Internet is intermittent. Power outages can last one or two days. No recurring software subscription budget is available. Repairing computers, a reliable local network, power protection, and backup media remain separate operational needs.

Departments: reception/cashier, outpatient, inpatient wards, nursing, maternity, pharmacy, eye clinic, theatre, dental, laboratory, and ultrasound. Approximately 35–40 beds; use a configurable bed register, not an invented actual inventory. There are day and night shifts. Some roles may be performed by the same person, but incompatible transactions still require separate review.

Payments: cash and hospital M-PESA Till. Bank payments are disabled by default; no card integration. Inpatients can make partial payments and settle their balances later. Do not assume insurance/SHA is in use. Provide room for future integration without presenting it as functional.

## 2. Defaults, uncertainties, and configuration

Use these choices without further interview:

| Topic | Default / known fact | Implementation treatment |
|---|---|---|
| Hospital name | Kingdom Faith Based Hospital | Editable official receipt header; never “Best” |
| Language | Plain English | Translation-ready labels; Kiswahili can follow |
| Owner phone access | Reports, primarily read-only | No mandatory owner approval queue |
| Official prices | Existing price list, CSV export possible | Import and validate; unknown prices cannot silently become zero |
| Eye scheduling target | Approximately 12 patients, flexible | Count unique patients, separately show eyes |
| Eye surgery charge | KES 12,000 per eye, as described | Initial configurable rate requiring operational confirmation before live use |
| Both eyes | KES 24,000 at the above rate | No implicit bilateral discount |
| Eye doctor fee | KES 2,000 per patient | Initial configurable rule, not per eye; explicitly confirm before live payroll/payout |
| Surgery payment | Full payment, no booking deposit | Waiting-list entry may precede payment; track settlement separately |
| Surgery package | Includes medicines and tests, based on final clarification | Exact inclusions require configuration; no guessed item list |
| Stock | One physical pharmacy/store | Track ward/theatre custody without inventing a second physical store |
| Spectacles/optical products | Answer unclear | Optional disabled category |
| Patient volume | Not supplied | No fabricated demand, income, or growth forecast |
| Approver | Trusted delegated reviewer, distinct from requester | Owner appoints during setup; role exists but no invented staff identity |

Missing operational data should not prevent building and demonstration. It must be conspicuous in the production setup checklist. Unconfigured approval authority disables sensitive changes; it must not silently allow self-approval.

## 3. Deployment and cost design

Prefer a browser-based application served by one reliable hospital computer on the local network. All workstations use the same database through the application server. No client installations beyond a supported browser; no database file shared across PCs. Local browser access should continue during an internet outage if the server, router, and workstation have power.

A local server cannot provide live remote reports while its internet connection or power is unavailable. Clearly show data freshness. Do not promise free cloud hosting, continuous availability, or zero maintenance. A future authenticated private remote connection can expose the owner's report view once an appropriate networking option is configured; never expose a database port or an unauthenticated hospital application to the internet. Initial remote access may remain unconfigured, with a clear setup guide, rather than a fake phone dashboard containing live-looking data.

Use a maintainable modular monolith. Suggested baseline: Django with PostgreSQL, server-rendered pages, progressive enhancement using HTMX, and a cohesive locally bundled design system. Equivalent technology is acceptable if it preserves low resource use, transactional correctness, and easy maintenance. Verify current supported dependencies during implementation and pin versions. Avoid unnecessary microservices, paid APIs, cloud-only authentication, external runtime fonts, and CDN dependencies for essential screens. Use fixed-precision money, transactional stock/financial writes, migrations, and automated backups. Browser localStorage is not a clinical or financial database.

Provide documented installation for the chosen host OS, start/stop/restart procedures, environment configuration, backup/restore tools, and a simple health screen. A desktop shortcut should open the application. Do not promise support for unspecified or broken hardware. Include a host-readiness checklist.

No multi-device offline write/sync engine in the first release. If the server is unavailable, staff use numbered downtime forms. Provide printable patient/visit, payment, dispensing, and nursing downtime forms and a controlled back-entry workflow. Capture original event time, paper reference, entry time, and staff member; prevent duplicate receipts or stock deductions. Urgent care continues through the downtime process. Cached screens must never claim to have saved a transaction when the server has not confirmed it.

Backups: daily encrypted database and attachment backup, configurable retention, a separately stored copy, documented recovery keys held by the owner, checksums, failure alerts, and a tested restore on another environment. A backup on the same hard drive is insufficient. Recommend a UPS for safe shutdown; do not imply it can power the hospital for two days. Include a power-loss recovery drill and an explicit recovery-point limitation between backups.

## 4. Interface and design contract

Build a coherent corporate hospital interface, not a generic template full of unrelated cards. Use deep navy (#142D4E), teal (#087F8C), off-white (#F5F7FA), white surfaces, restrained borders, and accessible semantic colours. Verify contrast rather than assuming these tokens are sufficient in all combinations. Use locally hosted fonts or system fonts, body text around 16px, comfortable spacing, and touch targets around 44px. Tables must remain readable on ordinary older desktop displays; owner reporting must work on a small phone screen.

Desktop layout: hospital identity and compact role-specific navigation on the left; page title, search, current user, shift, connectivity, and help in the header. Put the main task above secondary reports. Keep navigation names concrete: Patients, Queue, Payments, Pharmacy, Wards, Eye Clinic, Laboratory, Ultrasound, Theatre, Dental, Maternity, Reports, Stock & Purchasing, and Settings. Only show permitted sections. Provide breadcrumbs and consistent back navigation.

Forms: visible labels, sensible defaults, short sections, inline validation, large primary action, cancel/back option, searchable medicine/service lists, and plain-language explanations. Show units next to quantities. Warn about unsaved changes; safely retain drafts on the server. No patient data in browser caches by default. Show explicit saving/saved/failed states. Do not use icons without labels for essential actions.

Tables: search, filtering, readable dates, currency alignment, sticky headers where useful, pagination, and permission-controlled exports. Status labels must use words as well as colour. Empty states should explain the next useful action. Destructive or sensitive actions need a reason and a clear consequence preview. All visible controls must work or be explicitly disabled with an explanation.

Patient banner: patient number, name, date of birth or recorded estimated age, sex where clinically required, allergy status including “unknown,” current encounter, and clinician-visible alerts. Use two identifiers before orders, dispensing, results, and procedures. Never show payment disputes as if they were clinical allergies. Minimise financial staff access to clinical detail.

Owner homepage: a maximum of six prominent KPI cards, then collections trend, department activity, receivables, and prioritised exceptions. Every figure links to its underlying authorised records. Date filters: today, yesterday, last seven days, this month, custom range. Show last refresh and unavailable/stale data. Distinguish demo data prominently.

Role homepages:
- Reception: Find patient, Register patient, Start visit, Take payment, Print receipt, Close shift.
- Clinician: My queue, Patient chart, Notes, Orders, Prescription, Admit/discharge.
- Nurse: Ward board, Due observations, Medication administration, Handover.
- Pharmacy: Prescriptions, Walk-in order, Dispense, Stock lookup, Ward/theatre issues.
- Eye clinic: Waiting list, Clinical assessment, Surgery sessions, Follow-up.
- Lab/ultrasound: Requested work, In progress, Results awaiting review, Released results.
- Reviewer: Pending exceptions and purchasing approvals, with supporting evidence.

## 5. Permissions and accountability

Enforce permissions on the server for every action and record, not just by hiding navigation. Named accounts, secure password handling, session expiry, screen lock, and audited account recovery are required. Support additional authentication for owner/remote access. Do not ship default production credentials.

| Role | Allowed work | Key restrictions |
|---|---|---|
| Owner | Reports, user/role governance, configuration oversight | Clinical access only if separately authorised; no deletion of history |
| Reception/cashier | Registration, bills, cash/M-PESA recording, receipts, shift reconciliation | Cannot change official prices, silently verify M-PESA, dispense stock, or approve own refunds |
| Clinician | Notes, orders, prescriptions, admissions, clinical discharge | No collections or financial erasure |
| Nurse | Vitals, observations, administration, ward handover | No prescribing unless separately authorised by role and hospital policy |
| Pharmacy | Stock custody, dispensing, walk-in baskets, delivery checks | No payment collection; no self-approved write-offs |
| Administrator/procurement | Supplier records, purchase requests/orders, delivery entry | Cannot approve own purchase/adjustment or change supplier payment details unreviewed |
| Lab/imaging/dental/eye staff | Assigned departmental work within qualifications | Clinical and financial access scoped by duty |
| Delegated reviewer | Independent refunds, adjustments, procurement and variance review | Must differ from transaction initiator; no concealed record changes |

The administrator currently buys and checks deliveries. Preserve the ability to enter those activities, but add independent verification by another authorised person. When staffing prevents immediate independent review, record an explicit exception and outstanding review; do not pretend segregation occurred. Ordinary work should not await owner phone approval.

Keep append-only application audit events with actor, effective role, action, timestamp, entity, reason, before/after where appropriate, and session/device metadata without passwords or unnecessary clinical text. Export audit records to a separately protected destination when configured. Describe them as tamper-evident controls, not an impossible promise against someone with total server/database control. Audit sensitive record access, exports, role changes, and emergency access.

## 6. Reception and clinical workflows

Registration: generate unique patient number; search by name, phone, or number; warn of potential duplicates but do not merge automatically. Support children/newborns linked to guardians and patients lacking a phone or ID. Distinguish registration from encounter. Existing patients retain longitudinal history. Walk-in retail customers do not need a fabricated clinical visit; capture necessary identity when the medicine/workflow requires it.

Outpatient flow: registration → triage → clinician → requested tests/imaging → results review → prescription/procedure → cashier/dispensing → visit closure. Real workflows can loop; do not enforce a rigid single linear path. Clinical urgency and authorised emergency care override routine payment gates with a documented reason and subsequent financial review.

Clinical chart: history, complaints, examination, assessment/diagnosis entered by clinicians, allergies, medication history, vital signs with units and event times, investigations, treatment plan, follow-up, attachments, and authored notes. Support draft, signed, and amended notes; signed notes are not overwritten. Prescriptions contain medicine, strength, dose/unit, route, frequency, duration, quantity, instructions, and prescriber. Record cancellation and replacement safely. Do not invent dosing, diagnoses, normal ranges, clinical algorithms, or automated treatment advice. Any decision-support rule needs separate clinical validation.

Laboratory and ultrasound: link order, patient, encounter, charge, specimen details when applicable, performer, result/report, reviewer, release time, and amendments. Clinical results are released by authorised staff, not automatically by payment status. In-app communication can flag clinician-entered urgent findings; it is not an unattended patient-monitoring device or validated alarm system.

Inpatient: configurable wards/beds, admission, transfer, occupancy history, attending team, observation charts, input/output where configured, nursing notes, medication administration, and shift handover. Administration records include given/not given/withheld, time, dose, and reason. Distinguish order, issue to ward, administration, and return. Bed/day charging rules need explicit configuration for start/end/rounding and prevent duplicate accrual.

Clinical discharge and financial settlement are separate statuses. Discharge summary includes admission reason, course, clinician-entered diagnoses, procedures, discharge medicines, follow-up, and authorisation. Outstanding debt remains visible without preventing documentation of clinical discharge or emergency care.

Maternity: maternal encounter and admission, observations, clinician-entered labour/delivery records, maternal/newborn linkage, outcomes, nursing care, charges, and discharge. Do not invent obstetric charts or automated clinical thresholds; configurable templates require clinician review before use.

Dental: complaints, findings, optional tooth identifier, procedure orders/completion, clinician notes, prescriptions, charges, follow-up. Theatre: schedule, team, preoperative documentation, consent record/attachment, procedure, anaesthetic record template, consumables, recovery observations, and completion/cancellation. Clinical templates must be reviewed by qualified hospital staff before production.

## 7. Eye clinic and surgery sessions

Waiting list fields: patient, clinical assessment status, proposed procedure label, eye (right/left/both), intended session, readiness status, package price/version, payment status, notes, and follow-up. Do not infer a specific surgery type such as cataract from “eye surgery.” Clinical readiness must be confirmed by authorised clinical staff.

Session board: unscheduled, proposed, confirmed, completed, cancelled. Show unique patient count and eye count separately. At around 12 patients, show a scheduling suggestion. Permit smaller/larger sessions. Staff assign a visiting doctor, date, theatre, and patient roster; do not auto-confirm clinical readiness or send external messages.

Initial example pricing: right eye KES 12,000; left eye KES 12,000; both KES 24,000. The owner described full payment with no booking deposit. Keep the package rate editable by authorised staff and versioned; historic bills retain their original rate. Waiting-list booking does not itself create fictitious revenue.

Configure exactly which medicines, tests, and services are included. Included items create clinical/stock records and cost entries but do not produce a second patient charge. Extras must be explicitly excluded from the package and separately itemised. Do not set the internal cost of included medicines to zero.

Visiting eye doctor: default payable KES 2,000 per completed patient case, not per eye. Prevent duplicate accrual for a bilateral case. Other visiting clinicians use configurable fixed-fee arrangements. For twelve completed one-eye patients at these defaults, example gross package billing is KES 144,000 and doctor payable is KES 24,000; the KES 120,000 difference is not profit because other costs remain. These are examples, not actual hospital results. Confirm bilateral/repeated-session payout treatment before live use. Record accrued, approved, and paid status separately. Cancelled cases must not silently generate doctor payments.

## 8. Pharmacy, stores, and procurement

Medicine catalogue: generic/brand name as supplied, formulation, strength, base unit, purchase/sale units, pack conversions, approved prices, reorder level, supplier, batch, expiry, purchase cost, and optional barcode. Manual search is first-class. Optional scanner support later should not be required for operations.

Conversions are explicit per product: e.g. 1 box = 10 strips = 100 tablets only when configured for that product. Store movements in the base unit, display chosen selling units, validate divisibility, and prevent negative stock. Batch-aware dispensing should suggest earliest valid expiry; expired/quarantined stock cannot be dispensed. Record authorised substitutions and prescription changes without overwriting the original.

Walk-in workflow: pharmacy prepares priced basket → unique order reference → receptionist collects payment → pharmacy sees server-confirmed clearance or a documented authorised exception → actual dispense posts stock movement. One patient payment cannot clear two unrelated baskets. Counter-sale receipt and stock issue are linked. Clinically restricted medicines still follow configured prescription and pharmacist review requirements; do not assume every walk-in sale is unrestricted.

Hospital prescription workflow: prescribe → price/package/credit treatment → payment or authorised inpatient/emergency pathway → dispense actual quantity. Track partial dispensing, remaining quantity, cancellations, and returns. Prescription creation and invoice creation do not deduct stock.

Ward/theatre supplies: record custody movements from pharmacy to named department/person. Once issued, quantities remain visible as unconsumed departmental custody until administered, consumed, returned, or approved waste. Patient-specific and general consumable issues are distinct. Never deduct stock again at administration if already removed from pharmacy; reconcile total stock across custody locations.

Outsourced items: record unavailable item and whether the hospital procures it or the patient sources it externally. Hospital procurement requires supplier/cost/delivery evidence; patient-sourced items are documented without inventing a hospital sale or stock receipt.

Purchasing: request → quotation/reference price → approved order → received quantities and batches → independent check → supplier invoice match → payable/payment record. Allow partial deliveries and returns. Flag quantity/price discrepancies, duplicate invoices, supplier detail changes, and price deviations against configurable thresholds. Administrator cannot self-approve discrepancies. Supplier payment recording is bookkeeping, not automatic transfer of money.

Stock counts: opening witnessed count, cycle counts, blind count option, freeze/snapshot and movement-aware reconciliation, independent review, approved adjustment. Expected stock = opening + receipts + returns into custody − dispensing/consumption − returns out − approved write-offs, adjusted for transfers by location. Transfers cancel at whole-hospital level. No editable “current quantity” field that bypasses the ledger. Separate financial refund from physical return; returned medicine is quarantined until disposition is authorised.

## 9. Billing, payments, and shifts

Maintain versioned service and product price lists. Only authorised changes, with reason and effective date. Snapshot prices on invoice lines. Use decimal arithmetic; consistent explicit rounding. Do not assume taxation rules or claim a receipt is a compliant tax invoice without verifying applicable requirements. Tax/fiscal integration is a separate production configuration task.

Keep invoices, credit notes, payments, payment allocations, refunds, patient deposits, supplier payables, and cash transfers distinct. Deposits are not revenue just because cash arrived. Allocate partial payments explicitly. Outstanding balance = posted invoice charges − approved credits − allocated valid payments, with reversals reflected. Show unapplied deposits separately. Prevent over-allocation and duplicate settlement.

M-PESA: implement a provider adapter with a clearly marked demo mode and an honest unconfigured production state. Live integration requires verified account capability, credentials, and reachable infrastructure. A local-only server cannot receive public callbacks unaided. For initial manual operation, record references as “recorded—unverified”; a separate authorised reconciler verifies against hospital-controlled transaction records/statements. Never accept a screenshot as authoritative. Distinguish manually verified from provider-confirmed. Enforce reference uniqueness and handle reversals, mismatched amounts, delayed confirmations, duplicate events, and split allocations. Receipt wording must disclose verification status. Do not fabricate successful integration.

Cash: cashier opens shift with counted float; system records named cashier, receipts, authorised refunds, transfers, handover, and closing count. Support day/night shifts crossing midnight. Expected drawer cash = opening float + cash receipts + authorised cash transfers in − cash refunds − authorised transfers out. Pharmacy purchases and petty expenses must not silently reduce takings; use a documented transfer/petty-cash workflow. Record actual count, variance, explanation, and reviewer. Closing a shift does not delete or re-date its transactions. Display both calendar-day totals and shift totals with clear boundaries.

Corrections: posted transactions cannot be deleted. Use authorised reversals/credit notes with linked originals, reasons, actors, and dates. Refunds reference the original payment and cannot exceed the available refundable amount. Approved financial refund does not automatically restore medicine stock. Receipt reprints retain the same number and are labelled duplicate. Printer failure does not repost payment. Double-clicks and retries must not double-charge.

## 10. Owner KPIs and exceptions

No invented financial totals. Seed demo data only in the demo environment. All KPIs must reconcile to ledgers and share explicit date filters.

| KPI | Definition / caution |
|---|---|
| Net billed charges | Posted charges minus credit notes in period; exclude drafts |
| Collections | Valid recorded cash and verified electronic receipts in period, with refunds separately visible; unverified M-PESA shown separately |
| Receivables | Outstanding posted balances as of selected time; ageing buckets |
| Unapplied deposits | Received balances not allocated to bills; not double-counted as revenue |
| Cash variance | Actual closing drawer count minus expected drawer count |
| Stock discrepancy | Physical count minus expected stock at matching cutoff, with unit/cost basis |
| Stock value | Remaining stock valued using a documented consistent costing method |
| Department activity | Completed encounters/services, separate from cash attribution |
| Bed occupancy | Occupied active beds / configured usable active beds |
| Eye waiting list | Unique awaiting patients; eyes and readiness shown separately |
| Doctor payables | Accrued completed-case fees minus approved adjustments/payments |
| Operating result | Only if adequate costs and accounting basis are configured; otherwise show unavailable |

Department collections require explicit line-level allocation for mixed-department invoices; never assign the whole bill to each department. Use a documented allocation method and show unallocated payments separately. Do not double-count patient deposits, package components, or a returned/reversed sale.

Exception centre: unresolved cash variances, repeated refunds, unverified M-PESA, stock shortages, unusual write-offs, near-expiry batches, overdue receivables, purchase discrepancies, account/price changes, duplicate references, unreviewed emergency overrides, overdue backups, and pending independent reviews. Configurable thresholds; no employee “thief scores.” Each exception has evidence, responsible reviewer, status, and resolution history. No automatic external messaging or public disclosure.

## 11. Database and integrity requirements

Model at least: users/roles/permissions, patients/guardians, encounters/queues, notes/amendments, observations, prescriptions/items, service orders/results, wards/beds/admissions/transfers, medication administrations, surgery sessions/cases/packages, clinician fee rules/payables, catalogue/units/conversions/prices, batches/custody locations/stock movements/counts, suppliers/orders/receipts/invoices, patient invoices/lines/credits, payments/allocations/refunds, shifts/cash movements, approvals/exceptions, audit events, attachments, and import jobs.

Use stable internal identifiers and separate human-readable numbers. Preserve foreign keys. Server generates timestamps; retain actual event time and entry time where different. Store timestamps consistently and display Africa/Nairobi. Ledger rows and posted documents are immutable through normal application actions. Deactivate referenced catalogue records rather than deleting them.

Use database transactions and locks/constraints for stock deduction, payment allocation, refund limits, unique receipt/reference generation, and case-fee accrual. Implement idempotency keys for retries/imports/provider events. Two pharmacists cannot dispense the same last available stock simultaneously. Concurrency conflicts must produce understandable errors, not silent overwrites. Use optimistic version checks for editable clinical drafts/configuration.

## 12. Migration, security, and operational readiness

CSV import wizard: upload → map columns → preview → validate → dry run → confirm → report. Support products, pack units, prices, patients, opening stock/batches, and opening receivables with separate templates. Give row-level errors and prevent duplicate re-import. Escape spreadsheet formula injection in exports. Validate file size/type and attachment access. Never assume CSV files contain reliable balances; opening inventory and cash need witnessed checks. Do not import clinical data into production without authorised review. Audit import batches and provide safe rollback only for untouched imported records; otherwise use controlled corrections.

Use transport encryption for configured access, secrets outside source control, restricted database accounts, secure cookies, CSRF protection where relevant, input validation, authenticated attachment downloads, and no patient details in error logs. No third-party trackers. Minimise patient data in remote owner reports. Keep user-account administration separate from routine clinical access.

Before real use, verify applicable Kenyan health-data, professional-practice, fiscal/receipt, retention, and hosting obligations against current authoritative requirements. Do not claim the application is legally certified or clinically validated merely because it implements access controls. Document this as an operational readiness task, without obstructing fictional-data development. Do not publish real patient information or production credentials.

## 13. Implementation stages and required deliverables

Stage 1: coherent design system, authentication/permissions, real database/migrations, patient registry, service/product catalogue, reception queues, invoices, cash/manual M-PESA, pharmacy dispensing, stock ledger, shifts, basic owner reports, and independent approvals. Demonstrate the full walk-in sale and outpatient payment/dispensing flows.

Stage 2: clinical chart, prescriptions, laboratory/ultrasound workflows, inpatient beds/nursing/administration/discharge, maternity, dental, theatre, eye-surgery sessions/packages/fees, purchasing, full reconciliation, CSV migration, backups, and downtime reconciliation. These are required parts of the requested application, not permanently deferred placeholders.

Stage 3: configured remote owner access, verified live payment integration if credentials/infrastructure permit, deployment hardening, restore rehearsal, and supervised operational pilot. If external prerequisites are absent, complete adapters and guides, clearly report the blocked integration, and keep the rest usable locally.

Required deliverables: working source repository, reproducible setup, dependency lockfiles, schema/migrations, fictional demo seed, clean production bootstrap, environment example without secrets, CSV templates, printable receipts/forms, role-based quick guides, backup/restore scripts, automated critical-workflow tests, screenshots at desktop/mobile sizes, implementation status, and explicit remaining production prerequisites. Do not deploy publicly by default.

## 14. Acceptance scenarios

1. Reception registers a patient; clinician records and signs a note; pharmacy dispenses the actual prescribed amount; bill, payment, receipt, and stock agree.
2. A pharmacy walk-in basket is paid only at reception; pharmacy cannot collect or silently mark it paid. Repeated dispense clicks create one issue.
3. Product configured with 100 tablets per box receives two boxes and dispenses 15 tablets, leaving 185 tablets, with traceable conversion and batch.
4. Two concurrent requests for the last stock cannot both succeed. Expired/quarantined batches cannot be issued.
5. A KES 10,000 inpatient invoice with KES 4,000 allocated payment has KES 6,000 outstanding. An unapplied deposit is not counted twice.
6. A KES 1,000 opening float plus KES 8,000 cash receipts minus KES 500 refund minus KES 6,000 documented cash transfer gives KES 2,500 expected cash; a KES 2,300 count gives a KES 200 shortage. M-PESA does not change drawer cash.
7. A repeated M-PESA reference or duplicate callback cannot credit twice. Unverified references are visibly separate from confirmed receipts.
8. A refund requires an independent reviewer, creates linked financial records, and does not automatically return stock. A return enters quarantine.
9. A bilateral surgery at the default rates bills KES 24,000 and accrues one KES 2,000 case fee under the provisional per-patient rule; package items are not billed twice.
10. Twelve one-eye completed cases demonstrate KES 144,000 gross package charges and KES 24,000 accrued doctor fees, without labelling the difference profit.
11. A ward issue followed by administration does not deduct the same quantity twice; unused quantities can be reconciled and returned.
12. An administrator cannot approve their own purchase, write-off, or supplier-account change through either UI or direct API calls.
13. A clinician can record emergency care and clinical discharge despite an unresolved bill; debt and override remain accountable.
14. A signed clinical note can be amended with attribution, never silently overwritten. An unauthorised cashier cannot retrieve its contents through an endpoint.
15. Reprinting a failed receipt does not create another payment. Overnight shift totals and calendar-day totals remain explainable.
16. Internet loss leaves powered local operations working; server loss produces an honest failure state and downtime guidance. Back-entry does not duplicate paper transactions.
17. Backup restores successfully into a separate test environment with matching financial/stock balances and protected attachments.
18. CSV dry run identifies malformed/duplicate rows; retrying a committed import does not create duplicate opening stock or receivables.
19. Owner phone reports display real filtered totals, clearly show freshness, and provide no unauthorised patient-chart access.
20. Keyboard-only use, readable contrast, printing, empty/error/loading states, and ordinary desktop/phone layouts are checked with actual screenshots and interaction tests.

## 15. Completion standard

A beautiful static dashboard is not completion. Deliver a working application with persistent records, enforced permissions, reconciled stock and payments, independently reviewable exceptions, and usable staff workflows. Report exactly what was tested, what functions locally, and what still requires credentials, hospital configuration, clinical sign-off, or hardware. Do not promise that software alone prevents collusion, unrecorded cash sales, or physical theft: pair it with official price displays, patient receipts, restricted stock access, witnessed counts, and independent reconciliation.
