"""The non-budget durable state: profiles, dialogue memory, careers, the ambient rate.

Everything here used to live in a private SQLite file. Sharing the Playerbots database
instead removes a second thing to back up, a second thing to migrate, and a file whose
absence or corruption was a failure mode nobody monitored.

Separate from :mod:`playerbot_llm.ledger` because the two share nothing but a
connection and the lock table. Money has a ceiling, a circuit breaker, and a reconciled
schema it does not own; this has none of that, and reading one should not mean paging
through the other.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from playerbot_llm.schema import (
    CONVERSATION_LOCK_BUCKETS,
    LedgerError,
    acquire_named_lock,
)

# Bounded per bot conversation memory, trimmed on every write. An unbounded history is
# an unbounded prompt, which is an unbounded cost, which is the thing this whole module
# exists to prevent.
CONVERSATION_TURN_LIMIT = 12

AMBIENT_WINDOW = timedelta(hours=1)
MAX_AMBIENT_MESSAGES_PER_HOUR = 6


class SidecarStore:
    """The non-budget durable state, on the shared Playerbots database.

    Everything here used to live in a private SQLite file. Sharing the Playerbots
    database instead removes a second thing to back up, a second thing to migrate, and a
    file whose absence or corruption was a failure mode nobody monitored.

    Like :class:`BudgetLedger`, every method takes an open connection rather than owning
    a pool, so the caller decides connection lifetime.
    """

    async def record_profile(
        self,
        connection,
        *,
        bot_guid: int,
        profile_version: int,
        crafting_affinity: int,
        gathering_affinity: int,
        exploration_affinity: int,
        sociability: int,
        voice: str,
        bot_name: str,
        now: datetime,
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO playerbot_llm_profile (bot_guid, profile_version, crafting_affinity, "
                "gathering_affinity, exploration_affinity, sociability, voice, bot_name, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE profile_version = VALUES(profile_version), "
                "crafting_affinity = VALUES(crafting_affinity), "
                "gathering_affinity = VALUES(gathering_affinity), "
                "exploration_affinity = VALUES(exploration_affinity), "
                "sociability = VALUES(sociability), voice = VALUES(voice), "
                "bot_name = VALUES(bot_name), updated_at = VALUES(updated_at)",
                (
                    bot_guid,
                    profile_version,
                    crafting_affinity,
                    gathering_affinity,
                    exploration_affinity,
                    sociability,
                    voice,
                    bot_name,
                    now,
                ),
            )
        await connection.commit()

    async def get_profile(self, connection, *, bot_guid: int) -> dict[str, object] | None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT profile_version, crafting_affinity, gathering_affinity, exploration_affinity, "
                "sociability, voice, bot_name FROM playerbot_llm_profile WHERE bot_guid = %s",
                (bot_guid,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "profile_version": int(row[0]),
            "crafting_affinity": int(row[1]),
            "gathering_affinity": int(row[2]),
            "exploration_affinity": int(row[3]),
            "sociability": int(row[4]),
            "voice": row[5],
            "bot_name": row[6],
        }

    async def append_turn(self, connection, *, bot_guid: int, role: str, content: str, now: datetime) -> None:
        """Appends one turn and trims the bot's history to the limit.

        Trimmed on write rather than on read, so the table is bounded on disk and not
        merely in what a query returns. The subquery is wrapped in a derived table
        because MySQL will not read from the table it is deleting from otherwise.
        """

        if role not in ("user", "assistant"):
            raise LedgerError(f"unsupported conversation role: {role!r}")

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                # Serialized per bot. Two concurrent appends for the same bot would each
                # insert and then scan and delete the same id range, which deadlocks. The
                # lock is per bot rather than global so unrelated bots never wait.
                # Bucketed rather than one key per bot, so the lock table is bounded by
                # CONVERSATION_LOCK_BUCKETS rather than by how many bots the server has
                # ever seen. Two bots sharing a bucket wait for each other, which costs a
                # little contention and buys a table that cannot grow.
                await acquire_named_lock(cursor, f"conversation:{bot_guid % CONVERSATION_LOCK_BUCKETS}")
                await cursor.execute(
                    "INSERT INTO playerbot_llm_conversation_turn (bot_guid, role, content, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (bot_guid, role, content, now),
                )
                await cursor.execute(
                    "DELETE FROM playerbot_llm_conversation_turn WHERE bot_guid = %s AND id NOT IN "
                    "(SELECT id FROM (SELECT id FROM playerbot_llm_conversation_turn "
                    "WHERE bot_guid = %s ORDER BY id DESC LIMIT %s) AS keep)",
                    (bot_guid, bot_guid, CONVERSATION_TURN_LIMIT),
                )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    async def recent_turns(self, connection, *, bot_guid: int) -> list[tuple[str, str]]:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT role, content FROM playerbot_llm_conversation_turn "
                "WHERE bot_guid = %s ORDER BY id ASC",
                (bot_guid,),
            )
            rows = await cursor.fetchall()

        return [(row[0], row[1]) for row in rows]

    async def record_career_decision(
        self,
        connection,
        *,
        bot_guid: int,
        career_version: int,
        candidate_token: str,
        spending_style: str,
        now: datetime,
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO playerbot_llm_career_decision "
                "(bot_guid, career_version, candidate_token, spending_style, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE career_version = VALUES(career_version), "
                "candidate_token = VALUES(candidate_token), spending_style = VALUES(spending_style), "
                "updated_at = VALUES(updated_at)",
                (bot_guid, career_version, candidate_token, spending_style, now),
            )
        await connection.commit()

    async def get_career_decision(self, connection, *, bot_guid: int) -> dict[str, object] | None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT career_version, candidate_token, spending_style "
                "FROM playerbot_llm_career_decision WHERE bot_guid = %s",
                (bot_guid,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "career_version": int(row[0]),
            "candidate_token": row[1],
            "spending_style": row[2],
        }

    async def try_begin_ambient(self, connection, *, messages_per_hour: int, now: datetime) -> bool:
        """Consumes one ambient slot if the rolling hour has room.

        The whole check and insert run in one transaction with the count taken under a
        write lock, so two sidecar workers cannot both read the same count and both
        decide there was room.
        """

        if not 1 <= messages_per_hour <= MAX_AMBIENT_MESSAGES_PER_HOUR:
            return False

        cutoff = now - AMBIENT_WINDOW
        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                # One guard row rather than locking the whole attempts table. A bare
                # COUNT(*) FOR UPDATE locks every row it scans, which blocks unrelated
                # inserts and gets more expensive as the table grows, and under gap
                # locking two callers can deadlock on it.
                await acquire_named_lock(cursor, "ambient")

                await cursor.execute(
                    "DELETE FROM playerbot_llm_ambient_attempt WHERE created_at <= %s",
                    (cutoff,),
                )
                # Predicated on the indexed column, so this reads an index range rather
                # than the table.
                await cursor.execute(
                    "SELECT COUNT(*) FROM playerbot_llm_ambient_attempt WHERE created_at > %s",
                    (cutoff,),
                )
                row = await cursor.fetchone()
                if row is not None and int(row[0]) >= messages_per_hour:
                    await connection.commit()
                    return False

                await cursor.execute(
                    "INSERT INTO playerbot_llm_ambient_attempt (created_at) VALUES (%s)",
                    (now,),
                )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return True
