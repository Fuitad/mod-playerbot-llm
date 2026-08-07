"""The shared shape of the Playerbots database, and who owns which part of it.

Two owners, and the boundary is the whole point of this module. The sidecar creates the
five tables in :data:`SCHEMA_STATEMENTS` and nothing else. The budget tables and the
social runtime control row belong to the mod-playerbots SQL revisions; this process
verifies they are there and refuses to start if they are not.

That refusal replaced a second, incompatible declaration of the same two budget tables in
this codebase. Both used ``CREATE TABLE IF NOT EXISTS``, so whichever ran first won and
the other silently did nothing. On a deployed realm that was the module, which means every
write the sidecar made to a column only its own definition had would have failed at
runtime and nowhere else.

:func:`acquire_named_lock` lives here rather than beside either caller, because both the
budget ledger and the durable store serialize on the same table and a lock helper that
belongs to one of them invites a second one for the other.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

# How many named locks the per bot conversation trim spreads across. Bounded so the lock
# table cannot grow with the bot roster; wide enough that unrelated bots rarely collide.
CONVERSATION_LOCK_BUCKETS = 256

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS playerbot_llm_lock (
        `lock_key` VARCHAR(64) NOT NULL,
        PRIMARY KEY (`lock_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Named serialization points, from a bounded key set. Never deleted.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_llm_profile (
        `bot_guid` BIGINT UNSIGNED NOT NULL,
        `profile_version` INT UNSIGNED NOT NULL,
        `crafting_affinity` TINYINT UNSIGNED NOT NULL,
        `gathering_affinity` TINYINT UNSIGNED NOT NULL,
        `exploration_affinity` TINYINT UNSIGNED NOT NULL,
        `sociability` TINYINT UNSIGNED NOT NULL,
        `voice` VARCHAR(32) NOT NULL,
        `bot_name` VARCHAR(48) NOT NULL,
        `updated_at` DATETIME NOT NULL,
        PRIMARY KEY (`bot_guid`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Last observed personality profile per bot, written by the worldserver.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_llm_conversation_turn (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `bot_guid` BIGINT UNSIGNED NOT NULL,
        `role` ENUM('user','assistant') NOT NULL,
        `content` TEXT NOT NULL,
        `created_at` DATETIME NOT NULL,
        PRIMARY KEY (`id`),
        KEY `ix_bot` (`bot_guid`, `id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Bounded per bot conversation memory. Trimmed on write, never unbounded.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_llm_career_decision (
        `bot_guid` BIGINT UNSIGNED NOT NULL,
        `career_version` INT UNSIGNED NOT NULL,
        `candidate_token` VARCHAR(64) NOT NULL,
        `spending_style` VARCHAR(32) NOT NULL,
        `updated_at` DATETIME NOT NULL,
        PRIMARY KEY (`bot_guid`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='One current career decision per bot.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_llm_ambient_attempt (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `created_at` DATETIME NOT NULL,
        PRIMARY KEY (`id`),
        KEY `ix_created` (`created_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Rolling hourly ambient rate, surviving restart.'
    """,
)

# The budget tables are NOT in the list above, deliberately.
#
# `playerbot_llm_daily_budget`, `playerbot_llm_budget_reservation`, and the
# `playerbot_social_runtime_control` row that carries the circuit breaker belong to
# mod-playerbots and are created by its SQL revisions. The sidecar used to carry a second
# definition of the first two here, with different column names, integer nano money
# instead of decimal dollars, and a breaker on the daily row. Both definitions used
# CREATE TABLE IF NOT EXISTS, so whichever ran first won and the other silently did
# nothing: on the deployed database that was the module, and every write this file made to
# columns only its own DDL had would have failed at runtime and nowhere else.
#
# One owner, and it is the module. This process verifies the tables are there and refuses
# to start if they are not, rather than papering over a database the schema revisions
# never reached.
REQUIRED_MODULE_TABLES = (
    "playerbot_llm_daily_budget",
    "playerbot_llm_budget_reservation",
    "playerbot_social_runtime_control",
)

# The other half of that split: the tables SCHEMA_STATEMENTS above creates. Named here so
# the ownership boundary is one list rather than a property of statement order, and so a
# test can assert that a refused start left none of them behind.
SIDECAR_OWNED_TABLES = (
    "playerbot_llm_lock",
    "playerbot_llm_profile",
    "playerbot_llm_conversation_turn",
    "playerbot_llm_career_decision",
    "playerbot_llm_ambient_attempt",
)

