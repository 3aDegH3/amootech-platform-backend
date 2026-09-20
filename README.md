# Amootech platform development environment

This repository owns the Docker Compose environment for the Amootech backend,
the sibling frontend repository, and PostgreSQL. Keep both repositories in the
same parent directory:

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
