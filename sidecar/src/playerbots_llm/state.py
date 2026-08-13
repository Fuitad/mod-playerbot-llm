"""The durable state one request touches, and its MySQL implementation.

Two things live here and they are deliberately separate. :class:`SidecarState` is the
narrow set of operations the request path depends on. :class:`MySqlSidecarState` is the
only production implementation, and it is pure delegation: it acquires a connection,
hands it to :mod:`playerbots_llm.ledger`, and returns what comes back.

The delegation is kept free of decisions on purpose. Every rule about money lives in
:mod:`playerbots_llm.budget` as pure arithmetic, and every rule about transactions
lives in the ledger where a real MySQL proves it. A rule invented in this layer would be
a rule no test executes, because the unit tests substitute a double for exactly this
interface.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

import aiomysql

from playerbots_llm import ledger, protocol, schema
from playerbots_llm import store as store_module
from playerbots_llm.budget import AdmissionDecision, BudgetState, RequestKind, RequestPriority

if TYPE_CHECKING:
    # Only for typing: app imports this module, so importing it back at runtime would
    # be circular. The settings object is app's to parse and this module's to consume.
    from playerbots_llm.app import PlayerbotsDatabaseSettings

# Small on purpose. Every request already serializes behind the service's generation
# lock, so the pool exists for reconnection and not for parallelism.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4

# MySQL closes an idle connection after `wait_timeout`, 8 hours by default. A sidecar on
# a quiet realm sits idle far longer than that, and a recycled connection is the
# difference between the next request working and it failing with "server has gone away".
POOL_RECYCLE_SECONDS = 3600

# What a caller should catch around opening or using the database. The driver's own error
# base does NOT derive from OSError, so catching OSError alone lets a refused connection
# escape as a bare traceback rather than the bounded refusal every caller here promises.
DatabaseUnavailable = (OSError, aiomysql.Error)


class SidecarState(Protocol):
    """Every durable operation the request path can perform.

    Narrow on purpose. The service depends on this and on nothing else about storage, so
    a unit test can substitute an in-memory double and still be testing the service's own
    ordering decisions rather than re-testing MySQL.
    """

    async def purge_pending_bot_data(self) -> int: ...

    async def try_begin_ambient(self, *, messages_per_hour: int, now: datetime) -> bool: ...

    async def record_profile(self, request: protocol.ChatRequest, *, now: datetime) -> None: ...

    async def get_profile(self, *, bot_guid: int) -> dict[str, object] | None: ...

    async def recent_turns(self, *, bot_guid: int) -> list[tuple[str, str]]: ...

    async def append_turn(self, *, bot_guid: int, role: str, content: str, now: datetime) -> None: ...

    async def record_career_decision(
        self,
        *,
        bot_guid: int,
        career_version: int,
        candidate_token: str,
        spending_style: str,
        now: datetime,
    ) -> None: ...

    async def reserve(
        self,
        *,
        request_kind: RequestKind,
        model: str,
        max_cost_nano: int | None,
        priority: RequestPriority,
        now: datetime,
    ) -> tuple[AdmissionDecision, ledger.Reservation | None]: ...

    async def settle(
        self, *, reservation: ledger.Reservation, actual_cost_nano: int, now: datetime
    ) -> ledger.SettlementReceipt: ...

    async def release(self, *, reservation: ledger.Reservation) -> bool: ...

    async def budget_state(self, *, now: datetime) -> BudgetState: ...


class MySqlSidecarState:
    """The Playerbots database, behind :class:`SidecarState`."""

    def __init__(
        self, pool: aiomysql.Pool, book: ledger.BudgetLedger, store: store_module.SidecarStore
    ) -> None:
        self._pool = pool
        self._ledger = book
        self._store = store

    async def purge_pending_bot_data(self) -> int:
        async with self._connection() as connection:
            return await self._store.purge_pending_bot_data(connection)

    @asynccontextmanager
    async def _connection(self):
        """One pooled connection, guaranteed to be returned with no transaction open.

        aiomysql DISCARDS a connection that is released while a transaction is still
        open, so a read that never closes its transaction quietly destroys and reopens a
        socket on every single request. Autocommit is off, so even a bare SELECT starts
        one. The ledger's writes commit themselves and leave nothing open; this closes
        what the reads open, and checks first so the common case costs no round trip.
        """

        async with self._pool.acquire() as connection:
            try:
                yield connection
            finally:
                if connection.get_transaction_status():
                    await connection.rollback()

    async def try_begin_ambient(self, *, messages_per_hour: int, now: datetime) -> bool:
        async with self._connection() as connection:
            return await self._store.try_begin_ambient(
                connection, messages_per_hour=messages_per_hour, now=now
            )

    async def record_profile(self, request: protocol.ChatRequest, *, now: datetime) -> None:
        async with self._connection() as connection:
            await self._store.record_profile(
                connection,
                bot_guid=request.bot_guid,
                profile_version=request.profile_version,
                crafting_affinity=request.crafting_affinity,
                gathering_affinity=request.gathering_affinity,
                exploration_affinity=request.exploration_affinity,
                sociability=request.sociability,
                voice=request.voice,
                bot_name=request.bot_name,
                now=now,
            )

    async def get_profile(self, *, bot_guid: int) -> dict[str, object] | None:
        async with self._connection() as connection:
            return await self._store.get_profile(connection, bot_guid=bot_guid)

    async def recent_turns(self, *, bot_guid: int) -> list[tuple[str, str]]:
        async with self._connection() as connection:
            return await self._store.recent_turns(connection, bot_guid=bot_guid)

    async def append_turn(self, *, bot_guid: int, role: str, content: str, now: datetime) -> None:
        async with self._connection() as connection:
            await self._store.append_turn(connection, bot_guid=bot_guid, role=role, content=content, now=now)

    async def record_career_decision(
        self,
        *,
        bot_guid: int,
        career_version: int,
        candidate_token: str,
        spending_style: str,
        now: datetime,
    ) -> None:
        async with self._connection() as connection:
            await self._store.record_career_decision(
                connection,
                bot_guid=bot_guid,
                career_version=career_version,
                candidate_token=candidate_token,
                spending_style=spending_style,
                now=now,
            )

    async def reserve(
        self,
        *,
        request_kind: RequestKind,
        model: str,
        max_cost_nano: int | None,
        priority: RequestPriority,
        now: datetime,
    ) -> tuple[AdmissionDecision, ledger.Reservation | None]:
        async with self._connection() as connection:
            return await self._ledger.reserve(
                connection,
                request_kind=request_kind,
                model=model,
                max_cost_nano=max_cost_nano,
                priority=priority,
                now=now,
            )

    async def settle(
        self, *, reservation: ledger.Reservation, actual_cost_nano: int, now: datetime
    ) -> ledger.SettlementReceipt:
        async with self._connection() as connection:
            return await self._ledger.settle(
                connection, reservation=reservation, actual_cost_nano=actual_cost_nano, now=now
            )

    async def release(self, *, reservation: ledger.Reservation) -> bool:
        async with self._connection() as connection:
            return await self._ledger.release(connection, reservation=reservation)

    async def budget_state(self, *, now: datetime) -> BudgetState:
        async with self._connection() as connection:
            return await self._ledger.snapshot(connection, now=now)


async def open_state(
    settings: PlayerbotsDatabaseSettings, *, ceiling_nano: int, reserve_ratio: Decimal
) -> tuple[MySqlSidecarState, aiomysql.Pool]:
    """Opens the pool, creates the schema if it is missing, and returns both.

    The pool is handed back alongside the state because the caller owns its lifetime:
    ``serve`` closes it on shutdown, and a CLI command closes it when it is done. Nothing
    here registers an exit hook, so a pool is never left open by a code path that
    forgot it existed.
    """

    pool = await aiomysql.create_pool(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=settings.database,
        minsize=POOL_MIN_SIZE,
        maxsize=POOL_MAX_SIZE,
        # Off, because the ledger's transactions are the whole point of it.
        autocommit=False,
        charset="utf8mb4",
        pool_recycle=POOL_RECYCLE_SECONDS,
    )

    initialized = False
    try:
        book = ledger.BudgetLedger(ceiling_nano, reserve_ratio)
        store = store_module.SidecarStore()

        async with pool.acquire() as connection:
            await schema.ensure_schema(connection)
            # ensure_schema runs DDL, which MySQL commits implicitly, but the connection is
            # still handed back to a pool that discards anything with an open transaction.
            if connection.get_transaction_status():
                await connection.rollback()

        state = MySqlSidecarState(pool, book, store)
        initialized = True
        return state, pool
    finally:
        if not initialized:
            pool.close()
            await pool.wait_closed()


async def close_pool(pool: aiomysql.Pool) -> None:
    pool.close()
    await pool.wait_closed()
