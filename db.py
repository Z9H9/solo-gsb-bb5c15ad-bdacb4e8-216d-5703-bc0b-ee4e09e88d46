"""SQLite 持久化层：报文定义的存取、停用与版本查询。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from parser import DefinitionError, definition_fingerprint, version_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type   TEXT    NOT NULL,
    version       TEXT    NOT NULL,
    definition    TEXT    NOT NULL,
    fingerprint   TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL,
    UNIQUE(device_type, version)
);
CREATE INDEX IF NOT EXISTS idx_definitions_type_active
    ON definitions(device_type, active, id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        """自动初始化表结构（幂等）。"""
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # -- 写 ----------------------------------------------------------------

    def create_definition(self, device_type: str, version: str,
                          definition: dict[str, Any], description: str = "") -> dict[str, Any]:
        fp = definition_fingerprint(definition)
        row = {
            "device_type": device_type,
            "version": version,
            "definition": json.dumps(definition, ensure_ascii=False),
            "fingerprint": fp,
            "description": description,
            "created_at": _utcnow(),
        }
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO definitions
                       (device_type, version, definition, fingerprint,
                        description, active, created_at)
                       VALUES (:device_type, :version, :definition, :fingerprint,
                               :description, 1, :created_at)""",
                    row)
            except sqlite3.IntegrityError as e:
                raise DefinitionError(
                    f"设备类型 {device_type!r} 的版本 {version!r} 已存在"
                ) from e
            row_id = cur.lastrowid
        return self.get_definition_by_id(row_id)  # type: ignore[return-value]

    def set_active(self, device_type: str, version: str, active: bool) -> dict[str, Any] | None:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE definitions SET active = ? WHERE device_type = ? AND version = ?",
                (1 if active else 0, device_type, version))
            if cur.rowcount == 0:
                return None
        return self.get_definition(device_type, version)

    # -- 读 ----------------------------------------------------------------

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "device_type": r["device_type"],
            "version": r["version"],
            "definition": json.loads(r["definition"]),
            "fingerprint": r["fingerprint"],
            "description": r["description"],
            "active": bool(r["active"]),
            "created_at": r["created_at"],
        }

    def get_definition_by_id(self, def_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM definitions WHERE id = ?", (def_id,)).fetchone()
        return self._row_to_dict(r) if r else None

    def get_definition(self, device_type: str, version: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM definitions WHERE device_type = ? AND version = ?",
                (device_type, version)).fetchone()
        return self._row_to_dict(r) if r else None

    def list_definitions(self, device_type: str | None = None,
                         active: bool | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM definitions WHERE 1=1"
        params: list[Any] = []
        if device_type is not None:
            sql += " AND device_type = ?"
            params.append(device_type)
        if active is not None:
            sql += " AND active = ?"
            params.append(1 if active else 0)
        sql += " ORDER BY device_type, id DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_latest_version(self, device_type: str, active_only: bool = True) -> dict[str, Any] | None:
        """按语义版本号返回最新版本定义。"""
        rows = self.list_definitions(device_type, active=True if active_only else None)
        if not rows:
            return None
        return max(rows, key=lambda r: version_key(r["version"]))
