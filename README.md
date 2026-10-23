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
uv run --project services/api uvicorn collective.main:create_app --factory --app-dir services/api/src --port 8000
```

Web: <http://localhost:3000>. API health: <http://localhost:8000/v1/health>.
API docs: <http://localhost:8000/docs>. Supabase is local; no cloud project is linked.
Use `.env.example` for environment names; keep actual credentials untracked.
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
```

The development roadmap is in `docs/plans/`. Actual task evidence and outstanding
work are recorded in `docs/plans/development-progress.md`. The initial scaffold is
not a working purchasing product; application features are added in roadmap order.
