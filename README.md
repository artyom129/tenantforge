# TenantForge

TenantForge is a production-style multi-tenant SaaS backend built with Python and
FastAPI. It models the foundation of a B2B product: users join organizations, work
with organization-owned resources, authenticate with JWTs or API keys, consume
plan quotas, and generate auditable changes.

The repository is deliberately compact. PostgreSQL is the source of truth, Redis
handles short-lived coordination, and authorization rules stay close to the
queries they protect.

## What it demonstrates

- async FastAPI and SQLAlchemy 2.x request handling;
- tenant isolation in database queries, not after records are loaded;
- JWT access tokens, hashed rotating refresh tokens, and revocation;
- organization membership with `OWNER`, `ADMIN`, and `MEMBER` roles;
- one-time invitations and API keys whose plaintext is not stored;
- Redis-backed rate limits, a small job queue, retries, and a dead-letter queue;
- plan usage accounting and quota checks;
- idempotent, signed billing webhooks;
- structured request logs, request IDs, health probes, and Prometheus metrics;
- Alembic migrations, integration tests, Docker Compose, and GitHub Actions.

## Architecture

```text
Client
  |
  v
FastAPI
  |
  +------ PostgreSQL
  |         +-- Users, organizations, resources
  |         +-- Usage, subscriptions, audit events
  |
  +------ Redis
  |         +-- Rate-limit counters
  |         +-- Job queue and dead-letter queue
  |                    |
  |                    v
  +----------------- Worker
```

Routes validate transport input and resolve authentication. Small services own
business transactions. SQLAlchemy statements carry tenant and authorization
filters into PostgreSQL. Redis remains disposable infrastructure; durable product
state stays in PostgreSQL.

The API process applies Alembic migrations as a controlled Docker startup step.
Application startup itself never calls `Base.metadata.create_all()`.

## Multi-tenancy

An organization is the tenant boundary. Every tenant-owned table includes an
`organization_id`, and tenant-owned resources are queried with
`organization_id` as part of the database filter. A project lookup therefore
matches both project ID and organization ID in one statement. The API does not
load an arbitrary project and then check its tenant in Python.

Membership is unique on `(user_id, organization_id)`. Reusable dependencies load
the current membership for the requested organization before a route executes.
The same scoping is applied to projects, memberships, API keys, subscriptions,
usage, and audit records, preventing ID-based cross-tenant access through normal
API use.

Tenant-scoped lookups return `404` when the caller is not a member, avoiding an
existence oracle across organizations. Deleting an organization cascades its
tenant-owned records but does not delete user accounts.

## Authentication

Passwords are hashed with Argon2 through `pwdlib`. Login returns a short-lived
signed JWT access token and a longer-lived opaque refresh token. Only a SHA-256
hash of each refresh token is stored.

Refresh uses a database transaction and row lock. The presented token is revoked
before a replacement pair is issued, so a rotated token cannot be used again.
Logout revokes the matching refresh token. JWT validation checks signature,
token type, subject, and expiry, and inactive users cannot authenticate.
Invitation acceptance requires an authenticated account whose normalized email
matches the invitation. Expired and already-used tokens are rejected, and the
membership plus `accepted_at` update commit atomically.

