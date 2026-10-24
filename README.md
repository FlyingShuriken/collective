# Orbyte Collective

XMUM SDC Hackathon restocking assistant. AI proposes records; deterministic Python
calculates purchasing decisions; people approve records, learned rules and orders.

## Development

Use Node **22.23.1**, pnpm **10.32.1**, Python **3.12.13**, and a running Docker daemon.
The project-local Supabase CLI is pinned in `package.json`; dependency resolutions
are locked in `pnpm-lock.yaml` and `services/api/uv.lock`.

```sh
pnpm install --frozen-lockfile
uv sync --project services/api --locked
pnpm exec supabase start
uv run --project services/api python scripts/setup_local.py
```

Start these in separate terminals from the repository root:

```sh
pnpm dev
uv run --project services/api uvicorn collective.main:create_app --factory --reload --reload-dir services/api/src --port 8000 --no-access-log
```

Web: <http://localhost:3000>. API health: <http://localhost:8000/v1/health>.
API docs: <http://localhost:8000/docs>. Supabase is local; no cloud project is linked.
Use `.env.example` for environment names; keep actual credentials untracked.
Seed the synthetic personas and records before testing authenticated access:

```sh
uv run --project services/api python scripts/seed_demo.py --scenario three-cafes --anchor now
```

The seed prints the ignored local credential-file path. Sign in at
<http://127.0.0.1:3000/en/sign-in> using one of those accounts. Public signup is disabled.
The workspace link opens persisted comparisons, solo/pool approval, withdrawal, simulated
order creation and activity at `/en/approvals`. Each owner approves their own cap and expiry;
ready pools create no order until an approved owner explicitly executes the release.
Orders are simulated outputs and are never sent to suppliers.
The working [submission report](docs/submission-report.md) describes the problem,
architecture, demonstration and current evidence limits.
Repeated seeding preserves the existing run; use a new `--run` after its order window expires.

Run `scripts/setup_local.py` again after a local database reset, before starting the API:
Supabase resets recreate the runtime role without a login password. This script provisions
only the local restricted role and writes ignored environment files; it never links a cloud project.

```sh
pnpm lint
pnpm typecheck
pnpm build
pnpm test
pnpm contracts:generate
pnpm contracts:check
pnpm fixtures:check
uv run --project services/api pytest services/api/tests/integration -q
pnpm auth:check # Requires the running local web/API and the default demo seed.
pnpm exec playwright install chromium
pnpm exec playwright test
```

