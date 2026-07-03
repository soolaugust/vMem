"""Compatibility facade for store_vfs public APIs.

This module is the first step toward splitting the 17k-line store_vfs.py while
keeping existing flat imports stable. New code can import from this facade; the
legacy store_vfs.py entrypoint remains untouched for hook/runtime safety.
"""

from memory_os.store.vfs import (  # noqa: F401
    CHUNK_VERSION_FILE,
    MEMORY_OS_DIR,
    STORE_DB,
    _cjk_tokenize,
    _normalize_structured_summary,
    _safe_add_column,
    ensure_schema,
    fts_search,
    get_chunks,
    get_pinned_chunks,
    insert_chunk,
    is_pinned,
    open_db,
    pin_chunk,
    unpin_chunk,
)

__all__ = [
    "CHUNK_VERSION_FILE",
    "MEMORY_OS_DIR",
    "STORE_DB",
    "_cjk_tokenize",
    "_normalize_structured_summary",
    "_safe_add_column",
    "ensure_schema",
    "fts_search",
    "get_chunks",
    "get_pinned_chunks",
    "insert_chunk",
    "is_pinned",
    "open_db",
    "pin_chunk",
    "unpin_chunk",
]
