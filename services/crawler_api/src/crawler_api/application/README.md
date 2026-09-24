# Application layer

Use cases coordinate domain policies and repository ports. Commands and queries
must not contain FastAPI request objects or SQLAlchemy models.

`ports.py` defines the persistence boundary in domain terms. Use cases receive a
`UnitOfWork` factory, enter one unit per command/query transaction, and explicitly
commit successful commands. Infrastructure adapters own all row/entity conversion.
