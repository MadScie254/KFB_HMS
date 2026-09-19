# Production readiness audit — 19 September 2026

A defect-hunting pass over the whole system, looking for what would fail in a
real hospital rather than in a demonstration. Every finding below was
**reproduced against the running code before it was fixed**, and every fix has a
regression test that fails when the fix is reverted. That reversion was actually
performed to confirm it.

Fifteen defects. One would have made the production deployment unusable on
sight, four could corrupt the stock ledger or hide a number the owner acts on,
three would make a page fail outright, three are security exposures, three are
scaling faults that get worse every year the hospital stays open, and one would
have stopped the upgrade to this very release partway through.

The first and last of those were found only by doing the thing rather than
reading the code: running the application the way it would actually be deployed,
and rehearsing the migration against data shaped like a site that has been
running for a while. No test caught either one.

---

## 0. The production deployment served no stylesheet and no JavaScript

Django does not serve static files when `DEBUG` is off. Nothing in the
middleware chain did either, and `deploy/Caddyfile` reverse-proxies **every**
path — `/static/` included — straight to the application.

So on the documented production path, every asset 404s.

Reproduced with `DEBUG=False`:

```
GET /static/css/app.css        -> 404
GET /static/js/app.js          -> 404
GET /static/images/favicon.svg -> 404
```

The hospital would have opened the system on its first morning to unstyled HTML
with no JavaScript. Every test passed, because the test client renders templates
without ever fetching what they reference, and the demo runs with `DEBUG=1`
where Django *does* serve static files. The one configuration nobody exercised
was the only one that matters.

**Fixed** by adding WhiteNoise to the middleware chain, ahead of the session
middleware so an asset does not cost a session lookup, with hashed filenames and
immutable caching in production. Verified after the fix:

```
GET /static/css/app.96d13afac391.css -> 200  Cache-Control: max-age=315360000, public, immutable
GET /static/js/app.3049af15e10c.js   -> 200  Cache-Control: max-age=315360000, public, immutable
GET /favicon.ico                     -> 301  /static/images/favicon.svg
```

`collectstatic` is a required deploy step, so silence is no longer the failure
mode: `check_readiness` now reports when it has not been run, and the Caddyfile
says so at the point someone would read it. `/favicon.ico` is routed too — it
was answering a 19 KB error page to every browser that opened the site.

---

## A. Stock integrity — the ledger could go negative

### A1. One issue could spend the same batch twice

`issue_to_department` read each line's available balance from the ledger without
accounting for quantity already claimed by an **earlier line of the same issue**.
Two lines naming one batch each saw the untouched balance, both passed
validation, and both posted.

Reproduced: a batch holding 150 units accepted an issue of 100 + 100 and left
the ledger at **-50**.

This is the exact bug that had already been found and fixed in `dispense_order`,
where a `reserved` accumulator tracks in-flight claims. The custody path was
written later and never got it.

**Fixed** by carrying the same accumulator through `issue_to_department`. The
refusal now names what is available and how much another line already claimed.

### A2. A write-off was never re-checked against stock at approval

`review_write_off` validated the quantity when the write-off was *proposed* and
never again. Stock keeps moving in the hours or days before a reviewer gets to
it, so approving posted a deduction for stock that had already left.

Reproduced: 50 units proposed, 50 dispensed before review, approval accepted and
the ledger went to **-50**.

**Fixed** by re-reading the balance at approval. The refusal explains that the
stock has moved and asks for a fresh request, rather than silently posting.

### A3. FEFO meant different things on SQLite and PostgreSQL

Batch selection ordered by `expiry_date` and left NULL handling to the database.
SQLite sorts NULLs **first**; PostgreSQL sorts them **last**. A batch with no
recorded expiry was therefore dispensed first in the demo and last in
production — the same order, the same stock, a different batch off the shelf.

Reproduced by comparing the two orderings directly.

**Fixed** with an explicit `F("expiry_date").asc(nulls_last=True)` ordering,
stated once as `FEFO_ORDER` and used everywhere. A dated batch is now always
taken before an undated one, on any database.

### A4. A product that had never been stocked was invisible on the reorder list

`stock_position()` built its product list from `StockBatch` rows. A product with
no batch — newly catalogued, or never yet delivered — produced no row, so it
never appeared in `below_reorder`.

The item with nothing on the shelf is the one most urgently needing reorder. The
reorder list was silently missing exactly what it existed to surface.

Reproduced: an active product with reorder level 100 and no stock appeared in
neither `products` nor `below_reorder`.

**Fixed** by seeding the product list from the catalogue and folding stock into
it. Two related corrections came with it:

- `below_reorder` now requires a reorder level above zero. "Short by 0" is not
  short, and flagging every unmanaged product buried the ones that really were.
- A separate `out_of_stock` list reports active products with no sellable stock,
  which is a distinct question from being below a threshold.

---

## B. Pages that would fail outright

### B1. Raising an exception could 500 the request

Five call sites used `ExceptionRecord.objects.get_or_create(category=…,
summary=…)`. Neither field is unique and there is no constraint across them, so
nothing stopped two rows sharing a summary — and once two existed, **every later
attempt to raise that exception raised `MultipleObjectsReturned`**, a 500 in the
middle of receiving a delivery, accounting for ward stock, or approving a
write-off.

