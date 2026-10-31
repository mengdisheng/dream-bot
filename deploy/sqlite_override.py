"""Verify stdlib sqlite3 uses the pinned library from the compiler stage."""
import sqlite3
if sqlite3.sqlite_version_info < (3, 51, 3):
    raise RuntimeError('SQLite >=3.51.3 required')
