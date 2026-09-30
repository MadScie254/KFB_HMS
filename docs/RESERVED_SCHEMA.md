# Reserved clinical schema

Updated: 30 September 2026.

The following tables exist in migrations but have no normal application writer or reader. Their presence does not mean the related clinical workflow is available. They remain in place because a deployed database may already contain records; removing a model or table without checking live rows could lose data.

| Model | Current boundary | Required before activation |
|---|---|---|
| `MedicationAdministration` | No medication administration screen or review workflow. Ward custody accounting is a separate working workflow. | Clinically reviewed dose, timing, omission, correction, permissions and audit rules. |
| `NursingHandover` | No handover authoring or acceptance screen. | Shift ownership, acceptance and amendment rules. |
| `BedTransfer` | Admissions hold the current bed; no transfer action. | Atomic bed occupancy check, transfer history and accountable UI. |
| `TheatreCase` | No theatre scheduling or operative record workflow. | Clinical templates, consent handling and role review. |
| `DentalRecord` | No dental chart workflow. | Clinician-reviewed chart and correction rules. |
| `MaternityRecord`, `NewbornLink` | No maternity or newborn linkage workflow. | Clinician-reviewed templates, identity/link verification and permissions. |
| `EyePackageItem` | No package charging or stock deduction reads it. Configuration through Django admin is disabled to avoid presenting it as an active feature. | Define package versioning, invoice composition, stock consumption and reconciliation before exposing configuration. |
| `DowntimeEntry` | No electronic back-entry or reconciliation workflow. Paper reconciliation is manual under [downtime reconciliation](DOWNTIME_RECONCILIATION.md). | Idempotent reference capture, duplicate checks and accountable review. |

`Refund` is **not** reserved: request, independent review and payout are implemented through the application, and paid cash refunds reconcile with cashier shifts. Admission and discharge are also application workflows; only the nursing and transfer records above are reserved.

Before retiring any reserved table, inspect row counts and references in the live database, decide how existing records will be retained or migrated, rehearse the migration on a restored backup, and obtain clinical and operational sign-off. Historical migrations must remain in version control. No schema deletion is part of this status clarification.