LEGACY_PROVIDER_TABLES = (
    "playerbot_claude_daily_budget",
    "playerbot_claude_budget_reservation",
    "playerbot_claude_lock",
    "playerbot_claude_profile",
    "playerbot_claude_conversation_turn",
    "playerbot_claude_career_decision",
    "playerbot_claude_ambient_attempt",
)

NEUTRAL_BUDGET_IDENTIFIERS = (
    "ck_llm_daily_budget_reserved",
    "ck_llm_daily_budget_spent",
    "uk_llm_reservation_public_id",
    "ix_llm_reservation_day_state",
    "ix_llm_reservation_expiry",
    "ck_llm_reservation_max_cost",
    "ck_llm_reservation_actual_cost",
)

LEGACY_BUDGET_IDENTIFIERS = tuple(
    identifier.replace("_llm_", "_claude_") for identifier in NEUTRAL_BUDGET_IDENTIFIERS
)

PROVIDER_COLUMN_SHAPES = {
    "playerbot_llm_daily_budget": (
        ("budget_date", "date", "NO", None, False, False),
        ("reserved_usd", "decimal(12,6)", "NO", "0", False, False),
        ("spent_usd", "decimal(12,6)", "NO", "0", False, False),
        ("created_at", "timestamp", "NO", "current_timestamp", False, False),
        ("updated_at", "timestamp", "NO", "current_timestamp", False, True),
    ),
    "playerbot_llm_budget_reservation": (
        ("id", "bigint unsigned", "NO", None, True, False),
        ("public_id", "char(36)", "NO", None, False, False),
        ("budget_date", "date", "NO", None, False, False),
        (
            "request_kind",
            "enum('chat_response','backstory_generation','memory_extraction',"
            "'moderation_classification','career_generation')",
            "NO",
            None,
            False,
            False,
        ),
        (
            "priority_lane",
            "enum('unspecified','direct_human','mixed_human_bot','career_generation',"
            "'bot_only_continuation','new_starter','background_extraction')",
            "NO",
            "unspecified",
            False,
            False,
        ),
        ("model", "varchar(64)", "NO", None, False, False),
        ("max_cost_usd", "decimal(12,6)", "NO", None, False, False),
        ("actual_cost_usd", "decimal(12,6)", "YES", None, False, False),
        (
            "state",
            "enum('reserved','completed','released','expired')",
            "NO",
            "reserved",
            False,
            False,
        ),
        ("created_at", "timestamp", "NO", "current_timestamp", False, False),
        ("expires_at", "datetime", "NO", None, False, False),
        ("settled_at", "datetime", "YES", None, False, False),
    ),
    "playerbot_llm_lock": (("lock_key", "varchar(64)", "NO", None, False, False),),
    "playerbot_llm_profile": (
        ("bot_guid", "bigint unsigned", "NO", None, False, False),
        ("profile_version", "int unsigned", "NO", None, False, False),
        ("crafting_affinity", "tinyint unsigned", "NO", None, False, False),
        ("gathering_affinity", "tinyint unsigned", "NO", None, False, False),
        ("exploration_affinity", "tinyint unsigned", "NO", None, False, False),
        ("sociability", "tinyint unsigned", "NO", None, False, False),
        ("voice", "varchar(32)", "NO", None, False, False),
        ("bot_name", "varchar(48)", "NO", None, False, False),
        ("updated_at", "datetime", "NO", None, False, False),
    ),
    "playerbot_llm_conversation_turn": (
        ("id", "bigint unsigned", "NO", None, True, False),
        ("bot_guid", "bigint unsigned", "NO", None, False, False),
        ("role", "enum('user','assistant')", "NO", None, False, False),
        ("content", "text", "NO", None, False, False),
        ("created_at", "datetime", "NO", None, False, False),
    ),
    "playerbot_llm_career_decision": (
        ("bot_guid", "bigint unsigned", "NO", None, False, False),
        ("career_version", "int unsigned", "NO", None, False, False),
        ("candidate_token", "varchar(64)", "NO", None, False, False),
        ("spending_style", "varchar(32)", "NO", None, False, False),
        ("updated_at", "datetime", "NO", None, False, False),
    ),
    "playerbot_llm_ambient_attempt": (
        ("id", "bigint unsigned", "NO", None, True, False),
        ("created_at", "datetime", "NO", None, False, False),
    ),
}

