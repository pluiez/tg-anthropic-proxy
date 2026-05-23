import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path


def now_epoch_ms() -> int:
    return int(time.time() * 1000)


def is_cache_key(key: str) -> bool:
    return len(key) == 64 and all(char in "0123456789abcdef" for char in key)


class SqliteByteCache:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int,
        max_items: int | None = None,
        max_bytes: int | None = None,
        now_ms_fn: Callable[[], int] | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_items = None if max_items is None else max(1, int(max_items))
        self.max_bytes = None if max_bytes is None else max(1, int(max_bytes))
        self._now_ms_fn = now_ms_fn or now_epoch_ms
        self._lock = threading.RLock()
        self._init_db()

    def __len__(self) -> int:
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            row = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
            return int(row[0])

    @property
    def total_bytes(self) -> int:
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM cache_entries").fetchone()
            return int(row[0])

    def get(self, key: str) -> bytes | None:
        if not is_cache_key(key):
            return None
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            row = conn.execute("SELECT value FROM cache_entries WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            return bytes(row[0])

    def contains_many(self, keys: Iterable[str]) -> set[str]:
        valid_keys = [key for key in dict.fromkeys(keys) if is_cache_key(key)]
        if not valid_keys:
            return set()
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            found: set[str] = set()
            for key in valid_keys:
                row = conn.execute("SELECT 1 FROM cache_entries WHERE key = ?", (key,)).fetchone()
                if row is not None:
                    found.add(key)
            return found

    def put(self, key: str, value: bytes, *, cache_ts: int) -> bool:
        return bool(self.put_many([(key, value)], cache_ts=cache_ts))

    def put_many(self, entries: Iterable[tuple[str, bytes]], *, cache_ts: int) -> list[str]:
        cache_ts = int(cache_ts)
        created: list[str] = []
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            for key, value in entries:
                if not is_cache_key(key):
                    continue
                value = bytes(value)
                size = len(value)
                if self.max_bytes is not None and size > self.max_bytes:
                    continue

                existing = conn.execute(
                    "SELECT last_accessed_at FROM cache_entries WHERE key = ?",
                    (key,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO cache_entries
                            (key, value, size, created_at, last_accessed_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (key, sqlite3.Binary(value), size, self._now_ms(), cache_ts),
                    )
                    created.append(key)
                else:
                    last_accessed_at = max(int(existing[0]), cache_ts)
                    conn.execute(
                        """
                        UPDATE cache_entries
                        SET value = ?, size = ?, last_accessed_at = ?
                        WHERE key = ?
                        """,
                        (sqlite3.Binary(value), size, last_accessed_at, key),
                    )
            self._enforce_limits(conn)
            return self._retained_keys(conn, created)

    def touch_many(self, keys: Iterable[str], *, cache_ts: int) -> None:
        valid_keys = [key for key in dict.fromkeys(keys) if is_cache_key(key)]
        if not valid_keys:
            return
        cache_ts = int(cache_ts)
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            for key in valid_keys:
                conn.execute(
                    """
                    UPDATE cache_entries
                    SET last_accessed_at = CASE
                        WHEN last_accessed_at < ? THEN ?
                        ELSE last_accessed_at
                    END
                    WHERE key = ?
                    """,
                    (cache_ts, cache_ts, key),
                )

    def prune(self) -> None:
        with self._locked_connection() as conn:
            self._prune_expired(conn)
            self._enforce_limits(conn)

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY,
                    value BLOB NOT NULL,
                    size INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_accessed_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cache_entries_last_accessed
                ON cache_entries(last_accessed_at)
                """
            )

    def _locked_connection(self):
        return _LockedConnection(self)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _now_ms(self) -> int:
        return int(self._now_ms_fn())

    def _prune_expired(self, conn: sqlite3.Connection) -> None:
        expires_before = self._now_ms() - (self.ttl_seconds * 1000)
        conn.execute("DELETE FROM cache_entries WHERE last_accessed_at <= ?", (expires_before,))

    def _enforce_limits(self, conn: sqlite3.Connection) -> None:
        while True:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache_entries"
            ).fetchone()
            count = int(row[0])
            total_bytes = int(row[1])
            over_items = self.max_items is not None and count > self.max_items
            over_bytes = self.max_bytes is not None and total_bytes > self.max_bytes
            if not over_items and not over_bytes:
                return
            victim = conn.execute(
                """
                SELECT key FROM cache_entries
                ORDER BY last_accessed_at ASC, created_at ASC, key ASC
                LIMIT 1
                """
            ).fetchone()
            if victim is None:
                return
            conn.execute("DELETE FROM cache_entries WHERE key = ?", (victim[0],))

    def _retained_keys(self, conn: sqlite3.Connection, keys: list[str]) -> list[str]:
        if not keys:
            return []
        retained = {
            row[0]
            for row in (
                conn.execute("SELECT key FROM cache_entries WHERE key = ?", (key,)).fetchone()
                for key in keys
            )
            if row is not None
        }
        return [key for key in keys if key in retained]


class _LockedConnection:
    def __init__(self, cache: SqliteByteCache) -> None:
        self._cache = cache
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._cache._lock.acquire()
        self._conn = self._cache._connect()
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._conn is not None
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
            self._cache._lock.release()