Reproduced directly.

**Fixed** with a `raise_exception()` helper keyed on a hashed `dedupe_key`
carrying a partial unique constraint, so the raise is idempotent under
concurrency. Two behaviours improved with it:

- a recurrence now increments `occurrence_count` and updates `last_seen_at`;
- a recurrence of something already marked **resolved reopens it**. Previously a
  problem that came back after being closed was silently discarded.

A data migration backfills dedupe keys for existing rows so exception history
does not split in two at the upgrade.

### B2. A colliding reference number was an unrecoverable error

Patient, encounter, invoice, payment, order, receipt, issue, count and write-off
numbers were all `uuid4().hex[:6]` (or `[:7]`) behind a date prefix, under a
unique constraint, with **no retry**. A same-day collision is an `IntegrityError`
that aborts whatever transaction the caller was inside — a lost payment or a
lost registration.

At 500 invoices a day the birthday probability on six hex characters is roughly
0.7% per day: a few hard failures a year, each one looking inexplicable.

Reproduced by forcing `uuid4` to repeat.

**Fixed** with a `ReferenceNumberMixin` that raises the entropy to ten hex
characters and retries on collision inside a savepoint, re-raising untouched any
`IntegrityError` that is not a clash on the generated reference.

### B3. An M-PESA rule that a form could not report

`Payment.save()` raised `ValidationError` for a missing M-PESA reference. `save()`
is not part of form validation, so `full_clean()` passed and the error surfaced
as a 500 rather than a message on the field.

Reproduced: `full_clean()` accepted the payment, `save()` raised.

**Fixed** by moving the rule into `clean()`, where a form reports it on the
`reference` field, and adding a `CheckConstraint` so no code path can write a
referenceless M-PESA payment. A data migration labels any that already exist
rather than deleting or blocking money the hospital took.

---

## C. Security on a production deployment

### C1. Production defaulted to insecure cookies and no HSTS

`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT` and
`SECURE_HSTS_SECONDS` all defaulted to **off regardless of environment**. A
production deployment that simply forgot four environment variables served its
session and CSRF cookies over plain HTTP, with patient data and a live session
on the wire.

Reproduced by loading settings with `KFB_ENV=production`: all four off.

**Fixed** so the safe value is the default outside demo mode; a site that
genuinely terminates TLS elsewhere can still opt out explicitly. Alongside it:

- a weak, short or placeholder `KFB_SECRET_KEY` is now a **startup failure**
  outside demo mode, not a deploy-check warning nobody read;
- `KFB_DEBUG=1` outside demo mode is refused;
- `CSRF_TRUSTED_ORIGINS` defaults from `ALLOWED_HOSTS` over HTTPS.

### C2. Uploads were trusted on the uploader's word

Invoice photographs and clinical attachments were validated on file extension
and `content_type` — both supplied by the browser and freely forged.

**Fixed** by reading the file's own leading bytes and checking them against the
format it claims to be. A file named `.jpg` whose contents are not a JPEG is now
refused. Upload handler sizes are bounded in settings so a large file is not
buffered whole before a form ever sees it.

Two test fixtures that uploaded `b"fake-jpeg-bytes"` are now real minimal images;
they were correctly rejected by the new check, which is the check working.

### C3. Sign-in throttling could be sprayed and could be weaponised

Lockout counted failures **per username only**. An attacker could try a few
passwords against every staff account from one machine and never trip anything.

**Fixed** by adding a per-source-address ceiling in the same window, set higher
than the per-account one because a shared ward workstation legitimately produces
several people's typos. A successful sign-in clears that account's failures but
deliberately **not** the address history: one person remembering their password
says nothing about the other attempts from that machine.

Note on a residual trade-off: per-username lockout still lets someone lock a
known account out for fifteen minutes by failing against it deliberately. That is
inherent to username lockout; the alternative is no lockout. It is recorded here
rather than hidden.

---

## D. Scaling faults

### D1. Opening a stock count was one query per batch

`open_stock_count` called `batch.balance_at(cutoff)` inside a loop — one
aggregate per batch — then inserted rows one at a time. It also enrolled **every
batch that had ever existed**, including long-depleted ones.

Reproduced: 43 batches produced **93 queries**, and 42 of the 43 sheet lines were
zero-balance rows for batches nobody would count.

**Fixed** with a single aggregate over the ledger, `bulk_create` for the lines,
and a scope that counts what is on the shelf — any non-zero balance, plus live
products so that "the ledger says zero but here are twenty" is still recordable.
Empty, retired batches drop off.

### D2. Every stock screen scanned dead batches

`batch_rows()` loaded every batch ever created and built a Python dictionary for
each one. Reproduced: 300 batches produced 300 rows of which **5 held stock**.
That cost grows for as long as the hospital stays open, on every dashboard load.

**Fixed** by excluding depleted batches at the database level, with an
`include_depleted` escape hatch. The per-product totals that need a complete list
now come from the catalogue instead (see A4), so nothing is lost.

### D3. The ledger everything aggregates over had no indexes

