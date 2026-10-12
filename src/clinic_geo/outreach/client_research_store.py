"""Transaction-scoped persistence utilities for supplied city research."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .db import connect, default_db_path, initialize_database, transaction


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def utc(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc)


class ClientResearchStore:
    def __init__(self, root: Path | str, db_path=None):
        self.path = Path(db_path) if db_path else default_db_path(root)
        initialize_database(self.path)

    @contextmanager
    def session(self, *, write=False):
        with closing(connect(self.path)) as connection:
            if write:
                with transaction(connection, immediate=True):
                    yield connection
            else:
                yield connection
