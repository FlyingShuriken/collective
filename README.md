# Orbyte Collective

A restocking assistant for small Malaysian businesses, built for the **XMUM SDC Hackathon**. Review invoices, compare supplier prices, track stock, and pool purchases with nearby businesses while keeping every order under owner approval.

Built with Next.js, TypeScript, FastAPI, and Supabase. Supports English, Bahasa Melayu, and Chinese.

For a step-by-step café-owner walkthrough, see the [user guide](USAGE.md).

## Getting started

### Prerequisites

- Node.js 22.23.1 and pnpm 10.32.1
- Python 3.12.13 and uv
- Docker running locally

### 1. Install and configure

From the repository root:

```sh
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
cp .env.example .env
uv run --project services/api python scripts/generate_replay_fixtures.py
pnpm exec supabase start
uv run --project services/api python scripts/setup_local.py
```

Skip the copy command if you already have a configured `.env`. The setup script writes the local database and Auth settings to `.env` and `apps/web/.env.local`.

The default demo uses cached AI responses, so no AI API key is required.

### 2. Load demo data

```sh
uv run --project services/api python scripts/seed_demo.py --scenario three-cafes --anchor now
```

This creates three cafés, two suppliers, and demo accounts. Find the generated email addresses and passwords in `.cache/demo-demo.json`.

### 3. Start the app

Run the API:

```sh
uv run --project services/api uvicorn collective.main:create_app --factory --reload --reload-dir services/api/src --host 127.0.0.1 --port 8000 --no-access-log
```

In a second terminal, run the frontend:

```sh
pnpm dev --hostname 127.0.0.1
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000), sign in with a demo account, and open the workspace. API documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

The demo uses synthetic data. Purchase orders and supplier messages are simulated.

## Development

```sh
pnpm lint       # Lint frontend and Python code
pnpm typecheck  # Check TypeScript
pnpm test       # Run unit tests and formatting checks
pnpm build      # Build the frontend
```

To create a fresh demo after the current run expires or its orders are completed, rerun the seed command with `--run demo-2`. Credentials will be saved to `.cache/demo-demo-2.json`.
