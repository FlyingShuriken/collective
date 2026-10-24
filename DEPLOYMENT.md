# Deploy Orbyte Collective

This guide deploys the current project to **Supabase Cloud + Render + Vercel**. Run terminal commands from the repository root unless stated otherwise. Replace every `<PLACEHOLDER>` before executing a command or saving an environment value.

The result is a hosted hackathon app with real sign-in, a database, private file storage and a running job worker. Purchase orders and supplier messages remain simulated, including when AI runs live. This is a deployment recipe, not a record of a completed cloud deployment.

## 1. Choose the deployment mode

| Mode | Configuration | What works |
| --- | --- | --- |
| Scripted demo | `MODEL_MODE=replay`, `EMBEDDING_MODE=replay`, `DEMO_MODE=true` | Seeded purchasing, approvals, pools, invoice maths and matching cached interpretations. No AI credentials needed. Unseen sources and dynamic draft contexts can stop with a replay miss. |
| Full live reading | `MODEL_MODE=live`, `EMBEDDING_MODE=hosted`, `DEMO_MODE=false` | Reads new supported documents and drafts messages using Gemini; product retrieval uses a separate hosted BGE-M3 endpoint. Requires steps 7 and 8. |

Start with replay to verify hosting. Then switch to live if users or judges need to upload their own sources. Replay does **not** guarantee that every RFQ/query in a newly anchored hosted run has a cached response. The local query-preloading script is local-only; use live mode for arbitrary hosted drafting.

There is one hosting limit to know upfront: the current app sends uploads through a Next.js route handler. **Vercel Functions cap request/response payloads at 4.5 MB**, even though the app accepts up to 10 MB. Use small demo documents, preferably under 4 MB, and test source rendering too. If the full 10 MB feature is required, host Next.js on a persistent Node service or implement direct private uploads first. Changing the API limit alone cannot bypass Vercel's limit. [Vercel payload documentation](https://vercel.com/docs/errors/function_payload_too_large)

## 2. Prepare accounts, tools and code

You need:

- A GitHub repository containing this project and access to publish your reviewed deployment changes.
- Supabase, Render and Vercel accounts. Use a **dedicated synthetic-demo Supabase project**, separate from real customer data.
- Node 22.23.1, pnpm 10.32.1, Python 3.12.13 and uv, as specified by the repository.
- Docker for local builds/rehearsals and the optional embeddings image.
- For live mode: a Gemini API key with usable model access/quota, plus a hosted embeddings service.

Install dependencies and check the web app:

```sh
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm contracts:check
```

`pnpm test` requires the locally generated replay files. If they are absent, first run:

```sh
uv run --project services/api python scripts/generate_replay_fixtures.py
```

For PostgreSQL integration tests, use the local setup in [README.md](README.md), stop any API/worker sharing that local database, and keep Supabase running. Never point the test suite at the hosted project.

Before creating cloud services, make sure the deployment commit includes the Dockerfile, Render configuration, migrations, sources and matching fixtures. Keep `.env*` secrets, `.cache/` credentials and private uploads out of Git. The API Dockerfile generates its own replay fixtures during the build.

## 3. Create the Supabase project

