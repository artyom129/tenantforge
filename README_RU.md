# TenantForge

[English](README.md) | **Русский**

TenantForge — production-style backend-основа для multi-tenant SaaS на Python и FastAPI.

Проект моделирует типичный B2B SaaS: пользователи состоят в организациях, работают с tenant-owned ресурсами, входят через JWT или API keys, используют тарифные лимиты и оставляют audit trail.

## Что демонстрирует проект

- async FastAPI + SQLAlchemy 2.x;
- PostgreSQL как source of truth;
- Redis для rate limiting и background jobs;
- tenant isolation непосредственно в SQL-запросах;
- JWT access tokens;
- rotating hashed refresh tokens;
- RBAC с ролями OWNER / ADMIN / MEMBER;
- invitations;
- API keys, plaintext которых не хранится;
- plan quotas и usage accounting;
- signed и idempotent billing webhooks;
- audit logs;
- structured logging и request IDs;
- Prometheus metrics;
- Alembic migrations;
- integration tests;
- Docker Compose и GitHub Actions.

## Multi-tenancy

Граница tenant'а — организация. Все tenant-owned таблицы содержат `organization_id`, а запросы к таким ресурсам включают его в SQL filter. Приложение не загружает произвольный объект, чтобы затем проверить tenant в Python.

Для пользователя, который не является участником организации, tenant-scoped lookup возвращает `404`, что уменьшает риск existence oracle между организациями.

## Authentication

Пароли хешируются через Argon2. Login выдаёт короткоживущий JWT access token и более долгий opaque refresh token. В базе хранится только SHA-256 hash refresh token.

Refresh token ротируется внутри database transaction с row lock: старый token сначала revoke'ится, затем выдаётся новый.

## RBAC

Основные роли:

- **OWNER** — полный контроль организации, API keys, billing, members;
- **ADMIN** — управление проектами и ограниченное управление участниками;
- **MEMBER** — работа с обычными tenant resources.

Правила предотвращают удаление owner, admin → owner escalation и self-promotion обычного участника.

## API Keys

Organization owner может создавать и отзывать API keys. Полный ключ показывается только один раз. PostgreSQL хранит prefix и SHA-256 hash.

Клиент отправляет ключ через:

```text
X-API-Key: ...
```

## Usage & Quotas

Поддерживаются тарифные лимиты:

| План | API requests / месяц |
| --- | ---: |
| FREE | 1 000 |
| PRO | 50 000 |
| BUSINESS | 500 000 |

Usage write блокирует organization row, поэтому два concurrent request не могут одновременно потратить один и тот же остаток quota.

## Billing Webhooks

`POST /webhooks/billing` принимает Stripe-like events и проверяет timestamped HMAC-SHA256 signature. Event IDs уникальны в БД, поэтому повторная доставка не применяет billing change второй раз.

## Background Jobs

Redis queue используется для invitation email delivery, billing webhook processing и usage rollups. Есть retries и dead-letter queue.

## Безопасность

- secrets только через environment settings;
- Argon2 для паролей;
- JWT signing через секрет;
- opaque credentials хранятся только как hashes;
- invitation tokens, refresh tokens и raw API keys не попадают в logs;
- tenant identifiers включены в SQL filters;
- billing webhook требует HMAC signature;
- error responses имеют единый безопасный формат.

## Быстрый запуск

```bash
cp .env.example .env
docker compose up --build
```

После старта:

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Metrics: `http://localhost:8000/metrics`

Создать demo-данные:

```bash
python scripts/seed_demo.py
```

или через Docker:

```bash
docker compose run --rm api python scripts/seed_demo.py
```

## Тесты

```bash
ruff check .
python -m pytest -q
```

GitHub Actions поднимает PostgreSQL и Redis, применяет migrations и запускает test suite.

## Что демонстрирует проект

TenantForge показывает архитектуру реального SaaS backend: multi-tenancy, authentication, authorization, API keys, quotas, billing integration, background jobs, observability и security-oriented engineering.
