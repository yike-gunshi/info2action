"""Shared dependencies for FastAPI route handlers."""
import os
import db

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_db():
    """Request-scoped DB connection (use as FastAPI dependency)."""
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()