The [Playwright runner](https://playwright.dev/docs/test-cli) starts missing local API/web
servers, or reuses running ones. Restart an API without reload after source changes.
Stop other API/worker processes before Python integration tests: lease/recovery tests
need exclusive control of their local jobs. Restart the API for `auth:check`; Playwright
can start it with the normal worker when absent.
Each browser test seeds a new isolated synthetic run; credentials stay under ignored
`.cache/`. Login traces are disabled because they would record those credentials. Browser
screenshots and failure output stay under ignored `test-results/`.

The API starts the durable worker by default. [Supabase Cron](https://supabase.com/docs/guides/cron/quickstart)
enqueues due reorder/expiry evaluations once per minute; it never issues orders. The
Activity view separates queued, retrying, processing, completed and failed tasks from
human confirmation. Expiry is also checked synchronously when an owner releases an order.

To run the same worker separately, set `ENABLE_WORKER=false` for the API and start:

```sh
uv run --project services/api python -m collective.jobs.runner
# One processing iteration for a local check:
uv run --project services/api python -m collective.jobs.runner --once
```

Jobs have five-minute leases and at most three processing attempts. Expired leases can
be reclaimed; stale workers cannot commit outputs. Results, state and audit events commit
together. Reorder/expiry, invoice/stock intake, invoice checking, supplier quote-reading and message-drafting
handlers use this runner.

The Sources section accepts private PNG/JPEG/PDF files up to 10 MB and 10 pages,
or UTF-8 text up to 16,000 characters. It saves normalized pages and provides actual
source crops. Review sensitive values or draw image/PDF masks, then explicitly prepare
the provider copy. Originals stay separate; restricted staff cannot fetch price-bearing
sources. Preparation queues extraction but does not confirm a record. The liaison API can
read prepared supplier replies into editable quote proposals; confirmation creates a
sealed quote with unresolved terms preserved. RFQ/query jobs create unsent drafts from
authorized context. Owner Send binds the exact text, recipient and revision and returns
one simulated receipt. Prepared invoices and stock text now create pending record proposals
through the same runner. The proposal API supports human corrections, confirmed mapping
selection, flag acknowledgement and bounded staff confirmation. Accepted purchases preserve
unknown fees and unmapped lines; incomplete records cannot supply comparable prices.
Stock observations require a confirmed mapping and an explicit observation time. Invoice
confirmation does not establish stock arrival. Failed intake can be retried at
`POST /v1/businesses/{business_id}/sources/{source_id}/intake` using its saved source hash;
the original failure and attempts remain available. Open a source in the workspace to review
its pending reading and original evidence, edit fields, select confirmed mappings, save a
revision and explicitly confirm or reject. Confirmation refreshes current buying comparisons.
The synthetic demo does not establish automatic masking or
retention suitability for arbitrary real invoices.

Owners can open **Match a product specification** in record review to request the
Matching Agent. Every proposal remains unconfirmed until the owner checks the source,
physical attributes, sale unit and pack factors. If replay has no matching response,
the pending mapping remains available for explicit manual review. Confirmed mappings
appear in the record editor; select one and save the record revision before confirmation.
Box/carton/pack conversions are bound to their confirmed sale unit. Mapping confirmation
does not rewrite historical records or issue an order. The pinned embedding benchmark
and its measured limitations are documented in [local BGE-M3 validation](docs/validation/bge-m3-local.md).

Confirming a delivered purchase queues a deterministic invoice check. An invoice without
an explicitly selected usable PO remains unmatched. The invoice-check API can bind the
confirmed purchase revision to an own issued PO; it compares the final issued tier and
agreed fees, preserves every difference and records ringgit materiality. Owners can confirm
or reject a discrepancy, then separately record a documented correction/credit from a
different reviewed source. A query draft or simulated Send never counts as recovered money.
The confirmed delivered record opens its invoice-check screen. Select an issued PO, inspect
the recorded differences and confirm/reject the discrepancy. A supplier-query draft must be
saved and explicitly reviewed before simulated Send. For recovery, upload/prepare a separate
credit or correction in Sources, return to the invoice and select that evidence. One evidence
source can support one recorded recovery in this initial flow.

The synthetic single-business browser flow uses the seed's confirmed quote (RM315 PO) and
`single-business-invoice.txt` (RM320 invoice), producing a RM5 difference. Exact query replays
include run-specific issued terms. For a declared local synthetic run, preload its flagged
check before requesting the query:

```sh
DEMO_MODE=true MODEL_MODE=replay uv run --project services/api python scripts/preload_query_replay.py --run demo --check-id "$COLLECTIVE_CHECK_ID"
# Add --clean to remove that authored query replay after the rehearsal.
```

The helper refuses foreign runs, undeclared source assets and nonlocal databases; it calls
no model and changes no ledger/review/message rows. The browser test preloads and cleans its
own context, then exercises the normal configured worker. This does not establish complete
offline recovery for arbitrary inputs; the wider replay rehearsal remains T24.

`MODEL_MODE=replay` is the default. Declared synthetic quote text, invoice PNG/PDF and
BM stock text have authored replays; these are workflow fixtures, not measured model output.
Rebuild them for the
configured model IDs with `uv run --project services/api python scripts/generate_replay_fixtures.py`.
Changed inputs/versions stop with `REPLAY_MISS`. Live/record modes need a server-only
`GEMINI_API_KEY`; recording also requires demo mode and a predeclared synthetic source.
Live provider accuracy/availability and complete fresh-seed replay coverage remain T23–T24.

The development roadmap is in `docs/plans/`. Actual task evidence and outstanding
work are recorded in `docs/plans/development-progress.md`. The complete purchasing
workflow is still under development; application features are added in roadmap order.