Authentication endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/register` | Create a user account |
| `POST` | `/auth/login` | Issue an access/refresh pair |
| `POST` | `/auth/refresh` | Rotate a refresh token |
| `POST` | `/auth/logout` | Revoke a refresh token |
| `GET` | `/auth/me` | Read the authenticated user |

## RBAC

Permissions are expressed with a small role dependency plus explicit rules for
operations where role ordering is not enough.

| Capability | OWNER | ADMIN | MEMBER |
| --- | :---: | :---: | :---: |
| Read organization and projects | Yes | Yes | Yes |
| Change organization settings | Yes | Yes | No |
| Create and manage projects | Yes | Yes | Yes |
| Invite a member | Yes | Member role only | No |
| Change roles or remove members | Yes | Limited | No |
| Manage API keys | Yes | No | No |
| Read audit log | Yes | Yes | No |
| Manage subscription | Yes | No | No |
| Delete organization | Yes | No | No |

The limited admin membership operations reject changes to an owner and any
attempt to grant the owner role. A member cannot elevate their own membership.

## API Keys

Organization owners can create, list, and revoke API keys. A generated key uses
the `tf_live_` prefix and is returned only in the creation response. PostgreSQL
stores its prefix for identification and a SHA-256 hash for authentication; the
full key is never persisted.

Clients send the key in `X-API-Key`. Revoked keys are rejected, and a successful
request updates `last_used_at`. API-key traffic has a separate Redis-backed rate
limit from user JWT traffic.

## Usage & Quotas

Usage events are append-only records scoped to an organization. The usage service
sums the current subscription period before a quota-controlled operation and
rejects work that would exceed the plan limit. `POST
/organizations/{organization_id}/usage/events` records between 1 and 1,000 API
requests per call. It requires the raw organization API key in `X-API-Key`; a key
from another organization receives the same `404` response as an unknown tenant.
Every new organization receives an active `FREE` subscription in the same
transaction as its owner membership.

| Plan | Monthly API requests |
| --- | ---: |
| `FREE` | 1,000 |
| `PRO` | 50,000 |
| `BUSINESS` | 500,000 |

`GET /organizations/{organization_id}/usage` uses JWT membership authentication
and reports the plan, used amount, limit, remaining amount, and exact period
boundaries. Usage writes lock the organization row while checking the current
total and inserting the event, so concurrent requests cannot both spend the same
remaining quota.

The API-key rate limit is checked before quota accounting. Exceeding the
per-minute limit returns HTTP `429` with `rate_limit_exceeded` and a `Retry-After`
value for the Redis fixed window. Exceeding the monthly plan quota also returns
HTTP `429`, with `quota_exceeded` and `Retry-After` set to the remaining seconds
in the subscription period. A rejected quota write does not insert a usage event.

## Billing Webhooks

Billing is a development adapter, not real payment processing. Owners can inspect
a subscription and change its plan through:

- `GET /organizations/{organization_id}/subscription`
- `POST /organizations/{organization_id}/subscription/change-plan`

The plan-change endpoint is enabled only when `APP_ENV` is `development` or
`test`; other environments return `404` and must use provider webhooks.

`POST /webhooks/billing` accepts Stripe-like subscription and invoice events.
The handler verifies the timestamped HMAC-SHA256 value in `Stripe-Signature`
before accepting input. The supported event types are `subscription.created`, `subscription.updated`,
`subscription.cancelled`, and `invoice.payment_failed`.

## Background Jobs

Redis lists provide a deliberately small queue for invitation email delivery,
billing webhook processing, and usage rollups. Jobs use `tenantforge:jobs`, and
exhausted jobs move to `tenantforge:jobs:dead`. The worker uses blocking reads and runs with:

```bash
python -m app.workers.worker
```

A failed job is retried up to three times with 1, 2, and 4 second delays. Jobs
move through an in-flight list and are acknowledged only after successful handling;
a single worker recovers unfinished jobs when it starts. Jobs that exhaust the
retry budget move to a dead-letter queue, where credential-like payload fields are
redacted. The invitation flow hands the one-time token to an email adapter only
in memory. The development adapter queues non-secret delivery metadata, and the
worker writes that metadata to structured logs without storing or logging the
invitation token. A provider adapter can use the same method to deliver the link
directly.

## Idempotency

Idempotency is enforced at the persistence boundary:

- `(provider, external_event_id)` is unique for billing webhook events;
- webhook processing locks its event and commits billing changes with the
  `PROCESSED` marker in one transaction;
- invitation acceptance locks its row and records `accepted_at` atomically with
  membership creation;
- refresh rotation locks and revokes the old token in the issuing transaction;
- membership and organization subscription uniqueness constraints close races
  that application-level checks alone cannot prevent.

Repeated webhook delivery returns the existing result without reapplying billing
changes.

## Security Decisions

- Secrets come from environment settings and default values are rejected when
  `APP_ENV=production`.
- Passwords use Argon2; JWT signing uses PyJWT; opaque credentials use
  cryptographically secure random bytes and store hashes only.
- Invitation tokens, refresh tokens, and full API keys are excluded from logs.
- Tenant identifiers are part of SQL filters for tenant-owned reads and writes.
- RBAC prevents owner removal, admin-to-owner escalation, and member self-promotion.
- Redis fixed-window limits return HTTP `429` with `Retry-After`.
- Billing requests require an HMAC signature and event IDs are database-unique.
- Validation errors and application errors use one response shape and do not
  expose server tracebacks.

For a real deployment, set unique high-entropy values for `JWT_SECRET` and
`BILLING_WEBHOOK_SECRET`, terminate TLS at a trusted proxy, and restrict database
and Redis network access.

## Observability

Every request receives an `X-Request-ID`. A valid incoming value is preserved;
otherwise the API creates a UUID. The response carries the same ID, and the
structured completion log includes method, normalized path, status code, and
duration. Credentials and request bodies are not logged.

`GET /metrics` exposes Prometheus counters and histograms for HTTP traffic,
background jobs, job failures, and billing webhooks. Labels exclude user IDs,
organization IDs, raw URLs, and request IDs to avoid high-cardinality metrics.

Health endpoints serve different operational purposes:

- `GET /health/live` confirms that the process is running;
- `GET /health/ready` checks PostgreSQL and Redis and returns a non-2xx status if
  either dependency is unavailable.

## Tech Stack

- Python 3.12, FastAPI, Pydantic v2, Uvicorn
- SQLAlchemy 2.x async, asyncpg, PostgreSQL 16, Alembic
- Redis 7 for rate limiting and background jobs
- PyJWT, pwdlib with Argon2
- structlog and prometheus-client
- pytest, pytest-asyncio, HTTPX, Ruff
- Docker Compose and GitHub Actions

## Quick Start

The shortest path uses Docker and requires Docker Engine with Compose:

```bash
cp .env.example .env
docker compose up --build
```

On Windows PowerShell, the copy command is:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

After the health checks pass:

- API: <http://localhost:8000>
- Swagger UI: <http://localhost:8000/docs>
- OpenAPI document: <http://localhost:8000/openapi.json>
- Prometheus metrics: <http://localhost:8000/metrics>

Change both development secrets in `.env` before sharing an environment. Docker
Compose uses explicit container-network database and Redis addresses, while the
sample URLs with `localhost` support commands run directly on the host.

## Docker

The Compose stack contains `api`, `worker`, `postgres`, and `redis`. PostgreSQL
and Redis must pass their native health checks before the API starts. The API
applies `alembic upgrade head`, starts Uvicorn, and becomes healthy before the
worker starts.

```bash
docker compose up --build
docker compose logs -f api worker
docker compose down
```

PostgreSQL and Redis data live in named volumes. To intentionally remove local
development data as well as the containers, run `docker compose down --volumes`.

## Database Migrations

For host-based development, ensure `DATABASE_URL` points to the running database,
then use:

```bash
alembic upgrade head
alembic current
alembic history
alembic downgrade -1
```

After changing SQLAlchemy models, create and inspect a revision before applying
it:

```bash
alembic revision --autogenerate -m "describe schema change"
alembic upgrade head
```

The initial migration creates every table, foreign key, uniqueness constraint,
and tenant lookup index and supports a complete downgrade.

## Seed Demo

Apply migrations first, then create the idempotent demo dataset:

```bash
python scripts/seed_demo.py
```

With Docker:

```bash
docker compose run --rm api python scripts/seed_demo.py
```

The seed creates Acme Labs, three memberships, three projects, and a `PRO`
subscription. These credentials are for local development only:

| Role | Email | Password |
| --- | --- | --- |
| OWNER | `owner@tenantforge.local` | `tenantforge-demo` |
| ADMIN | `admin@tenantforge.local` | `tenantforge-demo` |
| MEMBER | `member@tenantforge.local` | `tenantforge-demo` |

Running the script again reuses records identified by email, organization slug,
membership pair, and project name.

## Running Tests

Create Python 3.12 environment and install the package with development tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check .
python -m pytest -q
```

