"""Database engine, session factory, and the declarative Base that every
model in app.models attaches itself to.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import normalized_database_url, settings

engine = create_engine(normalized_database_url(settings.database_url), pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a request-scoped session and always
    closes it, even if the request raises."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
