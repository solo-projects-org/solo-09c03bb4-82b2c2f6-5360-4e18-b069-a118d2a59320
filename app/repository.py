from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schemas import (
    BlockReviewInput,
    CatalogArticleInput,
    OCRPageInput,
    OCRWordInput,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    page_id     TEXT PRIMARY KEY,
    image_url   TEXT,
    width       REAL,
    height      REAL,
    revision    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_blocks (
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    ordinal     INTEGER NOT NULL,
    PRIMARY KEY (page_id, block_no),
    UNIQUE (page_id, ordinal),
    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS page_words (
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    word_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token       TEXT NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    PRIMARY KEY (page_id, block_no, word_no),
    FOREIGN KEY (page_id, block_no)
        REFERENCES page_blocks(page_id, block_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_page_words_token ON page_words(token);

CREATE TABLE IF NOT EXISTS articles (
    article_id     TEXT PRIMARY KEY,
    title          TEXT,
    revision       INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    indexed_words  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog_blocks (
    article_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    PRIMARY KEY (article_id, seq),
    UNIQUE (article_id, page_id, block_no),
    FOREIGN KEY (article_id) REFERENCES articles(article_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_catalog_blocks_page
    ON catalog_blocks(page_id, block_no);

CREATE TABLE IF NOT EXISTS article_index (
    article_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    word_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token       TEXT NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    PRIMARY KEY (article_id, seq),
    FOREIGN KEY (article_id) REFERENCES articles(article_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_index_token ON article_index(token);
CREATE INDEX IF NOT EXISTS idx_article_index_article_seq
    ON article_index(article_id, seq);

-- Per-block verification marks.  A row exists only while an archivist has
-- declared an uncataloged OCR block to be non-body text (header/ad/...).
-- No FK to page_blocks on purpose: OCR replacement deletes and re-inserts
-- page_blocks inside one transaction, and the reconciliation logic in
-- upsert_page_ocr decides explicitly which marks survive.
CREATE TABLE IF NOT EXISTS block_reviews (
    page_id          TEXT NOT NULL,
    block_no         INTEGER NOT NULL,
    marked_reason    TEXT,
    marked_at        TEXT NOT NULL,
    marked_version   INTEGER NOT NULL,
    PRIMARY KEY (page_id, block_no)
);

-- Monotonic per-block review token.  It is bumped (never reused, even when a
-- block is deleted and later restored byte-for-byte) whenever the block's OCR
-- content or its catalog-reference state changes.  Fetched as the optimistic
-- expected_version of every mark submission.
CREATE TABLE IF NOT EXISTS block_versions (
    page_id        TEXT NOT NULL,
    block_no       INTEGER NOT NULL,
    review_version INTEGER NOT NULL,
    exists_flag    INTEGER NOT NULL,
    last_change    TEXT NOT NULL,
    PRIMARY KEY (page_id, block_no)
);
"""

MAX_PHRASE_TOKENS = 40


class DomainError(Exception):
    """An expected error that should be returned as a structured HTTP error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_token(value: str) -> str:
    """Normalize case and Unicode width while keeping OCR token boundaries."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def _connection(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # BEGIN IMMEDIATE serializes writers.  All source changes and every
    # affected article's index rows commit together, never partially.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def _read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # A deferred read transaction gives a single SQLite snapshot for the whole
    # search (candidate positions, boxes, and article titles).
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def initialize_database(db_path: str | os.PathLike[str]) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connection(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def _get_page_refs(
    conn: sqlite3.Connection, page_id: str
) -> tuple[set[int], list[str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT article_id, block_no
        FROM catalog_blocks
        WHERE page_id = ?
        ORDER BY article_id, block_no
        """,
        (page_id,),
    ).fetchall()
    required = {int(row["block_no"]) for row in rows}
    affected = sorted({str(row["article_id"]) for row in rows})
    return required, affected


def _replace_page_source(
    conn: sqlite3.Connection,
    page_id: str,
    payload: OCRPageInput,
    blocks: list[tuple[int, list[OCRWordInput]]],
    updated_at: str,
) -> tuple[int, int]:
    old = conn.execute(
        "SELECT revision FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    revision = (int(old["revision"]) + 1) if old else 1

    if old:
        conn.execute(
            """
            UPDATE pages
            SET image_url = ?, width = ?, height = ?,
                revision = ?, updated_at = ?
            WHERE page_id = ?
            """,
            (
                payload.image_url,
                payload.width,
                payload.height,
                revision,
                updated_at,
                page_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO pages
                (page_id, image_url, width, height, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                payload.image_url,
                payload.width,
                payload.height,
                revision,
                updated_at,
            ),
        )

    # Children first; ordering is explicit because page block numbers need not
    # be contiguous or ascending in the source export.
    conn.execute("DELETE FROM page_words WHERE page_id = ?", (page_id,))
    conn.execute("DELETE FROM page_blocks WHERE page_id = ?", (page_id,))

    word_count = 0
    for ordinal, (block_no, words) in enumerate(blocks):
        conn.execute(
            "INSERT INTO page_blocks (page_id, block_no, ordinal) VALUES (?, ?, ?)",
            (page_id, block_no, ordinal),
        )
        rows = []
        for word_no, word in enumerate(words):
            token = normalize_token(word.text)
            rows.append(
                (
                    page_id,
                    block_no,
                    word_no,
                    word.text,
                    token,
                    word.bbox.x0,
                    word.bbox.y0,
                    word.bbox.x1,
                    word.bbox.y1,
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO page_words
                    (page_id, block_no, word_no, text, token, x0, y0, x1, y1)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        word_count += len(words)

    return revision, word_count


# ---------------------------------------------------------------------------
# Layout review helpers
# ---------------------------------------------------------------------------


def _touch_review_version(
    conn: sqlite3.Connection,
    page_id: str,
    block_no: int,
    *,
    exists_flag: bool,
    change: str,
) -> int:
    """Bump (or create) a block's review token and return its new value.

    Tokens are strictly monotonic per (page, block), including across delete
    and byte-identical restore cycles, so a token captured before any change
    can never validate a submission made after it.
    """
    row = conn.execute(
        "SELECT review_version FROM block_versions WHERE page_id = ? AND block_no = ?",
        (page_id, block_no),
    ).fetchone()
    version = (int(row["review_version"]) + 1) if row else 1
    conn.execute(
        """
        INSERT INTO block_versions
            (page_id, block_no, review_version, exists_flag, last_change)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(page_id, block_no) DO UPDATE SET
            review_version = excluded.review_version,
            exists_flag = excluded.exists_flag,
            last_change = excluded.last_change
        """,
        (page_id, block_no, version, 1 if exists_flag else 0, change),
    )
    return version


def _current_review_version(
    conn: sqlite3.Connection, page_id: str, block_no: int
) -> int | None:
    row = conn.execute(
        "SELECT review_version FROM block_versions WHERE page_id = ? AND block_no = ?",
        (page_id, block_no),
    ).fetchone()
    return int(row["review_version"]) if row else None


def _block_fingerprint(
    words: list[OCRWordInput] | list[sqlite3.Row],
) -> list[tuple[str, float, float, float, float]]:
    """Identity of a block: ordered (word_no, text, bbox) tuples.

    A mark only survives an OCR re-run when block number, word order, original
    text and every word box are all unchanged.
    """
    fingerprint: list[tuple[str, float, float, float, float]] = []
    for word_no, word in enumerate(words):
        if isinstance(word, sqlite3.Row):
            fingerprint.append(
                (
                    str(word["text"]),
                    float(word["x0"]),
                    float(word["y0"]),
                    float(word["x1"]),
                    float(word["y1"]),
                )
            )
        else:
            fingerprint.append(
                (
                    word.text,
                    float(word.bbox.x0),
                    float(word.bbox.y0),
                    float(word.bbox.x1),
                    float(word.bbox.y1),
                )
            )
    return fingerprint


def _existing_block_fingerprints(
    conn: sqlite3.Connection, page_id: str
) -> dict[int, list[tuple[str, float, float, float, float]]]:
    rows = conn.execute(
        """
        SELECT block_no, word_no, text, x0, y0, x1, y1
        FROM page_words
        WHERE page_id = ?
        ORDER BY block_no, word_no
        """,
        (page_id,),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["block_no"])].append(row)
    return {
        block_no: _block_fingerprint(block_rows)
        for block_no, block_rows in grouped.items()
    }


def _page_referenced_blocks(conn: sqlite3.Connection, page_id: str) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT block_no FROM catalog_blocks WHERE page_id = ?",
        (page_id,),
    ).fetchall()
    return {int(row["block_no"]) for row in rows}


def _review_status_for(
    referenced: bool,
    mark: sqlite3.Row | None,
    review_version: int,
) -> str:
    if referenced:
        return "cataloged"
    if mark is not None and int(mark["marked_version"]) == review_version:
        return "marked_non_body"
    return "pending_review"


def _build_block_review(
    conn: sqlite3.Connection,
    page_id: str,
    block_row: sqlite3.Row,
    word_rows: list[sqlite3.Row],
    referenced_blocks: set[int],
) -> dict[str, Any]:
    block_no = int(block_row["block_no"])
    referenced = block_no in referenced_blocks
    version_row = conn.execute(
        "SELECT review_version, last_change FROM block_versions "
        "WHERE page_id = ? AND block_no = ?",
        (page_id, block_no),
    ).fetchone()
    review_version = int(version_row["review_version"]) if version_row else 1
    last_change = version_row["last_change"] if version_row else None

    mark = conn.execute(
        "SELECT marked_reason, marked_at, marked_version FROM block_reviews "
        "WHERE page_id = ? AND block_no = ?",
        (page_id, block_no),
    ).fetchone()
    review_status = _review_status_for(referenced, mark, review_version)

    x0 = min(float(row["x0"]) for row in word_rows)
    y0 = min(float(row["y0"]) for row in word_rows)
    x1 = max(float(row["x1"]) for row in word_rows)
    y1 = max(float(row["y1"]) for row in word_rows)

    return {
        "block_no": block_no,
        "ordinal": int(block_row["ordinal"]),
        "referenced": referenced,
        "review_status": review_status,
        "review_version": review_version,
        "last_change": last_change,
        "marked_reason": mark["marked_reason"] if mark else None,
        "marked_at": mark["marked_at"] if mark else None,
        "marked_version": (
            int(mark["marked_version"]) if mark is not None else None
        ),
        "text": " ".join(str(row["text"]) for row in word_rows),
        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "words": [
            {
                "word_no": int(row["word_no"]),
                "text": str(row["text"]),
                "bbox": {
                    "x0": float(row["x0"]),
                    "y0": float(row["y0"]),
                    "x1": float(row["x1"]),
                    "y1": float(row["y1"]),
                },
            }
            for row in word_rows
        ],
    }


def _rebuild_article(conn: sqlite3.Connection, article_id: str) -> int:
    """Rebuild one article's flattened OCR sequence from current page OCR.

    An article is indexed only when every catalog-referenced block is present
    in OCR.  This makes catalog-before-OCR ingestion possible without exposing
    phrases that would silently jump over a missing block.
    """
    conn.execute("DELETE FROM article_index WHERE article_id = ?", (article_id,))

    incomplete = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if incomplete:
        conn.execute(
            "UPDATE articles SET indexed_words = 0 WHERE article_id = ?",
            (article_id,),
        )
        return 0

    rows = conn.execute(
        """
        SELECT pw.page_id, pw.block_no, pw.word_no, pw.text, pw.token,
               pw.x0, pw.y0, pw.x1, pw.y1
        FROM catalog_blocks AS cb
        JOIN page_words AS pw
          ON pw.page_id = cb.page_id
         AND pw.block_no = cb.block_no
        WHERE cb.article_id = ?
        ORDER BY cb.seq ASC, pw.word_no ASC
        """,
        (article_id,),
    ).fetchall()

    params = [
        (
            article_id,
            seq,
            row["page_id"],
            row["block_no"],
            row["word_no"],
            row["text"],
            row["token"],
            row["x0"],
            row["y0"],
            row["x1"],
            row["y1"],
        )
        for seq, row in enumerate(rows)
    ]
    if params:
        conn.executemany(
            """
            INSERT INTO article_index
                (article_id, seq, page_id, block_no, word_no,
                 text, token, x0, y0, x1, y1)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
    conn.execute(
        "UPDATE articles SET indexed_words = ? WHERE article_id = ?",
        (len(rows), article_id),
    )
    return len(rows)


def upsert_page_ocr(
    db_path: str | os.PathLike[str],
    page_id: str,
    payload: OCRPageInput,
) -> dict[str, Any]:
    page_id = page_id.strip()
    if not page_id:
        raise DomainError(422, "invalid_page_id", "page_id must not be blank")

    blocks = payload.grouped_blocks()
    provided = {block_no for block_no, _ in blocks}
    updated_at = utc_now()

    with _connection(db_path) as conn:
        with _write_transaction(conn):
            required, affected = _get_page_refs(conn, page_id)
            missing = sorted(required - provided)
            if missing:
                raise DomainError(
                    409,
                    "missing_catalog_blocks",
                    (
                        "OCR retransmission rejected: the page omits blocks that "
                        "catalog articles reference. No OCR or index change was "
                        "committed; the previous results remain queryable."
                    ),
                    page_id=page_id,
                    missing_blocks=missing,
                    affected_articles=affected,
                    unchanged=True,
                )

            # Snapshot the outgoing content while the old page rows still exist.
            old_fingerprints = _existing_block_fingerprints(conn, page_id)
            old_marks = {
                int(row["block_no"]): row
                for row in conn.execute(
                    "SELECT block_no FROM block_reviews WHERE page_id = ?",
                    (page_id,),
                ).fetchall()
            }

            revision, word_count = _replace_page_source(
                conn, page_id, payload, blocks, updated_at
            )

            # Reconcile verification state with the new OCR.  Everything in
            # this branch commits (or rolls back) together with the OCR rows
            # and the rebuilt article indexes.
            marks_carried: list[dict[str, Any]] = []
            marks_reset: list[dict[str, Any]] = []
            for block_no, words in blocks:
                new_fingerprint = _block_fingerprint(words)
                if block_no in old_fingerprints:
                    if old_fingerprints[block_no] == new_fingerprint:
                        # Identical block number, word order, text and boxes:
                        # keep both the token and any existing mark.
                        if block_no in old_marks:
                            marks_carried.append(
                                {
                                    "page_id": page_id,
                                    "block_no": block_no,
                                    "review_version": _current_review_version(
                                        conn, page_id, block_no
                                    )
                                    or 1,
                                }
                            )
                        conn.execute(
                            "UPDATE block_versions SET exists_flag = 1, "
                            "last_change = ? WHERE page_id = ? AND block_no = ?",
                            ("ocr_unchanged", page_id, block_no),
                        )
                        # A block may have no version row yet if it predates
                        # the review tables; create one without consuming a bump.
                        if _current_review_version(conn, page_id, block_no) is None:
                            conn.execute(
                                """
                                INSERT INTO block_versions
                                    (page_id, block_no, review_version,
                                     exists_flag, last_change)
                                VALUES (?, ?, 1, 1, 'ocr_unchanged')
                                """,
                                (page_id, block_no),
                            )
                    else:
                        version = _touch_review_version(
                            conn,
                            page_id,
                            block_no,
                            exists_flag=True,
                            change="ocr_content_changed",
                        )
                        conn.execute(
                            "DELETE FROM block_reviews "
                            "WHERE page_id = ? AND block_no = ?",
                            (page_id, block_no),
                        )
                        if block_no in old_marks:
                            marks_reset.append(
                                {
                                    "page_id": page_id,
                                    "block_no": block_no,
                                    "review_version": version,
                                }
                            )
                else:
                    _touch_review_version(
                        conn,
                        page_id,
                        block_no,
                        exists_flag=True,
                        change="ocr_block_added",
                    )

            for block_no in old_fingerprints:
                if block_no not in provided:
                    # Deleted blocks keep their history with a fresh token so a
                    # later byte-identical restore still cannot reuse old marks.
                    _touch_review_version(
                        conn,
                        page_id,
                        block_no,
                        exists_flag=False,
                        change="ocr_block_deleted",
                    )
                    conn.execute(
                        "DELETE FROM block_reviews "
                        "WHERE page_id = ? AND block_no = ?",
                        (page_id, block_no),
                    )

            for article_id in affected:
                _rebuild_article(conn, article_id)

            return {
                "page_id": page_id,
                "revision": revision,
                "updated_at": updated_at,
                "blocks_count": len(blocks),
                "words_count": word_count,
                "affected_articles": affected,
                "review_marks_carried": marks_carried,
                "review_marks_reset": marks_reset,
                "message": (
                    "Page OCR, verification marks and all affected article "
                    "indexes committed atomically."
                ),
            }


def upsert_catalog_article(
    db_path: str | os.PathLike[str],
    article_id: str,
    payload: CatalogArticleInput,
) -> dict[str, Any]:
    article_id = article_id.strip()
    if not article_id:
        raise DomainError(422, "invalid_article_id", "article_id must not be blank")

    updated_at = utc_now()
    with _connection(db_path) as conn:
        with _write_transaction(conn):
            old = conn.execute(
                "SELECT revision FROM articles WHERE article_id = ?",
                (article_id,),
            ).fetchone()
            old_ref_rows = conn.execute(
                "SELECT page_id, block_no FROM catalog_blocks WHERE article_id = ?",
                (article_id,),
            ).fetchall()

            missing = []
            for ref in payload.blocks:
                exists = conn.execute(
                    """
                    SELECT 1 FROM page_blocks
                    WHERE page_id = ? AND block_no = ?
                    """,
                    (ref.page_id, ref.block_no),
                ).fetchone()
                if not exists:
                    missing.append(f"{ref.page_id}/{ref.block_no}")

            # A brand-new article may be cataloged before its OCR pages arrive;
            # it stays unindexed until complete.  Replacing an existing article
            # with missing OCR blocks is rejected so its old results are not
            # silently discarded.
            if missing and old:
                raise DomainError(
                    409,
                    "missing_ocr_blocks",
                    (
                        "Catalog replacement rejected because referenced OCR "
                        "blocks are not present. The previous catalog and index "
                        "remain unchanged."
                    ),
                    article_id=article_id,
                    missing_blocks=missing,
                    unchanged=True,
                )

            revision = (int(old["revision"]) + 1) if old else 1

            if old:
                conn.execute(
                    """
                    UPDATE articles
                    SET title = ?, revision = ?, updated_at = ?
                    WHERE article_id = ?
                    """,
                    (payload.title, revision, updated_at, article_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO articles
                        (article_id, title, revision, updated_at, indexed_words)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (article_id, payload.title, revision, updated_at),
                )

            conn.execute(
                "DELETE FROM catalog_blocks WHERE article_id = ?", (article_id,)
            )
            conn.executemany(
                """
                INSERT INTO catalog_blocks
                    (article_id, seq, page_id, block_no)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (article_id, seq, ref.page_id, ref.block_no)
                    for seq, ref in enumerate(payload.blocks)
                ],
            )

            # Reference-state transitions bump the block's review token:
            # 0 -> 1 revokes any mark (the block is now cataloged body text);
            # 1 -> 0 means the block left the catalog and must be re-checked.
            old_counts: dict[tuple[str, int], int] = {}
            for row in old_ref_rows:
                key = (str(row["page_id"]), int(row["block_no"]))
                old_counts[key] = old_counts.get(key, 0) + 1
            new_counts: dict[tuple[str, int], int] = defaultdict(int)
            for ref in payload.blocks:
                new_counts[(ref.page_id, ref.block_no)] += 1

            revoked_review_marks: list[dict[str, str | int]] = []
            all_keys = sorted(set(old_counts) | set(new_counts))
            for ref_page_id, ref_block_no in all_keys:
                before = old_counts.get((ref_page_id, ref_block_no), 0)
                after = new_counts.get((ref_page_id, ref_block_no), 0)
                if before == after:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM page_blocks WHERE page_id = ? AND block_no = ?",
                    (ref_page_id, ref_block_no),
                ).fetchone()
                change = (
                    "catalog_referenced" if after > 0 else "catalog_unreferenced"
                )
                had_mark = conn.execute(
                    "SELECT 1 FROM block_reviews "
                    "WHERE page_id = ? AND block_no = ?",
                    (ref_page_id, ref_block_no),
                ).fetchone()
                version = _touch_review_version(
                    conn,
                    ref_page_id,
                    ref_block_no,
                    exists_flag=bool(exists),
                    change=change,
                )
                conn.execute(
                    "DELETE FROM block_reviews WHERE page_id = ? AND block_no = ?",
                    (ref_page_id, ref_block_no),
                )
                if after > 0 and had_mark:
                    revoked_review_marks.append(
                        {
                            "page_id": ref_page_id,
                            "block_no": ref_block_no,
                            "review_version": version,
                        }
                    )

            _rebuild_article(conn, article_id)
            info = _article_info_from_conn(conn, article_id)
            info["revoked_review_marks"] = revoked_review_marks
            return info


def _page_info_from_conn(conn: sqlite3.Connection, page_id: str) -> dict[str, Any]:
    page = conn.execute(
        "SELECT * FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if not page:
        raise DomainError(404, "page_not_found", f"page {page_id!r} was not found")

    blocks = conn.execute(
        """
        SELECT pb.block_no, pb.ordinal, COUNT(pw.word_no) AS word_count
        FROM page_blocks AS pb
        LEFT JOIN page_words AS pw
          ON pw.page_id = pb.page_id AND pw.block_no = pb.block_no
        WHERE pb.page_id = ?
        GROUP BY pb.page_id, pb.block_no, pb.ordinal
        ORDER BY pb.ordinal
        """,
        (page_id,),
    ).fetchall()

    return {
        "page_id": page["page_id"],
        "image_url": page["image_url"],
        "width": page["width"],
        "height": page["height"],
        "revision": int(page["revision"]),
        "updated_at": page["updated_at"],
        "blocks": [
            {
                "block_no": int(row["block_no"]),
                "ordinal": int(row["ordinal"]),
                "word_count": int(row["word_count"]),
            }
            for row in blocks
        ],
    }


def get_page_info(
    db_path: str | os.PathLike[str], page_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _page_info_from_conn(conn, page_id)


def _article_info_from_conn(
    conn: sqlite3.Connection, article_id: str
) -> dict[str, Any]:
    article = conn.execute(
        "SELECT * FROM articles WHERE article_id = ?", (article_id,)
    ).fetchone()
    if not article:
        raise DomainError(
            404, "article_not_found", f"article {article_id!r} was not found"
        )

    blocks = conn.execute(
        """
        SELECT page_id, block_no
        FROM catalog_blocks
        WHERE article_id = ?
        ORDER BY seq
        """,
        (article_id,),
    ).fetchall()
    incomplete = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    return {
        "article_id": article["article_id"],
        "title": article["title"],
        "revision": int(article["revision"]),
        "updated_at": article["updated_at"],
        "status": "pending_ocr" if incomplete else "indexed",
        "indexed_words": int(article["indexed_words"]),
        "blocks": [
            {"page_id": row["page_id"], "block_no": int(row["block_no"])}
            for row in blocks
        ],
        "revoked_review_marks": [],
    }


def get_article_info(
    db_path: str | os.PathLike[str], article_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _article_info_from_conn(conn, article_id)


# ---------------------------------------------------------------------------
# Layout review: uncataloged word-block verification
# ---------------------------------------------------------------------------


def _page_review_from_conn(
    conn: sqlite3.Connection, page_id: str
) -> dict[str, Any]:
    page = conn.execute(
        "SELECT * FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if not page:
        raise DomainError(404, "page_not_found", f"page {page_id!r} was not found")

    block_rows = conn.execute(
        "SELECT block_no, ordinal FROM page_blocks WHERE page_id = ? ORDER BY ordinal",
        (page_id,),
    ).fetchall()
    word_rows = conn.execute(
        """
        SELECT block_no, word_no, text, x0, y0, x1, y1
        FROM page_words
        WHERE page_id = ?
        ORDER BY block_no, word_no
        """,
        (page_id,),
    ).fetchall()
    words_by_block: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in word_rows:
        words_by_block[int(row["block_no"])].append(row)

    referenced_blocks = _page_referenced_blocks(conn, page_id)

    # The checklist covers word blocks only; empty layout blocks have no OCR
    # text and therefore nothing that could be miscataloged.
    blocks = [
        _build_block_review(
            conn, page_id, block_row, words_by_block[int(block_row["block_no"])],
            referenced_blocks,
        )
        for block_row in block_rows
        if words_by_block[int(block_row["block_no"])]
    ]

    cataloged = sum(1 for block in blocks if block["review_status"] == "cataloged")
    marked = sum(
        1 for block in blocks if block["review_status"] == "marked_non_body"
    )
    pending = sum(
        1 for block in blocks if block["review_status"] == "pending_review"
    )
    return {
        "page_id": page["page_id"],
        "revision": int(page["revision"]),
        "updated_at": page["updated_at"],
        "review_status": "complete" if not pending else "pending_review",
        "blocks_total": len(blocks),
        "blocks_cataloged": cataloged,
        "blocks_marked_non_body": marked,
        "blocks_pending": pending,
        "blocks": blocks,
    }


def get_page_review(
    db_path: str | os.PathLike[str], page_id: str
) -> dict[str, Any]:
    page_id = page_id.strip()
    if not page_id:
        raise DomainError(422, "invalid_page_id", "page_id must not be blank")
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _page_review_from_conn(conn, page_id)


def _load_review_target(
    conn: sqlite3.Connection, page_id: str, block_no: int
) -> tuple[sqlite3.Row, list[sqlite3.Row], bool, int, str | None]:
    """Resolve and validate one block for a mark mutation.

    Raises a structured DomainError when the block was deleted, changed or
    cataloged after the caller fetched the checklist.
    """
    block_row = conn.execute(
        "SELECT block_no, ordinal FROM page_blocks "
        "WHERE page_id = ? AND block_no = ?",
        (page_id, block_no),
    ).fetchone()
    if not block_row:
        raise DomainError(
            404,
            "block_not_found",
            (
                "Submission rejected: the target OCR block no longer exists. "
                "Refetch the page review checklist and re-check the block."
            ),
            page_id=page_id,
            block_no=block_no,
        )

    word_rows = conn.execute(
        """
        SELECT word_no, text, x0, y0, x1, y1
        FROM page_words
        WHERE page_id = ? AND block_no = ?
        ORDER BY word_no
        """,
        (page_id, block_no),
    ).fetchall()
    if not word_rows:
        raise DomainError(
            404,
            "block_not_found",
            (
                "Submission rejected: the target block has no OCR words. "
                "Refetch the page review checklist and re-check the block."
            ),
            page_id=page_id,
            block_no=block_no,
        )

    version_row = conn.execute(
        "SELECT review_version, last_change FROM block_versions "
        "WHERE page_id = ? AND block_no = ?",
        (page_id, block_no),
    ).fetchone()
    review_version = int(version_row["review_version"]) if version_row else 1
    last_change = version_row["last_change"] if version_row else None

    referenced = block_no in _page_referenced_blocks(conn, page_id)
    return block_row, word_rows, referenced, review_version, last_change


def _assert_review_token(
    expected_version: int,
    review_version: int,
    *,
    referenced: bool,
    last_change: str | None,
    page_id: str,
    block_no: int,
) -> None:
    if referenced:
        raise DomainError(
            409,
            "block_now_cataloged",
            (
                "Submission rejected: the block has been cited by a catalog "
                "article, so any verification mark was revoked. Refetch the "
                "checklist; cataloged blocks need no non-body mark."
            ),
            page_id=page_id,
            block_no=block_no,
            expected_version=expected_version,
            current_version=review_version,
            last_change=last_change,
            recheck_required=True,
        )
    if expected_version != review_version:
        raise DomainError(
            409,
            "review_version_stale",
            (
                "Submission rejected: the target block changed, was deleted or "
                "was referenced since you fetched the checklist (even if its "
                "content looks restored). Refetch the checklist and re-check "
                "the block."
            ),
            page_id=page_id,
            block_no=block_no,
            expected_version=expected_version,
            current_version=review_version,
            last_change=last_change,
            recheck_required=True,
        )


def mark_block_non_body(
    db_path: str | os.PathLike[str],
    page_id: str,
    block_no: int,
    payload: BlockReviewInput,
) -> dict[str, Any]:
    page_id = page_id.strip()
    if not page_id:
        raise DomainError(422, "invalid_page_id", "page_id must not be blank")

    with _connection(db_path) as conn:
        with _write_transaction(conn):
            (
                block_row,
                word_rows,
                referenced,
                review_version,
                last_change,
            ) = _load_review_target(conn, page_id, block_no)
            _assert_review_token(
                payload.expected_version,
                review_version,
                referenced=referenced,
                last_change=last_change,
                page_id=page_id,
                block_no=block_no,
            )

            marked_at = utc_now()
            conn.execute(
                """
                INSERT INTO block_reviews
                    (page_id, block_no, marked_reason, marked_at, marked_version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(page_id, block_no) DO UPDATE SET
                    marked_reason = excluded.marked_reason,
                    marked_at = excluded.marked_at,
                    marked_version = excluded.marked_version
                """,
                (page_id, block_no, payload.reason, marked_at, review_version),
            )

            summary = _page_review_from_conn(conn, page_id)
            review = next(
                block
                for block in summary["blocks"]
                if block["block_no"] == block_no
            )
            return {
                "page_id": page_id,
                "block_no": block_no,
                "review": review,
                "page_review_status": summary["review_status"],
                "message": "Block marked as non-body text.",
            }


def clear_block_review(
    db_path: str | os.PathLike[str],
    page_id: str,
    block_no: int,
    expected_version: int,
) -> dict[str, Any]:
    page_id = page_id.strip()
    if not page_id:
        raise DomainError(422, "invalid_page_id", "page_id must not be blank")

    with _connection(db_path) as conn:
        with _write_transaction(conn):
            (
                block_row,
                word_rows,
                referenced,
                review_version,
                last_change,
            ) = _load_review_target(conn, page_id, block_no)
            _assert_review_token(
                expected_version,
                review_version,
                referenced=referenced,
                last_change=last_change,
                page_id=page_id,
                block_no=block_no,
            )

            mark = conn.execute(
                "SELECT 1 FROM block_reviews WHERE page_id = ? AND block_no = ?",
                (page_id, block_no),
            ).fetchone()
            if not mark:
                raise DomainError(
                    404,
                    "review_mark_not_found",
                    "No non-body mark exists for this block.",
                    page_id=page_id,
                    block_no=block_no,
                    current_version=review_version,
                )
            conn.execute(
                "DELETE FROM block_reviews WHERE page_id = ? AND block_no = ?",
                (page_id, block_no),
            )

            summary = _page_review_from_conn(conn, page_id)
            review = next(
                block
                for block in summary["blocks"]
                if block["block_no"] == block_no
            )
            return {
                "page_id": page_id,
                "block_no": block_no,
                "review": review,
                "page_review_status": summary["review_status"],
                "message": "Non-body mark removed; the block needs re-check.",
            }


def _candidate_starts(
    conn: sqlite3.Connection,
    tokens: list[str],
    article_id: str | None,
    limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    """Find consecutive token runs with a bounded number of SQL self joins."""
    token_count = len(tokens)
    params: list[Any] = []

    if token_count == 1:
        sql = "SELECT article_id, seq FROM article_index WHERE token = ?"
        params.append(tokens[0])
    else:
        parts = ["SELECT i0.article_id, i0.seq FROM article_index AS i0"]
        for offset in range(1, token_count):
            parts.append(
                "JOIN article_index AS i{off} "
                "ON i{off}.article_id = i0.article_id "
                "AND i{off}.seq = i0.seq + {off} "
                "AND i{off}.token = ?".format(off=offset)
            )
            params.append(tokens[offset])
        parts.append("WHERE i0.token = ?")
        params.append(tokens[0])
        sql = "\n".join(parts)

    if article_id:
        sql += " AND article_id = ?" if token_count == 1 else " AND i0.article_id = ?"
        params.append(article_id)

    sql += (
        " ORDER BY article_id, seq" if token_count == 1 else " ORDER BY i0.article_id, i0.seq"
    )
    sql += " LIMIT ?"
    params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()
    return rows[:limit], len(rows) > limit


def search_phrase(
    db_path: str | os.PathLike[str],
    phrase: str,
    article_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    tokens = [normalize_token(token) for token in phrase.split()]
    if not tokens:
        raise DomainError(422, "empty_phrase", "phrase must contain at least one token")
    if len(tokens) > MAX_PHRASE_TOKENS:
        raise DomainError(
            422,
            "phrase_too_long",
            f"phrase may contain at most {MAX_PHRASE_TOKENS} tokens",
        )
    if limit < 1 or limit > 200:
        raise DomainError(422, "invalid_limit", "limit must be between 1 and 200")

    article_id = article_id.strip() if article_id else None
    if article_id == "":
        article_id = None

    with _connection(db_path) as conn:
        with _read_transaction(conn):
            candidates, truncated = _candidate_starts(
                conn, tokens, article_id, limit
            )
            if not candidates:
                return {
                    "query": phrase,
                    "normalized_tokens": tokens,
                    "limit": limit,
                    "count": 0,
                    "truncated": False,
                    "hits": [],
                }

            article_ids = sorted({str(row["article_id"]) for row in candidates})
            title_rows = conn.execute(
                """
                SELECT article_id, title
                FROM articles
                WHERE article_id IN ({})
                """.format(", ".join("?" for _ in article_ids)),
                article_ids,
            ).fetchall()
            titles = {
                str(row["article_id"]): row["title"] for row in title_rows
            }

            needed: dict[str, set[int]] = defaultdict(set)
            for row in candidates:
                start = int(row["seq"])
                aid = str(row["article_id"])
                needed[aid].update(range(start, start + len(tokens)))

            word_rows = []
            for aid, seqs in needed.items():
                ordered = sorted(seqs)
                # Stay comfortably below SQLite's host-parameter limit.
                for offset in range(0, len(ordered), 500):
                    chunk = ordered[offset : offset + 500]
                    word_rows.extend(
                        conn.execute(
                            """
                            SELECT article_id, seq, page_id, block_no, word_no,
                                   text, token, x0, y0, x1, y1
                            FROM article_index
                            WHERE article_id = ?
                              AND seq IN ({})
                            ORDER BY seq
                            """.format(", ".join("?" for _ in chunk)),
                            [aid, *chunk],
                        ).fetchall()
                    )

            words_by_seq = {
                (str(row["article_id"]), int(row["seq"])): row
                for row in word_rows
            }

            hits = []
            for candidate in candidates:
                aid = str(candidate["article_id"])
                start = int(candidate["seq"])
                sequence = [
                    words_by_seq[(aid, seq)]
                    for seq in range(start, start + len(tokens))
                ]
                pages: list[str] = []
                for row in sequence:
                    page_id = str(row["page_id"])
                    if page_id not in pages:
                        pages.append(page_id)

                hits.append(
                    {
                        "article_id": aid,
                        "title": titles.get(aid),
                        "start_seq": start,
                        "pages": pages,
                        "words": [
                            {
                                "seq": int(row["seq"]),
                                "page_id": row["page_id"],
                                "block_no": int(row["block_no"]),
                                "word_no": int(row["word_no"]),
                                "text": row["text"],
                                "bbox": {
                                    "x0": row["x0"],
                                    "y0": row["y0"],
                                    "x1": row["x1"],
                                    "y1": row["y1"],
                                },
                            }
                            for row in sequence
                        ],
                    }
                )

            return {
                "query": phrase,
                "normalized_tokens": tokens,
                "limit": limit,
                "count": len(hits),
                "truncated": truncated,
                "hits": hits,
            }


def health_counts(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    with _connection(db_path) as conn:
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        ocr_words = conn.execute("SELECT COUNT(*) FROM page_words").fetchone()[0]
        indexed_words = conn.execute("SELECT COUNT(*) FROM article_index").fetchone()[0]
        review_marks = conn.execute("SELECT COUNT(*) FROM block_reviews").fetchone()[0]
    return {
        "status": "ok",
        "database": str(db_path),
        "pages": int(pages),
        "articles": int(articles),
        "ocr_words": int(ocr_words),
        "indexed_words": int(indexed_words),
        "review_marks": int(review_marks),
    }


def seed_demo_if_empty(
    db_path: str | os.PathLike[str], sample_path: str | os.PathLike[str]
) -> bool:
    """Load the bundled local sample once into an empty database."""
    counts = health_counts(db_path)
    if counts["pages"] or counts["articles"]:
        return False

    with open(sample_path, "r", encoding="utf-8") as handle:
        sample = json.load(handle)

    for page_data in sample.get("pages", []):
        page_id = page_data["page_id"]
        page_payload = OCRPageInput.model_validate(page_data)
        upsert_page_ocr(db_path, page_id, page_payload)

    for article_data in sample.get("articles", []):
        article_id = article_data["article_id"]
        article_payload = CatalogArticleInput.model_validate(article_data)
        upsert_catalog_article(db_path, article_id, article_payload)

    return True