PROVIDER_INDEX_SHAPES = {
    "playerbot_llm_daily_budget": {
        "PRIMARY": (False, ("budget_date",)),
    },
    "playerbot_llm_budget_reservation": {
        "PRIMARY": (False, ("id",)),
        "uk_llm_reservation_public_id": (False, ("public_id",)),
        "ix_llm_reservation_day_state": (True, ("budget_date", "state")),
        "ix_llm_reservation_expiry": (True, ("state", "expires_at")),
    },
    "playerbot_llm_lock": {"PRIMARY": (False, ("lock_key",))},
    "playerbot_llm_profile": {"PRIMARY": (False, ("bot_guid",))},
    "playerbot_llm_conversation_turn": {
        "PRIMARY": (False, ("id",)),
        "ix_bot": (True, ("bot_guid", "id")),
    },
    "playerbot_llm_career_decision": {"PRIMARY": (False, ("bot_guid",))},
    "playerbot_llm_ambient_attempt": {
        "PRIMARY": (False, ("id",)),
        "ix_created": (True, ("created_at",)),
    },
}

PROVIDER_CHECK_SHAPES = {
    "playerbot_llm_daily_budget": {
        "ck_llm_daily_budget_reserved": "reserved_usd>=0",
        "ck_llm_daily_budget_spent": "spent_usd>=0",
    },
    "playerbot_llm_budget_reservation": {
        "ck_llm_reservation_max_cost": "max_cost_usd>=0",
        "ck_llm_reservation_actual_cost": "actual_cost_usdisnulloractual_cost_usd>=0",
    },
}


class LedgerError(RuntimeError):
    """The database could not complete an operation it was asked to perform."""


def _normalized_default(value: object) -> str | None:
    if value is None:
        return None

    text = str(value).lower().replace("()", "")
    try:
        if Decimal(text) == 0:
            return "0"
    except InvalidOperation:
        pass
    return text


def _normalized_check(value: object) -> str:
    return "".join(character for character in str(value).lower() if character not in "`() \t\r\n")


async def _validate_provider_table_shapes(connection, tables: set[str]) -> None:
    if not tables:
        return

    placeholders = ", ".join("%s" for _ in tables)
    parameters = tuple(sorted(tables))
    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT table_name, column_name, column_type, is_nullable, column_default, extra "  # noqa: S608
            "FROM information_schema.columns WHERE table_schema = DATABASE() "
            f"AND table_name IN ({placeholders}) ORDER BY table_name, ordinal_position",
            parameters,
        )
        column_rows = await cursor.fetchall()
        await cursor.execute(
            "SELECT table_name, index_name, non_unique, seq_in_index, column_name "  # noqa: S608
            "FROM information_schema.statistics WHERE table_schema = DATABASE() "
            f"AND table_name IN ({placeholders}) ORDER BY table_name, index_name, seq_in_index",
            parameters,
        )
        index_rows = await cursor.fetchall()
        await cursor.execute(
            "SELECT tc.table_name, cc.constraint_name, cc.check_clause "  # noqa: S608
            "FROM information_schema.table_constraints AS tc "
            "JOIN information_schema.check_constraints AS cc "
            "ON cc.constraint_schema = tc.constraint_schema "
            "AND cc.constraint_name = tc.constraint_name "
            "WHERE tc.constraint_schema = DATABASE() AND tc.constraint_type = 'CHECK' "
            f"AND tc.table_name IN ({placeholders}) ORDER BY tc.table_name, cc.constraint_name",
            parameters,
        )
        check_rows = await cursor.fetchall()

    actual_columns: dict[str, list[tuple[str, str, str, str | None, bool, bool]]] = {}
    for table, name, column_type, nullable, default, extra in column_rows:
        normalized_extra = str(extra).lower()
        actual_columns.setdefault(table, []).append(
            (
                name,
                str(column_type).lower(),
                nullable,
                _normalized_default(default),
                "auto_increment" in normalized_extra,
                "on update current_timestamp" in normalized_extra,
            )
        )

    actual_indexes: dict[str, dict[str, tuple[bool, tuple[str, ...]]]] = {}
    index_parts: dict[tuple[str, str], list[str]] = {}
    index_uniqueness: dict[tuple[str, str], bool] = {}
    for table, name, non_unique, _sequence, column in index_rows:
        index_parts.setdefault((table, name), []).append(column)
        index_uniqueness[(table, name)] = bool(non_unique)
    for (table, name), columns in index_parts.items():
        actual_indexes.setdefault(table, {})[name] = (index_uniqueness[(table, name)], tuple(columns))

    actual_checks: dict[str, dict[str, str]] = {}
    for table, name, clause in check_rows:
        actual_checks.setdefault(table, {})[name] = _normalized_check(clause)

    for table in sorted(tables):
        expected_columns = list(PROVIDER_COLUMN_SHAPES[table])
        if actual_columns.get(table) != expected_columns:
            raise LedgerError(f"unexpected provider table shape: {table} columns")
        if actual_indexes.get(table, {}) != PROVIDER_INDEX_SHAPES[table]:
            raise LedgerError(f"unexpected provider table shape: {table} indexes")
        if actual_checks.get(table, {}) != PROVIDER_CHECK_SHAPES.get(table, {}):
            raise LedgerError(f"unexpected provider table shape: {table} checks")


