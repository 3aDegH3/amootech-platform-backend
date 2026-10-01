# Amootech platform development environment

This backend repository owns the full-stack development Docker Compose configuration
for convenience and version control. The backend and frontend remain separate
applications in separate Git repositories; this Compose file only orchestrates
their local development environment with PostgreSQL. Keep both repositories in
the same parent directory:

```text
amootech/
├── amootech-platform-backend/
└── amootech-platform-frontend/
```

## Start

Copy `.env.example` to `.env`, replace its development-only placeholders, then
run all commands from this repository.

```bash
docker compose up --build
docker compose up -d --build
```

- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- Health endpoint: http://localhost:8000/api/v1/health/
- OpenAPI schema: http://localhost:8000/api/v1/schema/
- Swagger UI: http://localhost:8000/api/v1/docs/

The browser-facing `NEXT_PUBLIC_API_BASE_URL` defaults to
`http://localhost:8000/api/v1`. The database hostname inside Compose is `db`.
PostgreSQL is intentionally not published to the host.

## Common commands

```bash
docker compose down
docker compose logs -f
docker compose logs -f backend
docker compose logs -f frontend
docker compose exec backend python manage.py <command>
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py seed_academics
docker compose exec backend python manage.py test
docker compose exec frontend npm run lint
```

`docker compose down` removes containers and the network but preserves database
data in the `postgres_data` named volume. Use `docker compose down -v` only when
you explicitly want to delete local database data and the other Compose volumes.

## Authentication foundation

The project uses the `accounts.User` custom user model and JWT endpoints under
`/api/v1/auth/`. Public registration is intentionally not available. After a
fresh database is created, apply migrations explicitly with the command above.
Tests create and destroy a separate PostgreSQL test database.

Run `seed_academics` after migrations to load grade 10–12 planning subjects,
chapters, and representative topics for mathematics, experimental sciences,
and humanities. The command is safe to rerun and leaves existing records intact.


## Telegram Sprint 7 automation and mutation source

The backend remains business authority. Service-token protected automation endpoints are
`GET /api/internal/v1/telegram/automation/morning-recipients/?date=YYYY-MM-DD` and
`GET /api/internal/v1/telegram/automation/end-of-day-recipients/?date=YYYY-MM-DD`.
They return eligible Telegram chat/user identities only. Evening reuses the shared
Daily Report Close Day review and excludes finalized/resolved days; neither listing
creates business records.

Published plan/day/item saves/deletes and existing reorder/copy/move paths register a
best-effort `transaction.on_commit` refresh using the existing Bot Control client.
Configure `TELEGRAM_BOT_CONTROL_BASE_URL` and `TELEGRAM_BOT_CONTROL_TOKEN` as in Sprint 6.
A failed bot refresh logs a safe warning and does not roll back the planning mutation;
Admin Resend Plan remains the fallback. Draft edits do not notify. Bot content hashes
suppress unchanged refreshes. The HTTP call is synchronous after commit and has the
existing 10-second timeout; bulk planning edits may trigger several no-op refresh calls.
No outbox or queue is introduced.

`PlanItemExecution.source`, `DailyReportItem.source` and `DailyReport.source` use common
`WEB`/`TELEGRAM` choices with **last successful mutation channel** semantics. Existing
rows and untouched/materialized rows default WEB. Website writes set WEB; Telegram
execution, report metadata patch, extra activity and finalization set TELEGRAM on the
record being mutated. Read/open operations and unchanged finalization do not change
existing sources. Item writes do not change the parent report's source; that field
tracks report metadata/finalization. Repeated execution commands can set the invoking
channel while keeping the business status idempotent. Clients cannot choose the source.
Counselor execution/report/detail APIs expose these fields; the frontend report detail
shows separate source badges for report/item details and execution status.

Apply `python manage.py migrate` before running the new code. The Telegram execution
mutation now wraps the execution and report values in one transaction, so an invalid
performance payload cannot leave a partially saved execution/source. Finalization is
serialized with the existing student/report row locks.

See the bot's [production guide](../amootech-telegram-bot/docs/PRODUCTION.md) for the
independent operational PostgreSQL, HTTPS/webhook setup, scheduler and backups.
