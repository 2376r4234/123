"""Хранилище на SQLite: подписки пользователей, уже увиденные файлы и кэш расписаний."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import settings

DB_PATH = settings.data_dir / "bot.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id         INTEGER NOT NULL,
    school_id       TEXT    NOT NULL,
    group_name      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    last_message_id INTEGER,
    PRIMARY KEY (user_id, school_id)
);

CREATE TABLE IF NOT EXISTS school_files (
    school_id TEXT NOT NULL,
    file_id   TEXT NOT NULL,
    file_name TEXT NOT NULL,
    seen_at   TEXT NOT NULL,
    PRIMARY KEY (school_id, file_id)
);

CREATE TABLE IF NOT EXISTS school_cache (
    school_id   TEXT PRIMARY KEY,
    file_id     TEXT,
    file_name   TEXT,
    file_path   TEXT,
    groups_json TEXT,
    updated_at  TEXT
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}
        if "last_message_id" not in columns:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN last_message_id INTEGER")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_subscription(user_id: int, school_id: str, group_name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id, school_id, group_name, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, school_id) DO UPDATE SET group_name = excluded.group_name",
            (user_id, school_id, group_name, _now()),
        )


def delete_subscription(user_id: int, school_id: str | None = None) -> None:
    with _connect() as conn:
        if school_id is None:
            conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND school_id = ?",
                (user_id, school_id),
            )


def get_subscriptions(user_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT school_id, group_name FROM subscriptions "
            "WHERE user_id = ? ORDER BY school_id",
            (user_id,),
        ).fetchall()


def get_subscribers(school_id: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT user_id, group_name FROM subscriptions WHERE school_id = ?",
            (school_id,),
        ).fetchall()


def save_last_message(user_id: int, school_id: str, message_id: int) -> None:
    """Запоминает сообщение с расписанием, чтобы удалить его при следующей рассылке."""
    with _connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_message_id = ? WHERE user_id = ? AND school_id = ?",
            (message_id, user_id, school_id),
        )


def get_last_message(user_id: int, school_id: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_message_id FROM subscriptions WHERE user_id = ? AND school_id = ?",
            (user_id, school_id),
        ).fetchone()
    return row["last_message_id"] if row else None


def known_file_ids(school_id: str) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT file_id FROM school_files WHERE school_id = ?", (school_id,)
        ).fetchall()
    return {row["file_id"] for row in rows}


def mark_files_seen(school_id: str, files) -> None:
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO school_files (school_id, file_id, file_name, seen_at) "
            "VALUES (?, ?, ?, ?)",
            [(school_id, item.id, item.name, _now()) for item in files],
        )


def get_stats() -> list[sqlite3.Row]:
    """Возвращает статистику по школам: название школы и количество подписчиков."""
    with _connect() as conn:
        return conn.execute(
            "SELECT school_id, COUNT(user_id) as count FROM subscriptions GROUP BY school_id"
        ).fetchall()


def get_total_users_count() -> int:
    """Возвращает общее количество уникальных пользователей."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT user_id) as count FROM subscriptions"
        ).fetchone()
    return row["count"] if row else 0


def get_all_users() -> list[int]:
    """Возвращает список ID всех уникальных пользователей для рассылки."""
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT user_id FROM subscriptions").fetchall()
    return [row["user_id"] for row in rows]


def save_cache(
    school_id: str, file_id: str, file_name: str, file_path, groups: list[str]
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO school_cache (school_id, file_id, file_name, file_path, groups_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(school_id) DO UPDATE SET "
            "file_id = excluded.file_id, file_name = excluded.file_name, "
            "file_path = excluded.file_path, groups_json = excluded.groups_json, "
            "updated_at = excluded.updated_at",
            (
                school_id,
                file_id,
                file_name,
                str(file_path),
                json.dumps(groups, ensure_ascii=False),
                _now(),
            ),
        )


def get_cache(school_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT file_id, file_name, file_path, groups_json, updated_at "
            "FROM school_cache WHERE school_id = ?",
            (school_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "file_id": row["file_id"],
        "file_name": row["file_name"],
        "file_path": Path(row["file_path"]) if row["file_path"] else None,
        "groups": json.loads(row["groups_json"] or "[]"),
        "updated_at": row["updated_at"],
    }