async def acquire_named_lock(cursor, key: str) -> None:
    """Serializes every caller holding the same key, for the rest of the transaction.

    One statement, and it always WRITES, which is the point. A read that locks
    (``SELECT ... FOR UPDATE``) takes a gap lock when the row is missing, and two
    transactions holding compatible gap locks that then both insert into that gap
    deadlock. An upsert takes an exclusive row lock immediately instead, with no gap to
    share and no shared lock to upgrade.

    ``lock_key = lock_key`` is a deliberate no-op update: it makes the statement a write
    on an existing row without changing anything, which is what takes the lock.

    Keys come from a BOUNDED set, and rows here are never deleted. A per-day or per-bot
    key would add a permanent row for every day and every bot the server ever sees, which
    is a table that only grows. Deleting them instead is worse: removing a lock row while
    another transaction may still take that key reintroduces the missing-row race the
    helper exists to avoid. Bounded keys make the question moot.

    Nothing currently takes two named locks in one transaction. If something ever does,
    it needs a canonical acquisition order first, or two callers taking the same pair in
    opposite orders will deadlock.
    """

    await cursor.execute(
        "INSERT INTO playerbot_llm_lock (lock_key) VALUES (%s) ON DUPLICATE KEY UPDATE lock_key = lock_key",
        (key,),
    )


def utc_day(now: datetime) -> date:
    """The UTC calendar day a moment belongs to.

    UTC rather than local time, so the ceiling rolls over at one instant regardless of
    where the server is and regardless of daylight saving, which would otherwise give
    one day of the year 23 hours of budget and another 25.
    """

    if now.tzinfo is None:
        return now.replace(tzinfo=UTC).date()

    return now.astimezone(UTC).date()


