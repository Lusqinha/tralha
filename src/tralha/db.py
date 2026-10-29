"""Schema and database access (SQLite or Postgres, chosen by TRALHA_DATABASE_URL).

Full-text search depends on the database: SQLite uses an FTS5 virtual table, Postgres a
regular table with a GIN index over to_tsvector. The FTS5 table does not take part in
ON DELETE CASCADE, so code that deletes objects has to call unindex_doc.
"""

import time
from functools import lru_cache

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)

from tralha.config import get_settings

metadata = MetaData()

buckets = Table(
    "buckets",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(63), nullable=False, unique=True),
    Column("visibility", String(16), nullable=False, default="private"),
    Column("compression", String(16)),
    Column("max_bytes", BigInteger),
    Column("on_full", String(16), nullable=False, default="reject"),
    Column("created_at", BigInteger, nullable=False),
)

objects = Table(
    "objects",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "bucket_id",
        Integer,
        ForeignKey("buckets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("key", String(1024), nullable=False),
    Column("blob_sha256", String(64), nullable=False, index=True),
    Column("size", BigInteger, nullable=False),
    Column("content_type", String(255), nullable=False),
    Column("metadata", Text),
    Column("original_sha256", String(64), index=True),
    Column("original_content_type", String(255)),
    Column("original_size", BigInteger),
    Column("expires_at", BigInteger, index=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("last_accessed_at", BigInteger),
    UniqueConstraint("bucket_id", "key"),
)

lifecycle_rules = Table(
    "lifecycle_rules",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "bucket_id",
        Integer,
        ForeignKey("buckets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("prefix", String(1024), nullable=False, default=""),
    Column("expire_after", Integer, nullable=False),
    Column("basis", String(16), nullable=False, default="created"),
    Column("keep_shared", Boolean, nullable=False, default=False),
    Column("created_at", BigInteger, nullable=False),
)

system_settings = Table(
    "system_settings",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

admin_sessions = Table(
    "admin_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column(
        "key_id",
        Integer,
        ForeignKey("api_keys.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("expires_at", BigInteger, nullable=False),
)

shares = Table(
    "shares",
    metadata,
    Column("token", String(64), primary_key=True),
    Column(
        "object_id",
        Integer,
        ForeignKey("objects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("expires_at", BigInteger),
    Column("created_at", BigInteger, nullable=False),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("prefix", String(16), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("scopes", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger),
    Column("last_used_at", BigInteger),
    Column("last_used_ip", String(64)),
    Column("revoked_at", BigInteger),
)

upload_tokens = Table(
    "upload_tokens",
    metadata,
    Column("token", String(64), primary_key=True),
    Column(
        "bucket_id",
        Integer,
        ForeignKey("buckets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("key", String(1024), nullable=False),
    Column(
        "api_key_id",
        Integer,
        ForeignKey("api_keys.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ttl", Integer),
    Column("expires_at", BigInteger, nullable=False),
    Column("used_at", BigInteger),
)

_SQLITE_FTS = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(object_id UNINDEXED, content)",
]
_POSTGRES_FTS = [
    """CREATE TABLE IF NOT EXISTS doc_texts (
        object_id INTEGER PRIMARY KEY REFERENCES objects(id) ON DELETE CASCADE,
        content TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS doc_texts_fts ON doc_texts USING GIN (to_tsvector('simple', content))",
]


def now() -> int:
    return int(time.time())


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def is_sqlite() -> bool:
    return get_engine().dialect.name == "sqlite"


def init_db() -> None:
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in _SQLITE_FTS if is_sqlite() else _POSTGRES_FTS:
            conn.execute(text(ddl))


def index_doc(conn, object_id: int, content: str) -> None:
    unindex_doc(conn, object_id)
    table = "doc_fts" if is_sqlite() else "doc_texts"
    conn.execute(
        text(f"INSERT INTO {table} (object_id, content) VALUES (:id, :c)"),
        {"id": object_id, "c": content},
    )


def unindex_doc(conn, object_id: int) -> None:
    table = "doc_fts" if is_sqlite() else "doc_texts"
    conn.execute(text(f"DELETE FROM {table} WHERE object_id = :id"), {"id": object_id})


def _fts5_literal_terms(query: str) -> str:
    return " ".join('"' + term.replace('"', '""') + '"' for term in query.split())


def search_docs(conn, query: str, limit: int) -> list[tuple[int, str]]:
    if is_sqlite():
        match = _fts5_literal_terms(query)
        if not match:
            return []
        rows = conn.execute(
            text(
                "SELECT object_id, snippet(doc_fts, 1, '[', ']', '…', 16) FROM doc_fts "
                "WHERE doc_fts MATCH :q ORDER BY rank LIMIT :n"
            ),
            {"q": match, "n": limit},
        )
    else:
        rows = conn.execute(
            text(
                "SELECT object_id, ts_headline('simple', content, q, "
                "'StartSel=[,StopSel=],MaxWords=24,MinWords=8') "
                "FROM doc_texts, websearch_to_tsquery('simple', :q) q "
                "WHERE to_tsvector('simple', content) @@ q "
                "ORDER BY ts_rank(to_tsvector('simple', content), q) DESC LIMIT :n"
            ),
            {"q": query, "n": limit},
        )
    return [(int(r[0]), r[1]) for r in rows]