`StockMovement` declared **no indexes at all**. Every balance is a `SUM` over that
table and every statistic is a `SUM` over a slice of it, all of them unindexed
full scans.

**Fixed** by adding indexes on `(batch, event_at)`, `(movement_type, event_at)`,
`(event_at)` and `(reference_type, reference_id)`, plus supporting indexes on
`StockBatch`, `PriceVersion`, `Payment`, `GoodsReceipt`, `DepartmentIssue`,
`StockWriteOff` and `ExceptionRecord`.

---

## E. Smaller corrections

- **The `no-store` cache header was applied indiscriminately.** The header meant
  for patient data was set on every response the middleware saw. Static assets
  are now served by WhiteNoise ahead of that middleware, so in practice the
  caching fix for them is finding 0 above; the guard remains for any path under
  `MEDIA_URL` that is routed through Django later. Data pages stay `no-store`,
  which is verified by a test.
- **Persistent database connections were never health-checked.** `CONN_MAX_AGE`
  was 60 seconds without `CONN_HEALTH_CHECKS`, which hands the next request a
  socket the database has already closed — an intermittent `InterfaceError` that
  only appears under load. Now enabled.
- **`user_role()` swallowed every exception.** A bare `except Exception` meant any
  fault silently downgraded to "no permissions". Narrowed to
  `ObjectDoesNotExist`, which is the only case that legitimately means no role.
- **The log directory was created at import with no fallback.** A read-only or
  unwritable path crashed startup. Logging now falls back to the console, which
  a container platform collects.

---

## F. The upgrade itself

Found by rehearsing the migration rather than only writing it: a database was
built at the previous migration, populated through raw SQL with exactly the rows
the new constraints could reject — two exceptions sharing a summary, all marked
resolved, and an M-PESA payment with no reference — and then upgraded.

The first attempt **failed and rolled back**. `backfill_mpesa_references` used
`payment.save(update_fields=…)`, which raises `DatabaseError` if the row is not
matched, and one unmatched row aborts the entire upgrade. A data migration that
can be stopped by a single row is not a data migration you can run on a hospital's
live database.

**Fixed** by switching the backfill to `bulk_update`, which cannot raise on a
row that has moved and is far cheaper besides.

Rehearsed again after the fix:

```
Applying hospital.0008_production_hardening_indexes_and_constraints... OK

  exception 1: dedupe_key=3d210d231e97   summary='Duplicated summary'
  exception 2: dedupe_key=(blank)        summary='Duplicated summary'
  exception 3: dedupe_key=d0b89c45414b   summary='Distinct summary'
  payment: reference='MISSING-952EF197F7C5' verification=unverified

  raise_exception on an upgraded row -> pk 3, occurrences 2, status open
  total exceptions after re-raise: 3 (no duplicate created)
```

The second of the duplicated pair keeps a blank key, which the partial unique
constraint ignores, so legacy duplicates cannot fail the upgrade. The
referenceless payment is labelled for investigation rather than deleted or
blocked — it is real money the hospital took. And raising that exception again
afterwards reuses the existing row and reopens it, rather than creating a second.

---

## What was checked and found sound

Stated so the list above is not mistaken for the whole picture:

- The custody list page does not scale per row — it was already correctly
  prefetched. Measured at 5 and 30 issues: 12 queries both times.
- `dispense_order` already reserved across lines correctly (A1 is its sibling
  path, not a regression of it).
- Media is never served directly from disk; both file endpoints are behind role
  checks and audit every access.
- The append-only audit trail refuses `update()` and `delete()` at the queryset
  level as well as on the model.
- Segregation of duties holds at the service layer independently of page access,
  which is what makes the owner's full navigation safe.

---

## Verification performed

- **132 tests pass**, up from 111. The 21 new tests are one per defect.
- Each new test was confirmed to **fail against the unfixed code** by
  re-introducing the four structural defects and observing exactly the four
  corresponding failures.
- `scripts/checks.sh` exits 0 — ruff, tests, migrations-match-models, deployment
  check — and was confirmed to exit 1 when a lint error is present.
- The application was driven in a real browser across all nine roles: **216 page
  loads, zero server errors**, at desktop and 390×844 phone width with no
  horizontal overflow on any screen.
- Static delivery was verified by running `collectstatic` and fetching the
  assets with `DEBUG=False`, which is how the defect in finding 0 was both found
  and confirmed fixed.
- Production settings were loaded with `KFB_ENV=production` and confirmed to
  produce secure cookies, SSL redirect, one-year HSTS and derived CSRF origins.

## Still not done, and not claimed

Unchanged from `IMPLEMENTATION_STATUS.md`: supplier payables and returns to
supplier, opening-stock CSV handlers, count scheduling, disposal evidence for
write-offs, and per-location balance queries.

Added by this audit and deliberately **not** attempted here, because each is a
change of scope rather than a defect:

- there is no rate limit on anything except sign-in;
- `/admin/` is a second authentication surface with no additional protection
  beyond Django's own;
- background work (near-expiry sweeps, scheduled counts) has no runner;
- the per-username lockout trade-off in C3 is inherent, not solved.
