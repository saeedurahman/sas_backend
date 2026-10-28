"""``Idempotency-Key`` support for mutating endpoints.

Contract (per business, key unique):

* no header                       -> request runs normally;
* new key                         -> request runs, the response is stored;
* same key + same request         -> the stored response is replayed
                                     (``Idempotent-Replay: true``), nothing re-runs;
* same key + different request    -> 409;
* same key while the first is still running -> waits briefly, then 409.

Failed requests (4xx/5xx) are not stored, so the client may fix the problem and
retry with the same key. Bookkeeping uses its own short transactions so it is
unaffected by the business transaction's commit/rollback.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.database import AsyncSessionLocal

HEADER = "Idempotency-Key"
MAX_KEY_LENGTH = 128
RETENTION = timedelta(hours=24)
_STALE_IN_FLIGHT = timedelta(minutes=2)
_WAIT_STEP = 0.2
_WAIT_TOTAL = 15.0


def request_fingerprint(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method.upper()} {path}\n{canonical}".encode()).hexdigest()


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _claim(
    business_id: UUID, user_id: UUID, key: str, method: str, path: str, fingerprint: str
) -> tuple[bool, dict | None]:
    """Try to own the key. Returns (owned, existing_row_or_None)."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM idempotency_keys "
                    "WHERE business_id = :b AND expires_at < :now"
                ),
                {"b": business_id, "now": now},
            )
            inserted = (
                await session.execute(
                    text(
                        """
                        INSERT INTO idempotency_keys (
                            business_id, user_id, key, method, path,
                            request_hash, expires_at
                        ) VALUES (:b, :u, :k, :m, :p, :h, :exp)
                        ON CONFLICT (business_id, key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "b": business_id,
                        "u": user_id,
                        "k": key,
                        "m": method,
                        "p": path,
                        "h": fingerprint,
                        "exp": now + RETENTION,
                    },
                )
            ).first()
            if inserted is not None:
                return True, None
            row = (
                await session.execute(
                    text(
                        "SELECT request_hash, response_status, response_body, created_at "
                        "FROM idempotency_keys WHERE business_id = :b AND key = :k"
                    ),
                    {"b": business_id, "k": key},
                )
            ).mappings().first()
            return False, dict(row) if row is not None else None


async def _store(business_id: UUID, key: str, status_code: int, body: Any) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE idempotency_keys "
                    "SET response_status = :s, response_body = CAST(:body AS jsonb) "
                    "WHERE business_id = :b AND key = :k"
                ),
                {"s": status_code, "body": json.dumps(body), "b": business_id, "k": key},
            )


async def _release(business_id: UUID, key: str) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM idempotency_keys "
                    "WHERE business_id = :b AND key = :k AND response_status IS NULL"
                ),
                {"b": business_id, "k": key},
            )


async def _release_stale(business_id: UUID, key: str) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM idempotency_keys "
                    "WHERE business_id = :b AND key = :k AND response_status IS NULL "
                    "AND created_at < :cutoff"
                ),
                {
                    "b": business_id,
                    "k": key,
                    "cutoff": datetime.now(timezone.utc) - _STALE_IN_FLIGHT,
                },
            )


def _replay(row: dict) -> JSONResponse:
    return JSONResponse(
        status_code=row["response_status"],
        content=row["response_body"],
        headers={"Idempotent-Replay": "true"},
    )


async def run_idempotent(
    request: Request,
    *,
    business_id: UUID,
    user_id: UUID,
    body: BaseModel | dict | None,
    response_model: type[BaseModel],
    success_status: int,
    handler: Callable[[], Awaitable[Any]],
) -> Any:
    """Run ``handler`` once per Idempotency-Key; see module docstring."""
    key = request.headers.get(HEADER)
    if key is None:
        return await handler()
    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{HEADER} must be 1-{MAX_KEY_LENGTH} characters",
        )

    payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else body
    path = request.url.path
    fingerprint = request_fingerprint(request.method, path, payload)

    waited = 0.0
    while True:
        owned, row = await _claim(
            business_id, user_id, key, request.method, path, fingerprint
        )
        if owned:
            break
        if row is None:  # deleted between INSERT and SELECT; try again
            continue
        if row["request_hash"] != fingerprint:
            raise _conflict(
                f"{HEADER} was already used with a different request; "
                "send a new key for a new request"
            )
        if row["response_status"] is not None:
            return _replay(row)
        created = row["created_at"]
        if datetime.now(timezone.utc) - created > _STALE_IN_FLIGHT:
            await _release_stale(business_id, key)
            continue
        if waited >= _WAIT_TOTAL:
            raise _conflict("A request with this Idempotency-Key is still being processed")
        await asyncio.sleep(_WAIT_STEP)
        waited += _WAIT_STEP

    try:
        result = await handler()
    except BaseException:
        await _release(business_id, key)
        raise

    try:
        serialized = response_model.model_validate(result, from_attributes=True)
        await _store(
            business_id, key, success_status, serialized.model_dump(mode="json")
        )
        return serialized
    except Exception:
        # The business transaction is already committed; never fail the caller
        # over bookkeeping. Without a stored response a retry would re-run, so
        # drop the claim rather than leave it dangling.
        await _release(business_id, key)
        return result
