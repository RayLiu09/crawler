# Database bootstrap

## Development

The configured development server runs PostgreSQL 18.1. The application
connection applies `Asia/Shanghai` as its session timezone. The application role
can connect and create schema objects, but it does not have `CREATEDB`. Database
`crawler` has now been created; use the readiness script before migration to
confirm its current state.

A database administrator creates the database once from a maintenance database
when bootstrapping a new environment:

```sql
CREATE DATABASE crawler
    WITH OWNER = postgres_normal
         ENCODING = 'UTF8'
         TEMPLATE = template0;

ALTER DATABASE crawler SET timezone TO 'Asia/Shanghai';
```

After creation, verify that `postgres_normal` can connect to `crawler`. The
application deployment then runs `alembic upgrade head`; application startup
must not create databases or mutate the schema automatically.

Do not grant `SUPERUSER`, `CREATEDB`, or instance-management privileges to the
runtime application role merely to bypass this bootstrap step.

## Production

Production Compose initializes the `crawler` database through the official
PostgreSQL 18 image and then runs Alembic in the one-shot `migrate` service. The
PostgreSQL service is private to the Compose bridge and does not publish port
5432.