Windows PowerShell uses the same tools without Make:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ruff check .
python -m pytest -q
alembic upgrade head
python scripts/seed_demo.py
```

Coverage is available with `python -m pytest --cov=app --cov-report=term-missing`.
The GitHub Actions workflow runs Ruff, applies migrations to PostgreSQL, and runs
the test suite with PostgreSQL and Redis service containers.

For the same integration path locally, start PostgreSQL and Redis, apply the
migration, and select the external services explicitly:

```bash
TEST_USE_EXTERNAL_SERVICES=1 \
DATABASE_URL=postgresql+asyncpg://tenantforge:tenantforge@localhost:5432/tenantforge_test \
REDIS_URL=redis://localhost:6379/0 \
python -m pytest -q
```

```powershell
$env:TEST_USE_EXTERNAL_SERVICES = "1"
$env:DATABASE_URL = "postgresql+asyncpg://tenantforge:tenantforge@localhost:5432/tenantforge_test"
$env:REDIS_URL = "redis://localhost:6379/0"
python -m pytest -q
```

This mode runs the PostgreSQL row-lock and concurrent-idempotency tests that are
skipped by the fast SQLite/FakeRedis fallback.

If Make is installed, `make lint`, `make test`, `make migrate`, `make seed`,
`make dev`, and `make worker` are shortcuts for the same commands.

## API Examples

Register and obtain a token pair:

```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"developer@example.com","password":"correct-horse-battery-staple"}'

TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"developer@example.com","password":"correct-horse-battery-staple"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
```

Create an organization, retain its ID, and list the current user's organizations:

```bash
ORGANIZATION_ID=$(curl -s -X POST http://localhost:8000/organizations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Example Labs","slug":"example-labs"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['id'])")

curl http://localhost:8000/organizations \
  -H "Authorization: Bearer $TOKEN"
```

For an organization returned by that call, create a project:

```bash
curl -X POST "http://localhost:8000/organizations/$ORGANIZATION_ID/projects" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Customer Portal","description":"Tenant-scoped portal backend"}'
```

Create an API key as the organization owner, then record 25 API requests. The
full key is present only in its creation response:

```bash
API_KEY=$(curl -s -X POST \
  "http://localhost:8000/organizations/$ORGANIZATION_ID/api-keys" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"usage-ingest"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['key'])")

curl -X POST \
  "http://localhost:8000/organizations/$ORGANIZATION_ID/usage/events" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"quantity":25}'
```

The successful response has status `201` and identifies the durable event with
`metric` set to `api_requests`, the accepted `quantity`, and its creation time.

The full endpoint catalog and request schemas are available in Swagger UI. Main
resource routes are:

| Area | Endpoints |
| --- | --- |
| Organizations | `POST/GET /organizations`, `GET/PATCH/DELETE /organizations/{organization_id}` |
| Members | `GET /organizations/{organization_id}/members`, `PATCH/DELETE /organizations/{organization_id}/members/{membership_id}` |
| Invitations | `POST /organizations/{organization_id}/invitations`, `POST /invitations/{token}/accept` |
| API keys | `POST/GET /organizations/{organization_id}/api-keys`, `DELETE /organizations/{organization_id}/api-keys/{key_id}` |
| Projects | `POST/GET /organizations/{organization_id}/projects`, `GET/PATCH/DELETE /organizations/{organization_id}/projects/{project_id}` |
| Usage | `GET /organizations/{organization_id}/usage`, `POST /organizations/{organization_id}/usage/events` |
| Billing | `GET /organizations/{organization_id}/subscription`, `POST /organizations/{organization_id}/subscription/change-plan` |
| Audit | `GET /organizations/{organization_id}/audit-log` |

## Project Structure

```text
app/
  api/                FastAPI dependencies, router, and resource routes
  core/               errors, request logging, metrics, shared constants
  infrastructure/     Redis client, fixed-window rate limiter, job queue
  models/             SQLAlchemy identity, tenancy, resources, billing, audit
  schemas/            Pydantic request and response models
  security/           password, JWT, opaque-token, and permission helpers
  services/           business operations and transaction boundaries
  workers/            job handlers and worker process
  config.py           environment-backed settings
  database.py         async engine and session lifecycle
  main.py             application assembly
alembic/               migration environment and schema revisions
scripts/seed_demo.py   repeatable development dataset
tests/                 API, service, security, and infrastructure tests
docker-compose.yml     API, worker, PostgreSQL, and Redis stack
```

## Trade-offs

- Tenant filtering is explicit in application queries. This is easy to review and
  test; PostgreSQL row-level security would add another defense layer but also
  requires connection-level tenant context and more operational care.
- The rate limiter uses a fixed window. It is transparent and atomic in Redis but
  allows bursts near a window boundary.
- The Redis list queue has retry and dead-letter behavior without a framework.
  Its in-flight recovery assumes one worker process; multiple workers would need
  per-worker ownership and expiring leases. It does not provide scheduling or
  workflow orchestration.
- Usage totals are computed from durable events. This keeps the source data
  inspectable but eventually calls for rollups as event volume grows.
- The billing and email adapters model integration boundaries without charging
  cards or sending email, keeping local development deterministic.

## Future Improvements

- Add PostgreSQL row-level security as defense in depth.
- Replace development billing and email adapters with provider implementations.
- Introduce an outbox for guaranteed database-to-queue publication.
- Add OpenTelemetry traces and deployment-specific dashboards.
- Roll up high-volume usage events while preserving an auditable source stream.
- Add organization SSO, MFA, and key rotation policies.

## License

TenantForge is available under the [MIT License](LICENSE).