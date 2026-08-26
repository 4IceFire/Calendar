"""Service-token storage and validation for the TDeck HTTP API.

The module is deliberately independent from Flask so the web application and
``calendarctl`` can share the same token lifecycle implementation.  Only a
SHA-256 digest is stored; the high-entropy plaintext token is returned once at
creation time and cannot be recovered from ``auth.db``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SERVICE_TOKEN_SCOPES = (
    "read",
    "timers",
    "videohub",
    "tvs",
    "atem",
    "propresenter",
    "ccb",
    "pixie",
    "digico",
    "calendar",
    "config",
    "admin",
)

SERVICE_TOKEN_CONSTRAINT_KEYS = (
    "allowed_paths",
    "tv_targets",
    "videohub_outputs",
    "videohub_inputs",
    "videohub_presets",
    "atem_sources",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds")


def _parse_timestamp(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def normalize_scopes(scopes: Iterable[str]) -> list[str]:
    values = {str(scope or "").strip().lower() for scope in scopes}
    values.discard("")
    unknown = sorted(values - set(SERVICE_TOKEN_SCOPES) - {"*"})
    if unknown:
        raise ValueError(f"Unknown service-token scope(s): {', '.join(unknown)}")
    if "*" in values:
        return ["*"]
    return sorted(values)


def normalize_constraints(constraints: dict | None) -> dict:
    raw = constraints if isinstance(constraints, dict) else {}
    unknown = sorted(set(raw) - set(SERVICE_TOKEN_CONSTRAINT_KEYS))
    if unknown:
        raise ValueError(f"Unknown service-token constraint(s): {', '.join(unknown)}")
    result: dict[str, list] = {}
    for key in ("allowed_paths", "tv_targets", "atem_sources"):
        values = sorted({str(item or "").strip() for item in (raw.get(key) or []) if str(item or "").strip()})
        if key == "allowed_paths":
            for value in values:
                method, separator, path = value.partition(" ")
                if not separator or method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not path.startswith("/api/"):
                    raise ValueError("allowed_paths entries must look like 'POST /api/timers/apply'")
            values = [value.split(" ", 1)[0].upper() + " " + value.split(" ", 1)[1] for value in values]
        if values:
            result[key] = values
    for key in ("videohub_outputs", "videohub_inputs", "videohub_presets"):
        values = sorted({int(item) for item in (raw.get(key) or []) if int(item) > 0})
        if values:
            result[key] = values
    return result


def ensure_service_token_schema(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS service_tokens (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              description TEXT,
              token_prefix TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE,
              scopes_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT,
              expires_at TEXT,
              last_used_at TEXT,
              revoked_at TEXT,
              revoked_by TEXT,
              constraints_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(service_tokens)").fetchall()}
        if "constraints_json" not in columns:
            conn.execute("ALTER TABLE service_tokens ADD COLUMN constraints_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_service_tokens_prefix "
            "ON service_tokens(token_prefix)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_service_tokens_active "
            "ON service_tokens(revoked_at, expires_at)"
        )
        conn.commit()
    finally:
        conn.close()


def create_service_token(
    db_path: str | Path,
    *,
    name: str,
    scopes: Iterable[str],
    description: str = "",
    created_by: str = "CLI",
    expires_at: str | None = None,
    expires_in_days: int | None = None,
    constraints: dict | None = None,
) -> dict:
    token_name = str(name or "").strip()
    if not token_name:
        raise ValueError("Token name is required")
    scope_list = normalize_scopes(scopes)
    constraint_values = normalize_constraints(constraints)
    if not scope_list:
        raise ValueError("At least one scope is required")
    if expires_at and expires_in_days is not None:
        raise ValueError("Use either expires_at or expires_in_days, not both")
    expiry = _parse_timestamp(expires_at)
    if expires_at and expiry is None:
        raise ValueError("expires_at must be an ISO-8601 timestamp")
    if expires_in_days is not None:
        days = int(expires_in_days)
        if days <= 0:
            raise ValueError("expires_in_days must be positive")
        expiry = _utc_now() + timedelta(days=days)
    if expiry is not None and expiry <= _utc_now():
        raise ValueError("Token expiry must be in the future")

    # The independently random prefix allows safe identification without
    # exposing any useful portion of the bearer secret.
    prefix = secrets.token_hex(6)
    plaintext = f"tdk_{prefix}_{secrets.token_urlsafe(32)}"
    digest = token_digest(plaintext)
    ensure_service_token_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            INSERT INTO service_tokens(
              name,description,token_prefix,token_hash,scopes_json,created_at,
              created_by,expires_at,constraints_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                token_name,
                str(description or "").strip() or None,
                prefix,
                digest,
                json.dumps(scope_list),
                _timestamp(),
                str(created_by or "CLI").strip() or "CLI",
                _timestamp(expiry) if expiry else None,
                json.dumps(constraint_values, sort_keys=True),
            ),
        )
        conn.commit()
        token_id = int(cur.lastrowid)
    finally:
        conn.close()
    return {
        "id": token_id,
        "name": token_name,
        "description": str(description or "").strip(),
        "token_prefix": prefix,
        "scopes": scope_list,
        "created_at": _timestamp(),
        "expires_at": _timestamp(expiry) if expiry else None,
        "constraints": constraint_values,
        "token": plaintext,
    }


def _row_to_public_dict(row: sqlite3.Row) -> dict:
    try:
        scopes = normalize_scopes(json.loads(str(row["scopes_json"] or "[]")))
    except Exception:
        scopes = []
    try:
        constraints = normalize_constraints(json.loads(str(row["constraints_json"] or "{}")))
    except Exception:
        constraints = {}
    now = _utc_now()
    expiry = _parse_timestamp(row["expires_at"])
    return {
        "id": int(row["id"]),
        "name": str(row["name"] or ""),
        "description": str(row["description"] or ""),
        "token_prefix": str(row["token_prefix"] or ""),
        "scopes": scopes,
        "constraints": constraints,
        "created_at": str(row["created_at"] or ""),
        "created_by": str(row["created_by"] or ""),
        "expires_at": str(row["expires_at"] or ""),
        "last_used_at": str(row["last_used_at"] or ""),
        "revoked_at": str(row["revoked_at"] or ""),
        "revoked_by": str(row["revoked_by"] or ""),
        "expired": bool(expiry and expiry <= now),
        "active": not bool(row["revoked_at"]) and not bool(expiry and expiry <= now),
    }


def list_service_tokens(db_path: str | Path) -> list[dict]:
    ensure_service_token_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM service_tokens ORDER BY id ASC"
        ).fetchall()
        return [_row_to_public_dict(row) for row in rows]
    finally:
        conn.close()


def revoke_service_token(
    db_path: str | Path,
    identifier: str | int,
    *,
    revoked_by: str = "CLI",
) -> dict | None:
    ensure_service_token_schema(db_path)
    raw = str(identifier or "").strip()
    if not raw:
        raise ValueError("Token ID or prefix is required")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if raw.isdigit():
            row = conn.execute(
                "SELECT * FROM service_tokens WHERE id=?", (int(raw),)
            ).fetchone()
        else:
            rows = conn.execute(
                "SELECT * FROM service_tokens WHERE token_prefix LIKE ?",
                (raw + "%",),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("Token prefix is ambiguous; use the numeric ID")
            row = rows[0] if rows else None
        if row is None:
            return None
        if not row["revoked_at"]:
            conn.execute(
                "UPDATE service_tokens SET revoked_at=?, revoked_by=? WHERE id=?",
                (_timestamp(), str(revoked_by or "CLI"), int(row["id"])),
            )
            conn.commit()
        updated = conn.execute(
            "SELECT * FROM service_tokens WHERE id=?", (int(row["id"]),)
        ).fetchone()
        return _row_to_public_dict(updated)
    finally:
        conn.close()


def authenticate_service_token(
    db_path: str | Path,
    plaintext: str,
    *,
    update_last_used: bool = True,
) -> tuple[dict | None, str | None]:
    raw = str(plaintext or "").strip()
    if not raw or len(raw) > 512:
        return None, "invalid_token"
    ensure_service_token_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM service_tokens WHERE token_hash=?", (token_digest(raw),)
        ).fetchone()
        if row is None:
            return None, "invalid_token"
        public = _row_to_public_dict(row)
        if row["revoked_at"]:
            return None, "revoked_token"
        if public["expired"]:
            return None, "expired_token"
        if update_last_used:
            previous = _parse_timestamp(row["last_used_at"])
            if previous is None or (_utc_now() - previous).total_seconds() >= 60:
                now = _timestamp()
                conn.execute(
                    "UPDATE service_tokens SET last_used_at=? WHERE id=?",
                    (now, int(row["id"])),
                )
                conn.commit()
                public["last_used_at"] = now
        return public, None
    finally:
        conn.close()


def token_allows(scopes: Iterable[str], required_scope: str, *, read_only: bool) -> bool:
    granted = {str(scope or "").strip().lower() for scope in scopes}
    if "*" in granted:
        return True
    required = str(required_scope or "read").strip().lower() or "read"
    if read_only and "read" in granted:
        return True
    return required in granted