async def ensure_schema(connection) -> None:
    # The guard runs FIRST, before any DDL. `CREATE TABLE` commits itself in MySQL,
    # so a guard placed after the loop cannot be undone by refusing afterwards: the
    # sidecar's five tables would already be sitting in a database its own message
    # says is not ready. Refusing to touch a database that has not been migrated
    # means not touching it.
    async with connection.cursor() as cursor:
        provider_tables = LEGACY_PROVIDER_TABLES + REQUIRED_MODULE_TABLES + SIDECAR_OWNED_TABLES
        await cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() "
            "AND table_name IN (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            provider_tables,
        )
        present = {row[0] for row in await cursor.fetchall()}

        budget_identifiers = LEGACY_BUDGET_IDENTIFIERS + NEUTRAL_BUDGET_IDENTIFIERS
        await cursor.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE constraint_schema = DATABASE() "
            "AND table_name IN ('playerbot_llm_daily_budget', 'playerbot_llm_budget_reservation') "
            "AND constraint_name IN (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            budget_identifiers,
        )
        present_identifiers = {row[0] for row in await cursor.fetchall()}
        await cursor.execute(
            "SELECT DISTINCT index_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'playerbot_llm_budget_reservation' "
            "AND index_name IN (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            budget_identifiers,
        )
        present_identifiers.update(row[0] for row in await cursor.fetchall())
    await connection.commit()

    legacy_present = present & set(LEGACY_PROVIDER_TABLES)
    neutral_present = present & (set(REQUIRED_MODULE_TABLES[:2]) | set(SIDECAR_OWNED_TABLES))
    fresh_neutral = neutral_present == set(REQUIRED_MODULE_TABLES[:2])
    complete_neutral = neutral_present == set(REQUIRED_MODULE_TABLES[:2]) | set(SIDECAR_OWNED_TABLES)
    missing = [name for name in REQUIRED_MODULE_TABLES if name not in present]

    if not legacy_present and not neutral_present and missing:
        raise LedgerError(
            "the mod-playerbots social schema is missing from this database "
            f"({', '.join(missing)}); apply data/sql/playerbots/updates before starting"
        )

    if legacy_present or not (fresh_neutral or complete_neutral):
        raise LedgerError(
            "legacy or mixed provider table layout detected; apply "
            "2026_08_07_00_playerbot_llm_tables.sql before starting"
        )

    missing_identifiers = set(NEUTRAL_BUDGET_IDENTIFIERS) - present_identifiers
    legacy_identifiers = set(LEGACY_BUDGET_IDENTIFIERS) & present_identifiers
    if missing_identifiers or legacy_identifiers:
        raise LedgerError(
            "legacy or incomplete provider budget identifiers detected; apply "
            "2026_08_07_00_playerbot_llm_tables.sql before starting"
        )

    if missing:
        # Refused rather than created. These tables belong to the mod-playerbots SQL
        # revisions, and a sidecar that creates its own version of them is how the two
        # definitions diverged in the first place. A missing table here means the
        # revisions have not been applied to this database, which is an operator
        # problem with an operator fix.
        raise LedgerError(
            "the mod-playerbots social schema is missing from this database "
            f"({', '.join(missing)}); apply data/sql/playerbots/updates before starting"
        )

    await _validate_provider_table_shapes(connection, neutral_present)

    async with connection.cursor() as cursor:
        # Every start after the first would otherwise print one "table already
        # exists" note per table. The driver surfaces those as Python warnings on
        # stderr, which buries a real message in seven fake ones and, for the CLI
        # commands, prints ahead of the JSON somebody is trying to read. Silenced at
        # the server for this connection only; genuine errors still raise.
        await cursor.execute("SET sql_notes = 0")
        try:
            for statement in SCHEMA_STATEMENTS:
                await cursor.execute(statement)
        finally:
            await cursor.execute("SET sql_notes = 1")

    await _validate_provider_table_shapes(
        connection,
        set(REQUIRED_MODULE_TABLES[:2]) | set(SIDECAR_OWNED_TABLES),
    )

    # The retired key shapes are deliberately NOT deleted here.
    #
    # An earlier version cleaned them up automatically, which is unsafe during a
    # restart: an old sidecar still running against this database is using
    # `budget_day:<date>` and `conversation:<bot_guid>` as its live locks, and
    # removing them mid-flight strips its mutual exclusion and reintroduces the
    # missing row race for it. A cosmetic tidy is not worth that.
    #
    # Leaving them is safe and still bounded: after the upgrade nothing creates
    # a key of either shape, so the residue is a fixed historical set rather
    # than a table that keeps growing. `retire_superseded_locks` below removes
    # it when an operator knows no old process remains.

    await connection.commit()


async def retire_superseded_locks(connection) -> int:
    """Removes lock rows from the retired per day and per bot key shapes.

    NOT called automatically, and that is the point. Deleting a lock row is only
    safe once nothing can still take that key, and during a rolling restart an older
    sidecar is still using these as its live locks. Run this from a maintenance step
    when no old process remains; skipping it costs a fixed residue of rows, not
    growth.

    Returns how many rows were removed.
    """

    try:
        await connection.begin()
        async with connection.cursor() as cursor:
            # No parameters, so the % is a literal rather than a placeholder.
            await cursor.execute("DELETE FROM playerbot_llm_lock WHERE lock_key LIKE 'budget_day:%'")
            removed = cursor.rowcount

            # A conversation key at or above the bucket bound cannot have come from
            # the current code. One BELOW it is indistinguishable from a live bucket
            # key and is left alone.
            await cursor.execute(
                "DELETE FROM playerbot_llm_lock WHERE lock_key LIKE 'conversation:%%' "
                "AND CAST(SUBSTRING(lock_key, 14) AS UNSIGNED) >= %s",
                (CONVERSATION_LOCK_BUCKETS,),
            )
            removed += cursor.rowcount
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise

    return removed
