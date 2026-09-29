"""SQLite: пользователи, история скачиваний и кэш file_id."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "bot.db"


@dataclass(frozen=True)
class CachedVideo:
    video_id: str
    title: str | None
    author: str | None
    file_id: str
    file_size: int | None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                is_banned INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                file_id TEXT,
                file_size INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS urls (
                url TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                url TEXT NOT NULL,
                video_id TEXT,
                title TEXT,
                author TEXT,
                source TEXT,
                status TEXT NOT NULL,
                file_size INTEGER,
                error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_user
                ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_video
                ON downloads(video_id);
            CREATE INDEX IF NOT EXISTS idx_urls_video
                ON urls(video_id);
            """
        )


def _upsert_user(telegram_id: int, username: str | None, first_name: str | None) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen_at = excluded.last_seen_at
            """,
            (telegram_id, username, first_name, now, now),
        )


def _is_banned(telegram_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT is_banned FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return bool(row and row["is_banned"])


def _cached_from_row(row: sqlite3.Row | None) -> CachedVideo | None:
    if row is None or not row["file_id"]:
        return None
    return CachedVideo(
        video_id=row["video_id"],
        title=row["title"],
        author=row["author"],
        file_id=row["file_id"],
        file_size=row["file_size"],
    )


def _get_cached_video(url: str, video_id: str | None) -> CachedVideo | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT v.video_id, v.title, v.author, v.file_id, v.file_size
            FROM videos v
            JOIN urls u ON u.video_id = v.video_id
            WHERE u.url = ? AND v.file_id IS NOT NULL
            """,
            (url,),
        ).fetchone()
        if row is None and video_id:
            row = conn.execute(
                """
                SELECT video_id, title, author, file_id, file_size
                FROM videos
                WHERE video_id = ? AND file_id IS NOT NULL
                """,
                (video_id,),
            ).fetchone()
    return _cached_from_row(row)


def _save_video(
    video_id: str,
    title: str | None,
    author: str | None,
    file_id: str | None,
    file_size: int | None,
    url: str,
) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO videos (video_id, title, author, file_id, file_size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = COALESCE(excluded.title, videos.title),
                author = COALESCE(excluded.author, videos.author),
                file_id = COALESCE(excluded.file_id, videos.file_id),
                file_size = COALESCE(excluded.file_size, videos.file_size)
            """,
            (video_id, title, author, file_id, file_size, now),
        )
        conn.execute(
            """
            INSERT INTO urls (url, video_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET video_id = excluded.video_id
            """,
            (url, video_id, now),
        )


def _add_download(
    user_id: int,
    url: str,
    video_id: str | None,
    title: str | None,
    author: str | None,
    source: str | None,
    status: str,
    file_size: int | None,
    error: str | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO downloads (
                user_id, url, video_id, title, author, source,
                status, file_size, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                url,
                video_id,
                title,
                author,
                source,
                status,
                file_size,
                error,
                _now(),
            ),
        )


async def upsert_user(telegram_id: int, username: str | None, first_name: str | None) -> None:
    await asyncio.to_thread(_upsert_user, telegram_id, username, first_name)


async def is_banned(telegram_id: int) -> bool:
    return await asyncio.to_thread(_is_banned, telegram_id)


async def get_cached_video(url: str, video_id: str | None) -> CachedVideo | None:
    return await asyncio.to_thread(_get_cached_video, url, video_id)


async def save_video(
    video_id: str,
    title: str | None,
    author: str | None,
    file_id: str | None,
    file_size: int | None,
    url: str,
) -> None:
    await asyncio.to_thread(_save_video, video_id, title, author, file_id, file_size, url)


async def add_download(
    user_id: int,
    url: str,
    video_id: str | None,
    title: str | None,
    author: str | None,
    source: str | None,
    status: str,
    file_size: int | None = None,
    error: str | None = None,
) -> None:
    await asyncio.to_thread(
        _add_download,
        user_id,
        url,
        video_id,
        title,
        author,
        source,
        status,
        file_size,
        error,
    )