1. Create a new project for this synthetic demo. Choose a region close to the backend when available.
2. Save the project database password in your password manager.
3. Record its **project reference**: the 20 lowercase letters in its project URL.
4. Copy the project URL, **publishable key**, and backend **secret key** from the project's API settings. A personal Supabase CLI access token is a different credential.
5. Under Auth's JWT signing-key settings, make **ES256 or RS256** the current signing algorithm. If migrating from legacy HS256, complete the documented rotation, then sign in again for a fresh token. The API rejects HS256 tokens. Check the project's public JWKS at `https://<PROJECT_REF>.supabase.co/auth/v1/.well-known/jwks.json`. [Signing-key instructions](https://supabase.com/docs/guides/auth/signing-keys)
6. Keep the `collective` and `collective_private` schemas out of the public Data API's exposed schema list. The API accesses them through restricted PostgreSQL roles; do not add blanket browser grants.

## 4. Apply the database migrations

First inspect the installed CLI's supported flags:

```sh
pnpm exec supabase login --help
pnpm exec supabase link --help
pnpm exec supabase db push --help
pnpm exec supabase migration list --help
```

Then authenticate, link the **new demo project**, and inspect the migration plan:

```sh
pnpm exec supabase login
pnpm exec supabase link --project-ref <PROJECT_REF>
pnpm exec supabase db push --linked --dry-run
```

Verify the selected reference matches the project you just created. Apply and inspect:

```sh
pnpm exec supabase db push --linked
pnpm exec supabase migration list --linked
```

Use the prompted database password. Do not paste it into a committed command. Do not run a remote reset, add `--include-seed`, or ignore a migration failure. These migrations include roles, immutable records, tenant policies and Storage policies; an empty database without them cannot run the app.

In the cloud SQL Editor, verify:

```sql
select rolname, rolcanlogin, rolsuper, rolbypassrls, rolinherit
from pg_roles
where rolname in ('collective_backend', 'collective_tenant', 'collective_coordinator');

select count(*) as application_tables
from information_schema.tables
where table_schema = 'collective';
```

All three roles should exist; the backend should not be superuser, bypass RLS, or inherit privileges. It initially has no login until the next step. If role grants fail on the hosted project's PostgreSQL version, resolve the specific migration error before continuing; do not switch the app to an unrestricted `postgres` connection.

If the CLI reports a telemetry write permission error on your workstation, retry the same command with `DO_NOT_TRACK=1` prefixed. This is unrelated to the database password.

## 5. Configure the operator and restricted runtime connections

From Supabase's **Connect** dialog, copy either:

- Direct connection: `db.<PROJECT_REF>.supabase.co:5432`, user `postgres`, when your network supports it.
- **Session pooler**, port **5432**: copy the exact pooler host, user `postgres.<PROJECT_REF>`. This is the practical option for an IPv4-only network.

Use `sslmode=require` or verified TLS. Percent-encode special characters in URL passwords. Do not select the transaction pooler on port 6543 for this recipe; this app uses a persistent psycopg pool. [Connection methods and custom-role usernames](https://supabase.com/docs/guides/database/connecting-to-postgres)

Create an ignored deployment environment file on your computer:

```sh
touch .env.deploy
chmod 600 .env.deploy
```

Open it in an editor and set:

```dotenv
DEMO_MODE=true
SUPABASE_URL=https://<PROJECT_REF>.supabase.co
SUPABASE_PUBLISHABLE_KEY=<PUBLISHABLE_KEY>
SUPABASE_SECRET_KEY=<SECRET_KEY>
MIGRATION_DATABASE_URL=postgresql://postgres.<PROJECT_REF>:<ENCODED_DATABASE_PASSWORD>@<SESSION_POOLER_HOST>:5432/postgres?sslmode=require
```

For a direct connection, use the direct host and `postgres` username instead. The operator connection stays on your computer; Render gets a separate restricted connection.

Run this once from the repository root. It generates a backend password, provisions the login, checks the actual connected role and both capabilities, and saves `DATABASE_URL` to the same private file without printing it:

```sh
uv run --project services/api python - <<'PY'
import secrets
from getpass import getpass
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import dotenv_values, set_key
from scripts.seed_demo import seed_target

path = Path('.env.deploy')
env = dotenv_values(path)
ref = getpass('Confirm the dedicated demo project reference (hidden): ').strip()
operator, _ = seed_target(env, ref)
password = secrets.token_urlsafe(32)
with psycopg.connect(operator) as conn:
    conn.execute(sql.SQL('alter role collective_backend login password {}').format(sql.Literal(password)))
options = conninfo_to_dict(operator)
options['user'] = 'collective_backend.' + ref if options['host'].endswith('.pooler.supabase.com') else 'collective_backend'
options['password'] = password
runtime = make_conninfo(**options)
with psycopg.connect(runtime) as conn:
    assert conn.execute('select current_user').fetchone()[0] == 'collective_backend'
    for role in ('collective_tenant', 'collective_coordinator'):
        conn.execute(sql.SQL('set local role {}').format(sql.Identifier(role)))
        assert conn.execute('select current_user').fetchone()[0] == role
        conn.execute('reset role')
set_key(path, 'DATABASE_URL', runtime)
path.chmod(0o600)
print('Restricted connection verified and saved to .env.deploy; no credentials printed.')
PY
```

Do not run that block casually after deployment: it rotates the backend password. If you rerun it, update Render's `DATABASE_URL` and redeploy immediately. The script uses a libpq connection string rather than a URL; psycopg accepts both. Copy the **value inside the dotenv quotes**, not the surrounding dotenv syntax, into Render.

The default local setup script remains local-only. It is not used for cloud provisioning.

## 6. Seed hosted accounts, private storage and the café scenario

The explicit hosted option reuses the existing synthetic scenario. It checks the project reference, HTTPS Auth/Storage URL, TLS database target and `DEMO_MODE=true` before making requests. It does not identify a customer's production project for you; use only the dedicated demo project.

```sh
uv run --project services/api python scripts/seed_demo.py \
  --scenario three-cafes \
  --run prelim-1 \
  --anchor now \
  --project-ref <PROJECT_REF> \
  --env-file .env.deploy
```

This creates five confirmed-email accounts (three owners, staff and network admin), their memberships, three cafés, suppliers, a cup specification, stock/history, needs, quotes and private sample sources. It creates or checks the **private** `collective-sources` bucket with a 10 MB file limit. Existing records are not reset.

Credentials are saved locally to:

```text
.cache/demo-<PROJECT_REF>-prelim-1.json
```

Keep that file private. Confirm the Auth users exist and Storage's `collective-sources` bucket is private in the dashboard. Do not make the bucket public to fix a source-viewing error.

The seeded ordering window lasts **24 hours** from the anchor. Seed near the rehearsal. For a fresh window or a completed scenario, rerun with a new run name, such as `prelim-2`, and use its new credentials. Rerunning an existing run does not reset its decisions. Auth/Storage and PostgreSQL are separate systems, so a failed seed can leave partially created synthetic assets; fix the cause and retry the same run, keeping its credentials file.

## 7. Set up Gemini for live mode

Skip this section for replay mode.

1. In [Google AI Studio](https://aistudio.google.com/apikey), create a key for your Google project. Enable any billing required by the models you choose.
2. In AI Studio, verify that your selected fast and strong models support text/image input and structured JSON output.
3. Record their API model IDs for `MODEL_FAST_ID` and `MODEL_STRONG_ID`. Do not assume the repository's provisional defaults are available to your account.
4. Add the key as **`GEMINI_API_KEY` on Render**, never as a `NEXT_PUBLIC_*` variable or a frontend secret.
5. After deployment, test a small synthetic invoice end to end, including source preparation and a successful reading job. Creating a key or listing a model is not proof that document extraction works.

The key is for the Gemini Developer API used by this adapter, not a Vertex service-account credential. Set project spending/quota controls appropriate to your demo. [Google key documentation](https://ai.google.dev/gemini-api/docs/api-key)

## 8. Host BGE-M3 for live matching

Skip this section for replay mode. A Gemini key does not provide this endpoint.

The adapter expects the pinned **GPUStack BGE-M3 Q4_K_M** weights, CLS pooling, normalized 1024-dimensional dense vectors, and an OpenAI-style `/v1/embeddings` response. It requires this exact model alias:

```text
gpustack/bge-m3-GGUF@2d48f1737679ad900d5c26c5aad5410e9c70fdca
```

One deployment recipe is a separate Render Docker web service running the official llama.cpp CPU server. Start with **at least 2 GB RAM**, then measure actual usage; the local benchmark observed about 784 MiB for the model process, so 512 MB is insufficient. This recipe's hosted performance has not been measured.

1. Inspect the official server image and record the image digest:

   ```sh
   docker buildx imagetools inspect ghcr.io/ggml-org/llama.cpp:server
   ```

2. Create `services/embeddings/Dockerfile` using the following recipe, replacing `<IMAGE_DIGEST>` with the `sha256:...` digest from that inspection. Pinning the image avoids a different server build appearing on the next deploy:

   ```dockerfile
   # syntax=docker/dockerfile:1
   FROM ghcr.io/ggml-org/llama.cpp:server@<IMAGE_DIGEST>
   ADD --checksum=sha256:6d39681b26c61279ac1f82db35a04a05009e94c415b51c858ff571489a82fc06 https://huggingface.co/gpustack/bge-m3-GGUF/resolve/2d48f1737679ad900d5c26c5aad5410e9c70fdca/bge-m3-Q4_K_M.gguf /models/bge-m3.gguf
   ENTRYPOINT ["/bin/sh", "-c"]
   CMD ["exec /app/llama-server --model /models/bge-m3.gguf --embeddings --pooling cls --embd-normalize 2 --gpu-layers 0 --threads 2 --threads-batch 2 --parallel 1 --ctx-size 4096 --batch-size 4096 --ubatch-size 4096 --host 0.0.0.0 --port ${PORT:-8080} --alias gpustack/bge-m3-GGUF@2d48f1737679ad900d5c26c5aad5410e9c70fdca --api-key \"${EMBEDDING_API_KEY:?Set EMBEDDING_API_KEY}\" --no-webui"]
   ```

3. Build locally with Docker before committing the new recipe. The 438 MB model is downloaded during the image build; do not commit the weights. Check the pinned runtime's `--help` if a flag is rejected.
4. Commit/push the reviewed Dockerfile. Create a Render **Docker Web Service** using that path, with the repository root as context, a paid always-running instance and `/health` as its health-check path.
5. Generate a random `EMBEDDING_API_KEY` in your password manager and set it on this service. The service must refuse unauthenticated embedding requests.
6. Save its HTTPS endpoint: `https://<EMBEDDINGS_SERVICE>.onrender.com/v1/embeddings`.
7. Use that URL and the same key on the API service in step 9.

Validate the response through the **application's own adapter**, which checks alias, indices, vector dimensions and finite numbers. This prompts for the key without echoing it:

```sh
uv run --project services/api python - <<'PY'
import asyncio
from getpass import getpass
from collective.adapters.embeddings import EmbeddingGateway, REVISION
from collective.settings import Settings

async def main():
    url = getpass('HTTPS embeddings URL, including /v1/embeddings (hidden): ').strip()
    key = getpass('Embeddings API key: ')
    settings = Settings(_env_file=None, embedding_mode='hosted', embedding_url=url, embedding_api_key=key)
    result = await EmbeddingGateway(settings).encode(['250ml paper cup'], REVISION)
    assert len(result.vectors) == 1 and len(result.vectors[0]) == 1024
    print('Hosted embedding response passed the application adapter checks.')

asyncio.run(main())
PY
```

Do not substitute Gemini/OpenAI embeddings or a differently quantized model under this alias. [Official server image](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md), [pinned weight artifact](https://huggingface.co/gpustack/bge-m3-GGUF/tree/2d48f1737679ad900d5c26c5aad5410e9c70fdca).

## 9. Deploy the API and embedded worker on Render

Create a **Web Service** connected to the repository:

| Setting | Value |
| --- | --- |
| Runtime / language | Docker |
| Root / build context | Repository root |
| Dockerfile path | `services/api/Dockerfile` |
| Instance | Paid, always running; one instance initially |
| Health-check path | `/v1/ready` |
| Start command | Leave the Dockerfile default |
| Auto-deploy | Off during the demo, or controlled after your checks pass |

Add these variables in Render's Environment settings:

```dotenv
DATABASE_URL=<RESTRICTED_CONNECTION_FROM_STEP_5>
SUPABASE_URL=https://<PROJECT_REF>.supabase.co
SUPABASE_PUBLISHABLE_KEY=<PUBLISHABLE_KEY>
SUPABASE_SECRET_KEY=<SECRET_KEY>
SUPABASE_JWT_ISSUER=https://<PROJECT_REF>.supabase.co/auth/v1
SUPABASE_JWT_AUDIENCE=authenticated
ENABLE_WORKER=true
MODEL_PROVIDER=gemini
```

For **replay**, also set:

```dotenv
DEMO_MODE=true
MODEL_MODE=replay
EMBEDDING_MODE=replay
MODEL_FAST_ID=gemini-3.8-flash
MODEL_STRONG_ID=gemini-4-argon
EMBEDDING_MODEL_REVISION=2d48f1737679ad900d5c26c5aad5410e9c70fdca
```

These model IDs identify authored replay records; they do not establish live model availability. Keep them consistent with the fixture generator.

For **live**, instead set:

```dotenv
DEMO_MODE=false
MODEL_MODE=live
MODEL_FAST_ID=<VERIFIED_FAST_MODEL_ID>
MODEL_STRONG_ID=<VERIFIED_STRONG_MODEL_ID>
GEMINI_API_KEY=<GOOGLE_KEY>
EMBEDDING_MODE=hosted
EMBEDDING_URL=https://<EMBEDDINGS_SERVICE>.onrender.com/v1/embeddings
EMBEDDING_API_KEY=<EMBEDDINGS_KEY>
EMBEDDING_MODEL_REVISION=2d48f1737679ad900d5c26c5aad5410e9c70fdca
```

The local `.env.deploy` remains `DEMO_MODE=true` for explicit synthetic seeding even if the hosted API runs live. These are different environments. Do not upload that operator file to Render.

The existing `render.yaml` describes live mode. If using **Blueprint** deployment instead of the manual settings above, review its instance SKU and override mode/model settings to match your chosen mode. Do not assume its plan label is available to your account. The Dockerfile reads Render's `PORT`; no custom port command is needed.

Deploy and check:

```sh
curl --fail https://<API_SERVICE>.onrender.com/v1/health
curl --fail https://<API_SERVICE>.onrender.com/v1/ready
```

Both should return `{"status":"ok"}`. Readiness verifies database access but does not prove a successful Gemini, embeddings or Storage request. Check Render logs for successful startup and a running worker. No separate worker service is needed for this small deployment.

Render supports Docker builds from the repository. Free web services sleep after inactivity, which interrupts this embedded worker; use an always-running instance for the demo. [Docker setup](https://render.com/docs/docker), [free-service limits](https://render.com/docs/free).

## 10. Deploy the frontend on Vercel

1. Import the same Git repository into Vercel.
2. Select the **Next.js** framework preset and set **Root Directory** to `apps/web`.
3. Enable access to source files outside the root directory if the project settings offer that option, so the workspace lockfile/dependencies remain available.
4. Use Node **22.x** and the repository's pinned pnpm version. Build from the app root with `pnpm build`; leave Next.js output detection automatic. Verify the install log finds the root `pnpm-lock.yaml` and workspace.
5. Add the following **Production** environment variables:

   ```dotenv
   COLLECTIVE_API_URL=https://<API_SERVICE>.onrender.com
   NEXT_PUBLIC_SUPABASE_URL=https://<PROJECT_REF>.supabase.co
   NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=<PUBLISHABLE_KEY>
   ```

6. Deploy and save the stable production URL, such as `https://<APP>.vercel.app`.

Use the API **origin**, without `/v1`, credentials or query parameters. The Next.js proxy supplies authenticated requests to the API. Do not add a browser-wide API CORS exception as a substitute for this proxy.

No database password, Supabase secret key, Gemini key or embeddings key belongs in Vercel's browser-visible environment. Redeploy after changing `NEXT_PUBLIC_*` values because they are used in the frontend build. Keep Preview deployments on a separate demo backend/database if you enable them; otherwise leave Preview unconfigured.

[Vercel monorepo deployment](https://vercel.com/docs/monorepos).

## 11. Finish Auth URL configuration

In Supabase **Authentication → URL Configuration**:

1. Set **Site URL** to the stable Vercel production origin.
2. Add the exact redirect URLs used by any email/reset flows you enable. The current app uses email/password sign-in, rather than a custom OAuth callback.
3. If you attach a custom domain, update these settings and test the custom domain too.
4. Use the seed-generated, confirmed-email demo accounts. There is no public signup UI and no SMTP setup is needed for those seeded accounts.

Open `/en/sign-in`, enter the owner-a credentials from the hosted seed file and choose **Open workspace**. Do not use the local seed's credentials: hosted credentials are stored separately.

## 12. Verify the hosted workflow before sharing it

Follow [USAGE.md](USAGE.md) against the hosted URL. Use separate browser profiles or sign out between café owners.

Check all of these:

- Owner sign-in works, and the expected café and quotes appear.
- Staff cannot approve an order; restricted staff do not see prices or private sources.
- A small source uploads, is privately readable and can be prepared.
- In replay mode, a declared source produces its cached proposal and an unseen source stops honestly. In live mode, actual reading and product matching jobs succeed.
- A human correction and record confirmation persist after reload.
- The three-café pool shows each owner's own cost. A withdrawal recalculates the remaining offers and voids an insufficient cap.
- Fresh approval is required before issuing separate simulated POs. Retrying does not create a duplicate.
- A delivered invoice checks against the issued terms; a query alone records no recovered money.
- Live RFQ/query drafting works if live mode is enabled; review exact text and recipient before simulated Send.
- Activity and background-task states update; English, Malay and Chinese navigation works on mobile.

Run the read-only API smoke script:

```sh
uv run --project services/api python scripts/smoke_staging.py \
  --base-url https://<API_SERVICE>.onrender.com
```

Without an owner access token, **exit code 2 / pending is expected**; health checks are not complete acceptance. To exercise the authenticated checks without placing a token in shell history:

```sh
uv run --project services/api python - <<'PY'
import os
import subprocess
import sys
from getpass import getpass

url = getpass('API origin (hidden): ').strip()
token = getpass('Current hosted owner access token: ')
result = subprocess.run(
    [sys.executable, 'scripts/smoke_staging.py', '--base-url', url],
    env=os.environ | {'COLLECTIVE_STAGING_TOKEN': token},
)
raise SystemExit(result.returncode)
PY
```

You can obtain your own short-lived access token from the signed-in browser's developer tools: inspect a Supabase auth token response or its `sb-...-auth-token` browser session entry. Copy only `access_token`, not the refresh token; do not share or screenshot it. The script may still report pending until the staged pool/PO has been created. Exit 0 means its limited checks passed, not that every feature was tested.

Record the deployed Git revision, service URLs, chosen mode, successful checks and remaining limitations before submitting the link.

## 13. Updates and troubleshooting

For later changes: review the migration diff, apply compatible migrations first, deploy the API, deploy the frontend, and rerun the affected hosted checks. Do not assume a code rollback reverses a database migration. Keep service secrets in their dashboards/password manager.

| Symptom | Check |
| --- | --- |
| API fails startup / readiness | Restricted `collective_backend` credentials, TLS, port 5432, migration completion and role grants. Confirm the custom pooler username includes the project reference. |
| Sign-in succeeds but workspace requests return 401 | Current ES256/RS256 signing key, fresh sign-in, matching issuer/audience/project, and API access to the JWKS endpoint. |
| No assigned workspace | Seed Auth IDs and business memberships in the same project; verify you used the hosted credential file. |
| Missing/expired needs or unavailable pool | Seed a new run close to the demo. The original order window is 24 hours; do not alter issued orders to revive it. |
| Source upload/view fails | Private bucket exists, Storage policies applied, backend secret belongs to this project, source ownership is correct. On Vercel, also check request/response payload size. |
| `413 FUNCTION_PAYLOAD_TOO_LARGE` | Use a smaller source/rendered response. Full 10 MB uploads require a hosting or upload-flow change. |
| Job stays queued | `ENABLE_WORKER=true`, always-running API instance, worker logs and database access. |
| `REPLAY_MISS` / embedding replay miss | Source, prompt/model identity or draft context differs from the cache. Use supported examples or configure live mode; no automatic live fallback exists. |
| Gemini error | Valid backend key, actual model IDs, structured-output support, billing/quota and provider availability. Inspect private logs without sharing document content or keys. |
| Embedding error | Correct HTTPS URL including `/v1/embeddings`, shared key, exact alias/revision, pinned weights and 1024-dimensional response. Run the adapter probe in step 8. |
| Vercel build fails | App root, monorepo source access, Node/pnpm versions, workspace install and public environment variables. |
| Browser mutation returns 403 | Use the normal app origin and proxy. Check forwarded host/protocol if adding another reverse proxy; don't remove the same-origin guard. |

This guide does not provision a real supplier channel or payments. Successful live calls are operational checks, not the planned 30-document model-accuracy evaluation or field-validation evidence.
