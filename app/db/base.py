from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared ORM metadata used by application models and Alembic."""
