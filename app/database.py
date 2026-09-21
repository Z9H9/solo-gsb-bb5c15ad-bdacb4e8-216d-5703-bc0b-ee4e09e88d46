"""SQLite persistence, auto-initialized on startup."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get("DB_PATH", "parser.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type TEXT    NOT NULL,
    version     TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    spec        TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    UNIQUE (device_type, version)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    spec = json.loads(row["spec"])
    return {
        "device_type": row["device_type"],
        "version": row["version"],
        "description": row["description"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "fields": spec["fields"],
        "checksum": spec["checksum"],
    }


def version_key(version: str):
    """Sort key so that 1.10.0 > 1.9.0; non-numeric parts fall back to string order."""
    parts = []
    for part in version.split("."):
        if part.isdigit():
            parts.append((0, int(part), ""))
        else:
            parts.append((1, 0, part))
    return parts


def create_definition(
    device_type: str,
    version: str,
    description: str,
    fields: List[Dict[str, Any]],
    checksum: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Returns the created record, or None if (device_type, version) already exists."""
    spec = json.dumps({"fields": fields, "checksum": checksum})
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO definitions (device_type, version, description, spec, active, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?)",
                (device_type, version, description, spec, created_at),
            )
    except sqlite3.IntegrityError:
        return None
    return get_definition(device_type, version)


def get_definition(device_type: str, version: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM definitions WHERE device_type = ? AND version = ?",
            (device_type, version),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_latest(device_type: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM definitions WHERE device_type = ?"
    if active_only:
        query += " AND active = 1"
    with _connect() as conn:
        rows = conn.execute(query, (device_type,)).fetchall()
    if not rows:
        return None
    return _row_to_dict(max(rows, key=lambda r: version_key(r["version"])))


def list_definitions(
    device_type: Optional[str] = None, include_inactive: bool = False
) -> List[Dict[str, Any]]:
    query = "SELECT * FROM definitions"
    clauses, params = [], []
    if device_type:
        clauses.append("device_type = ?")
        params.append(device_type)
    if not include_inactive:
        clauses.append("active = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY device_type, created_at"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def deactivate(device_type: str, version: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE definitions SET active = 0 WHERE device_type = ? AND version = ?",
            (device_type, version),
        )
    if cursor.rowcount == 0:
        return None
    return get_definition(device_type, version)
