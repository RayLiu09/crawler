# PostgreSQL persistence

This package contains SQLAlchemy 2.0 mappings for all 11 crawler tables, focused
repositories for the current use cases, an explicit-commit async unit of work, and
Alembic migrations. Development connects to an existing PostgreSQL service;
production runs PostgreSQL as a separate container with a persistent volume on the
control-center server.

Alembic is the only schema authority. Application startup must never call
`Base.metadata.create_all()`. Repository methods convert persistence rows to domain
entities so SQLAlchemy models do not cross into application use cases. A unit of
work must be entered before repository access; callers explicitly invoke
`commit()`, while exceptions and clean exits without a commit roll back.

The current repository set covers API clients, collection Agents, browser Profile
upsert/list, collection jobs, collection tasks, lease assignment, and immutable
task attempts. Claim candidates lock the job before the task and use
`FOR UPDATE SKIP LOCKED`; this ordering is shared with job control operations to
avoid inverse job/task lock acquisition. Event, parsed-record, lease-completion,
and Outbox repositories are added with their owning use cases.
