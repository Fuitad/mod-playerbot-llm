"""Budget ledger tests against a real MySQL.

Marked ``mysql`` and skipped unless ``PLAYERBOT_LLM_TEST_MYSQL_DSN`` names a
disposable database. These cannot be mocked usefully: what they prove is that
``SELECT ... FOR UPDATE`` actually serializes two concurrent transactions, and a mock
that serializes them is a mock that assumes the answer.

``scripts/run_ledger_mysql_tests.sh`` starts a throwaway container, exports the DSN, and
removes it afterwards. It never touches a MySQL server that is already running.
"""

from __future__ import annotations

import asyncio
import json
import os
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiomysql
import pytest
from pymysql.constants import CLIENT
from pymysql.err import OperationalError

from playerbot_llm import app, budget, generation, ledger, protocol, provider, schema
from playerbot_llm import state as state_module
from playerbot_llm import store as store_module
from playerbot_llm.app import PlayerbotsDatabaseSettings
from playerbot_llm.budget import AdmissionDecision, RequestKind, RequestPriority
from playerbot_llm.providers import anthropic as anthropic_provider

pytestmark = [pytest.mark.mysql, pytest.mark.asyncio]

DSN = os.environ.get("PLAYERBOT_LLM_TEST_MYSQL_DSN")

if not DSN:  # pragma: no cover - the skip is the point
    pytest.skip("PLAYERBOT_LLM_TEST_MYSQL_DSN is not set", allow_module_level=True)


CEILING = budget.usd_to_nano("10.00")
QUARTER = Decimal("0.25")
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

# More distinct bots than there are lock buckets, so the bound is actually exercised.
CONVERSATION_LOCK_SAMPLE = 400

# Same fixture token the unit suite uses; the protocol only checks length and match.
TOKEN = "0123456789abcdef0123456789abcdef"

# The deployed schema, from the files that actually deploy it.
#
# The module migrations are the deployed schema authority. Reproducing their shape in a
# fixture would let the copies drift apart while this suite kept passing.
MODULES_ROOT = Path(__file__).resolve().parents[3]
SOCIAL_UPDATES = MODULES_ROOT / "mod-playerbots-social" / "data" / "sql" / "db_playerbot" / "updates"
LLM_UPDATES = MODULES_ROOT / "mod-playerbot-llm" / "data" / "sql" / "db_playerbot" / "updates"
SQL_REVISIONS = sorted(
    (*SOCIAL_UPDATES.glob("2026_08_*.sql"), *LLM_UPDATES.glob("2026_08_*.sql")),
    key=lambda revision: revision.name,
)

LEGACY_SOCIAL_REVISIONS = (
    SOCIAL_UPDATES / "2026_08_01_00_playerbot_social_schema.sql",
    LLM_UPDATES / "2026_08_04_00_playerbot_budget_lane_unspecified.sql",
)
LLM_TABLE_MIGRATION = LLM_UPDATES / "2026_08_07_00_playerbot_llm_tables.sql"
BOT_PURGE_MIGRATION = LLM_UPDATES / "2026_08_08_00_playerbot_llm_bot_purge.sql"
PROFILE_V3_MIGRATION = SOCIAL_UPDATES / "2026_08_09_00_playerbot_social_profile_v3.sql"
PROFILE_V3_PREREQUISITES = tuple(
    revision for revision in SQL_REVISIONS if revision.name < PROFILE_V3_MIGRATION.name
)
PROVIDER_TABLE_SUFFIXES = (
    "daily_budget",
    "budget_reservation",
    "lock",
    "profile",
    "conversation_turn",
    "career_decision",
    "ambient_attempt",
)
MALFORMED_PROVIDER_TABLE_MUTATIONS = (
    (
        "daily_budget_column",
        "ALTER TABLE playerbot_llm_daily_budget MODIFY reserved_usd DECIMAL(11, 6) NOT NULL DEFAULT 0",
    ),
    (
        "reservation_column",
        "ALTER TABLE playerbot_llm_budget_reservation MODIFY model VARCHAR(63) NOT NULL",
    ),
    (
        "lock_column",
        "ALTER TABLE playerbot_llm_lock MODIFY lock_key VARCHAR(63) NOT NULL",
    ),
    (
        "profile_column",
        "ALTER TABLE playerbot_llm_profile MODIFY bot_name VARCHAR(47) NOT NULL",
    ),
    (
        "conversation_column",
        "ALTER TABLE playerbot_llm_conversation_turn MODIFY content VARCHAR(255) NOT NULL",
    ),
    (
        "career_column",
        "ALTER TABLE playerbot_llm_career_decision MODIFY candidate_token VARCHAR(63) NOT NULL",
    ),
    (
        "ambient_column",
        "ALTER TABLE playerbot_llm_ambient_attempt MODIFY created_at DATETIME(1) NOT NULL",
    ),
    (
        "reservation_default",
        "ALTER TABLE playerbot_llm_budget_reservation "
        "MODIFY state ENUM('reserved', 'completed', 'released', 'expired') "
        "NOT NULL DEFAULT 'released'",
    ),
    (
        "reservation_index",
        "ALTER TABLE playerbot_llm_budget_reservation "
        "DROP INDEX ix_llm_reservation_day_state, "
        "ADD KEY ix_llm_reservation_day_state (state, budget_date)",
    ),
    (
        "daily_budget_constraint",
        "ALTER TABLE playerbot_llm_daily_budget "
        "DROP CHECK ck_llm_daily_budget_reserved, "
        "ADD CONSTRAINT ck_llm_daily_budget_reserved CHECK (reserved_usd >= -1)",
    ),
)

if not SQL_REVISIONS:  # pragma: no cover - a moved module directory, not a test outcome
    raise RuntimeError("no social schema revisions found; the module path moved")


def _settings() -> PlayerbotsDatabaseSettings:
    # The module-level skip above already guarantees this; restating it keeps the type
    # checker honest without a cast that would also hide a real None.
    assert DSN is not None
    return PlayerbotsDatabaseSettings.parse_info(DSN)


async def _connect() -> aiomysql.Connection:
    # Spelled out rather than splatted from a dict: a **kwargs splat makes every argument
    # an untyped object, so a misspelled or wrongly typed connection setting reaches the
    # driver instead of the type checker.
    settings = _settings()
    return await aiomysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=settings.database,
        autocommit=False,
    )


def _revision_statements(revision: Path) -> tuple[str, ...]:
    delimiter = ";"
    current: list[str] = []
    statements: list[str] = []

    for line in revision.read_text().splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            if any(part.strip() for part in current):
                raise ValueError(f"{revision.name} changes delimiter inside a statement")
            delimiter = stripped.split(maxsplit=1)[1]
            continue

        current.append(line)
        if not line.rstrip().endswith(delimiter):
            continue

        statement = "\n".join(current)
        statement = statement[: statement.rfind(delimiter)].strip()
        if statement:
            statements.append(statement)
        current.clear()

    if any(part.strip() for part in current):
        raise ValueError(f"{revision.name} ends with an unterminated statement")

    return tuple(statements)


def _settings_for(database: str) -> PlayerbotsDatabaseSettings:
    settings = _settings()
    return PlayerbotsDatabaseSettings(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        database=database,
    )


async def _apply_revisions(database: str, revisions: tuple[Path, ...]) -> None:
    settings = _settings()
    connection = await aiomysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=database,
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )
    try:
        for revision in revisions:
            async with connection.cursor() as cursor:
                for statement in _revision_statements(revision):
                    await cursor.execute(statement)
    finally:
        connection.close()


async def _execute_statements(database: str, statements: tuple[str, ...]) -> None:
    settings = _settings()
    connection = await aiomysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=database,
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            for statement in statements:
                await cursor.execute(statement)
    finally:
        connection.close()


async def _create_legacy_sidecar_schema(database: str) -> None:
    statements = tuple(
        statement.replace("playerbot_llm_", "playerbot_claude_") for statement in schema.SCHEMA_STATEMENTS
    )
    await _execute_statements(database, statements)


async def _provider_table_snapshot(database: str) -> dict[str, int]:
    settings = _settings()
    connection = await aiomysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=database,
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() "
                "AND (table_name LIKE 'playerbot_llm_%' OR table_name LIKE 'playerbot_claude_%') "
                "AND table_name <> 'playerbot_llm_bot_purge' "
                "ORDER BY table_name"
            )
            names = [row[0] for row in await cursor.fetchall()]
            snapshot: dict[str, int] = {}
            for name in names:
                assert name in {
                    f"playerbot_{prefix}_{suffix}"
                    for prefix in ("claude", "llm")
                    for suffix in PROVIDER_TABLE_SUFFIXES
                }
                await cursor.execute(f"SELECT COUNT(1) FROM `{name}`")  # noqa: S608
                row = await cursor.fetchone()
                assert row is not None
                snapshot[name] = row[0]
            return snapshot
    finally:
        connection.close()


@asynccontextmanager
async def _isolated_database(suffix: str) -> AsyncIterator[str]:
    settings = _settings()
    database = f"{settings.database}_{suffix}"
    admin = await _connect()
    try:
        async with admin.cursor() as cursor:
            await cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            await cursor.execute(f"CREATE DATABASE `{database}`")
        await admin.commit()
    finally:
        admin.close()

    try:
        yield database
    finally:
        admin = await _connect()
        try:
            async with admin.cursor() as cursor:
                await cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            await admin.commit()
        finally:
            admin.close()


async def _apply_deployed_schema() -> None:
    """Runs the module's SQL revisions, in order, against the test database.

    Statements are split only at the active MySQL client delimiter. This preserves quoted
    DDL used by ``PREPARE`` and lets procedure migrations use ``DELIMITER`` directives,
    which the server protocol itself does not understand. Every revision is idempotent,
    so running them against a database that already has them is the no-op they are written
    to be.
    """

    settings = _settings()
    connection = await aiomysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=settings.database,
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT COUNT(1) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() "
                "AND table_name IN "
                "('playerbot_llm_daily_budget', 'playerbot_llm_budget_reservation', "
                "'playerbot_llm_bot_purge')"
            )
            required_tables = (await cursor.fetchone())[0]

        # The AzerothCore updater records completed revisions and never replays an old
        # CREATE TABLE after a later revision renamed that table. This fixture used to
        # replay every historical file before every test, which would recreate the two
        # retired names beside the migrated tables. Apply the revision chain once, just
        # as the real updater does.
        if required_tables == 3:
            return

        for revision in SQL_REVISIONS:
            async with connection.cursor() as cursor:
                # Otherwise every CREATE TABLE IF NOT EXISTS past the first run emits a
                # note the driver raises as a Python warning, one per table per test.
                await cursor.execute("SET sql_notes = 0")
                try:
                    for statement in _revision_statements(revision):
                        await cursor.execute(statement)
                finally:
                    await cursor.execute("SET sql_notes = 1")
    finally:
        connection.close()


@pytest.fixture
async def clean_ledger():
    await _apply_deployed_schema()
    book = ledger.BudgetLedger(CEILING, QUARTER)
    connection = await _connect()
    try:
        await schema.ensure_schema(connection)
        async with connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_llm_budget_reservation")
            await cursor.execute("DELETE FROM playerbot_llm_daily_budget")
            # The breaker lives on the social runtime control row now, so a test that
            # trips it would otherwise deny every later test in the session.
            await cursor.execute("DELETE FROM playerbot_social_runtime_control")
        await connection.commit()
        yield book, connection
    finally:
        connection.close()


async def test_starting_against_a_database_without_the_module_schema_is_refused() -> None:
    """The sidecar no longer creates the budget tables, so it has to say when they are absent.

    It used to create its own version of them, which is how two incompatible definitions
    came to exist: both used CREATE TABLE IF NOT EXISTS, so whichever ran first won and
    the other silently did nothing. Refusing to start is the honest replacement. Creating
    them here again would put the divergence straight back.

    The second half of this test is the one that bites. `CREATE TABLE` commits itself in
    MySQL, so a guard placed after the DDL loop cannot be undone by raising afterwards:
    the sidecar's own five tables would already be sitting in a database its own error
    message calls unready. Asserting on the module table alone would not have caught that,
    because the sidecar never creates that one either way. The whole sidecar set has to be
    absent for the refusal to mean what it says.
    """

    settings = _settings()
    bare = f"{settings.database}_bare"

    async def _drop_and_create() -> None:
        # DROP first, not just CREATE IF NOT EXISTS. A previous run that failed its
        # assertions leaves this database behind with whatever it created, and reusing
        # it would make the next run assert against the last run's residue. Recreating
        # from nothing is what makes "no table exists" mean this run created none.
        admin = await _connect()
        try:
            async with admin.cursor() as cursor:
                await cursor.execute(f"DROP DATABASE IF EXISTS `{bare}`")
                await cursor.execute(f"CREATE DATABASE `{bare}`")
            await admin.commit()
        finally:
            admin.close()

    async def _drop() -> None:
        admin = await _connect()
        try:
            async with admin.cursor() as cursor:
                await cursor.execute(f"DROP DATABASE IF EXISTS `{bare}`")
            await admin.commit()
        finally:
            admin.close()

    await _drop_and_create()
    try:
        connection = await aiomysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            db=bare,
            autocommit=False,
        )
        try:
            with pytest.raises(schema.LedgerError, match="social schema is missing"):
                await schema.ensure_schema(connection)

            # Nothing at all was created: not the module's tables, and not the sidecar's.
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
                )
                created = {row[0] for row in await cursor.fetchall()}
            await connection.commit()
            assert created == set(), f"a refused start left tables behind: {sorted(created)}"

            # Spelled out as well as compared, so a future table added to either list is
            # covered by name rather than by the emptiness of a set nobody re-reads.
            assert not created & set(schema.SIDECAR_OWNED_TABLES)
            assert not created & set(schema.REQUIRED_MODULE_TABLES)
        finally:
            connection.close()
    finally:
        # In a finally, so a failing assertion cannot leave this database for the next
        # run to inherit. That is exactly how the first version of this test passed
        # against a defect it had already been shown to catch.
        await _drop()


async def test_fresh_migration_runs_before_sidecar_schema_initialization() -> None:
    async with _isolated_database("fresh_llm_migration") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _apply_revisions(database, (LLM_TABLE_MIGRATION, BOT_PURGE_MIGRATION))

        settings = _settings()
        connection = await aiomysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            db=database,
            autocommit=False,
        )
        try:
            await schema.ensure_schema(connection)
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() "
                    "AND (table_name LIKE 'playerbot_llm_%' OR table_name LIKE 'playerbot_claude_%')"
                )
                tables = {row[0] for row in await cursor.fetchall()}
            await connection.commit()
        finally:
            connection.close()

        assert tables == (
            set(schema.REQUIRED_MODULE_TABLES) - {"playerbot_social_runtime_control"}
            | set(schema.SIDECAR_OWNED_TABLES)
        )


async def test_profile_v3_migration_preserves_only_coherent_v2_traits() -> None:
    async with _isolated_database("profile_v3_migration") as database:
        await _apply_revisions(database, PROFILE_V3_PREREQUISITES)
        await _execute_statements(
            database,
            (
                "INSERT INTO playerbot_social_actor "
                "(id, public_id, character_guid, display_name, actor_kind, last_seen_at) VALUES "
                "(1, 'act_00000000000000000000000000000001', 41, 'Coherent', 'bot', "
                "'2026-08-09 12:00:00'), "
                "(2, 'act_00000000000000000000000000000002', 42, 'Mixed', 'bot', "
                "'2026-08-09 12:00:00'), "
                "(3, 'act_00000000000000000000000000000003', 43, 'Future', 'bot', "
                "'2026-08-09 12:00:00')",
                "INSERT INTO playerbot_social_profile "
                "(bot_actor_id, schema_version, traits_version, social_traits, biography_state, "
                "biography_request_token, biography_attempted_at, biography, biography_generated_at) VALUES "
                "(1, 2, 2, JSON_OBJECT('warmth', 71, 'interests', JSON_ARRAY('mining')), "
                "'ready', 99, '2026-08-09 12:00:00', JSON_OBJECT('version', 2), '2026-08-09 12:01:00'), "
                "(2, 2, 3, JSON_OBJECT('warmth', 52), 'ready', 98, '2026-08-09 12:00:00', "
                "JSON_OBJECT('version', 2), '2026-08-09 12:01:00'), "
                "(3, 4, 4, JSON_OBJECT('warmth', 83), 'ready', 97, '2026-08-09 12:00:00', "
                "JSON_OBJECT('version', 4), '2026-08-09 12:01:00')",
            ),
        )

        await _apply_revisions(database, (PROFILE_V3_MIGRATION, PROFILE_V3_MIGRATION))

        settings = _settings()
        connection = await aiomysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            db=database,
            autocommit=True,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT bot_actor_id, schema_version, traits_version, "
                    "JSON_UNQUOTE(JSON_EXTRACT(social_traits, '$.warmth')), biography_state, "
                    "biography_request_token, biography_attempted_at, biography, biography_generated_at "
                    "FROM playerbot_social_profile ORDER BY bot_actor_id"
                )
                rows = await cursor.fetchall()
        finally:
            connection.close()

        assert rows[0] == (1, 3, 3, "71", "absent", 0, None, None, None)
        assert rows[1][0:7] == (2, 2, 3, "52", "ready", 98, datetime(2026, 8, 9, 12, 0))
        assert rows[1][7] == '{"version": 2}'
        assert rows[1][8] == datetime(2026, 8, 9, 12, 1)
        assert rows[2][0:7] == (3, 4, 4, "83", "ready", 97, datetime(2026, 8, 9, 12, 0))
        assert rows[2][7] == '{"version": 4}'
        assert rows[2][8] == datetime(2026, 8, 9, 12, 1)


async def test_full_legacy_migration_preserves_every_provider_table_and_row() -> None:
    async with _isolated_database("full_llm_migration") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _create_legacy_sidecar_schema(database)
        await _execute_statements(
            database,
            (
                "INSERT INTO playerbot_claude_daily_budget "
                "(budget_date, reserved_usd, spent_usd) VALUES ('2026-08-07', 1.25, 2.50)",
                "INSERT INTO playerbot_claude_budget_reservation "
                "(public_id, budget_date, request_kind, priority_lane, model, max_cost_usd, "
                "actual_cost_usd, state, expires_at, settled_at) VALUES "
                "('req_0123456789abcdef0123456789abcdef', '2026-08-07', 'chat_response', "
                "'unspecified', 'fixture-model', 1.25, 0.50, 'completed', "
                "'2026-08-07 12:05:00', '2026-08-07 12:01:00')",
                "INSERT INTO playerbot_claude_lock (lock_key) VALUES ('fixture-lock')",
                "INSERT INTO playerbot_claude_profile "
                "(bot_guid, profile_version, crafting_affinity, gathering_affinity, "
                "exploration_affinity, sociability, voice, bot_name, updated_at) "
                "VALUES (42, 3, 1, 2, 3, 4, 'warm', 'Mira', '2026-08-07 12:00:00')",
                "INSERT INTO playerbot_claude_conversation_turn "
                "(bot_guid, role, content, created_at) "
                "VALUES (42, 'assistant', 'A preserved turn.', '2026-08-07 12:00:00')",
                "INSERT INTO playerbot_claude_career_decision "
                "(bot_guid, career_version, candidate_token, spending_style, updated_at) "
                "VALUES (42, 2, 'blacksmithing', 'careful', '2026-08-07 12:00:00')",
                "INSERT INTO playerbot_claude_ambient_attempt (created_at) VALUES ('2026-08-07 12:00:00')",
            ),
        )

        assert set((await _provider_table_snapshot(database)).values()) == {1}

        await _apply_revisions(database, (LLM_TABLE_MIGRATION,))
        migrated = await _provider_table_snapshot(database)
        assert migrated == {f"playerbot_llm_{suffix}": 1 for suffix in PROVIDER_TABLE_SUFFIXES}

        # A second updater pass is an exact no-op for names and rows.
        await _apply_revisions(database, (LLM_TABLE_MIGRATION,))
        assert await _provider_table_snapshot(database) == migrated


@pytest.mark.parametrize("suffix", PROVIDER_TABLE_SUFFIXES)
async def test_each_old_and_new_table_collision_is_refused_before_ddl(suffix: str) -> None:
    async with _isolated_database(f"collision_{suffix}") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _create_legacy_sidecar_schema(database)
        await _execute_statements(
            database,
            (f"CREATE TABLE `playerbot_llm_{suffix}` LIKE `playerbot_claude_{suffix}`",),
        )
        before = await _provider_table_snapshot(database)

        with pytest.raises(OperationalError, match="mixed_or_colliding_schema"):
            await _apply_revisions(database, (LLM_TABLE_MIGRATION,))

        assert await _provider_table_snapshot(database) == before


@pytest.mark.parametrize(
    "mutation",
    (
        "RENAME TABLE `playerbot_claude_lock` TO `playerbot_llm_lock`",
        "DROP TABLE `playerbot_claude_lock`",
    ),
)
async def test_unrecognized_partial_table_layout_is_refused_before_ddl(mutation: str) -> None:
    async with _isolated_database("partial_layout") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _create_legacy_sidecar_schema(database)
        await _execute_statements(database, (mutation,))
        before = await _provider_table_snapshot(database)

        with pytest.raises(OperationalError, match="mixed_or_colliding_schema"):
            await _apply_revisions(database, (LLM_TABLE_MIGRATION,))

        assert await _provider_table_snapshot(database) == before


async def test_sidecar_refuses_a_mixed_schema_before_creating_neutral_tables() -> None:
    async with _isolated_database("mixed_sidecar_start") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _apply_revisions(database, (LLM_TABLE_MIGRATION, BOT_PURGE_MIGRATION))
        legacy_lock_statement = schema.SCHEMA_STATEMENTS[0].replace("playerbot_llm_", "playerbot_claude_")
        await _execute_statements(database, (legacy_lock_statement,))
        before = await _provider_table_snapshot(database)

        settings = _settings()
        connection = await aiomysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            db=database,
            autocommit=False,
        )
        try:
            with pytest.raises(schema.LedgerError, match="legacy or mixed provider table layout"):
                await schema.ensure_schema(connection)
        finally:
            connection.close()

        assert await _provider_table_snapshot(database) == before


@pytest.mark.parametrize(("case", "mutation"), MALFORMED_PROVIDER_TABLE_MUTATIONS)
async def test_migration_refuses_malformed_legacy_table_shapes_before_ddl(case: str, mutation: str) -> None:
    async with _isolated_database(f"badmig_{case}") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _create_legacy_sidecar_schema(database)
        legacy_mutation = mutation.replace("playerbot_llm_", "playerbot_claude_").replace("_llm_", "_claude_")
        await _execute_statements(database, (legacy_mutation,))
        before = await _provider_table_snapshot(database)

        with pytest.raises(OperationalError, match="unexpected_table_shape"):
            await _apply_revisions(database, (LLM_TABLE_MIGRATION,))

        assert await _provider_table_snapshot(database) == before


@pytest.mark.parametrize(
    ("case", "mutation"),
    MALFORMED_PROVIDER_TABLE_MUTATIONS,
)
async def test_sidecar_refuses_malformed_neutral_table_shapes(case: str, mutation: str) -> None:
    async with _isolated_database(f"malformed_{case}") as database:
        await _apply_revisions(database, LEGACY_SOCIAL_REVISIONS)
        await _apply_revisions(database, (LLM_TABLE_MIGRATION, BOT_PURGE_MIGRATION))

        settings = _settings()
        connection = await aiomysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            db=database,
            autocommit=False,
        )
        try:
            await schema.ensure_schema(connection)
            await _execute_statements(database, (mutation,))

            with pytest.raises(schema.LedgerError, match="unexpected provider table shape"):
                await schema.ensure_schema(connection)
        finally:
            connection.close()


async def test_purge_pending_bot_data_deletes_only_queued_bot_rows_and_acknowledges() -> None:
    async with _isolated_database("bot_purge_scope") as database:
        await _apply_revisions(database, tuple(SQL_REVISIONS))
        state, pool = await state_module.open_state(
            _settings_for(database), ceiling_nano=CEILING, reserve_ratio=QUARTER
        )
        try:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    for bot_guid, bot_name in ((42, "Target"), (73, "Survivor")):
                        await cursor.execute(
                            "INSERT INTO playerbot_llm_profile "
                            "(bot_guid, profile_version, crafting_affinity, gathering_affinity, "
                            "exploration_affinity, sociability, voice, bot_name, updated_at) "
                            "VALUES (%s, 3, 1, 2, 3, 4, 'warm', %s, %s)",
                            (bot_guid, bot_name, NOW),
                        )
                        await cursor.execute(
                            "INSERT INTO playerbot_llm_conversation_turn "
                            "(bot_guid, role, content, created_at) VALUES (%s, 'assistant', %s, %s)",
                            (bot_guid, f"turn for {bot_name}", NOW),
                        )
                        await cursor.execute(
                            "INSERT INTO playerbot_llm_career_decision "
                            "(bot_guid, career_version, candidate_token, spending_style, updated_at) "
                            "VALUES (%s, 2, 'blacksmithing', 'careful', %s)",
                            (bot_guid, NOW),
                        )

                    await cursor.execute("INSERT INTO playerbot_llm_lock (lock_key) VALUES ('fixture-lock')")
                    await cursor.execute(
                        "INSERT INTO playerbot_llm_ambient_attempt (created_at) VALUES (%s)", (NOW,)
                    )
                    await cursor.execute("INSERT INTO playerbot_llm_bot_purge (bot_guid) VALUES (%s)", (42,))
                await connection.commit()

            assert await state.purge_pending_bot_data() == 1
            assert await state.purge_pending_bot_data() == 0

            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    remaining: dict[str, list[int]] = {}
                    for table in (
                        "playerbot_llm_profile",
                        "playerbot_llm_conversation_turn",
                        "playerbot_llm_career_decision",
                    ):
                        await cursor.execute(f"SELECT bot_guid FROM `{table}` ORDER BY bot_guid")  # noqa: S608
                        remaining[table] = [int(row[0]) for row in await cursor.fetchall()]

                    await cursor.execute(
                        "SELECT COUNT(*), COUNT(acknowledged_at) FROM playerbot_llm_bot_purge"
                    )
                    purge_count, acknowledged_count = (int(value) for value in await cursor.fetchone())
                    await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_lock")
                    lock_count = int((await cursor.fetchone())[0])
                    await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_ambient_attempt")
                    ambient_count = int((await cursor.fetchone())[0])

            assert remaining == {
                "playerbot_llm_profile": [73],
                "playerbot_llm_conversation_turn": [73],
                "playerbot_llm_career_decision": [73],
            }
            assert purge_count == 1
            assert acknowledged_count == 1
            assert lock_count >= 1
            assert ambient_count == 1
        finally:
            await state_module.close_pool(pool)


async def test_purge_pending_bot_data_rolls_back_and_retains_the_intent() -> None:
    async with _isolated_database("bot_purge_rollback") as database:
        await _apply_revisions(database, tuple(SQL_REVISIONS))
        state, pool = await state_module.open_state(
            _settings_for(database), ceiling_nano=CEILING, reserve_ratio=QUARTER
        )
        try:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO playerbot_llm_profile "
                        "(bot_guid, profile_version, crafting_affinity, gathering_affinity, "
                        "exploration_affinity, sociability, voice, bot_name, updated_at) "
                        "VALUES (42, 3, 1, 2, 3, 4, 'warm', 'Target', %s)",
                        (NOW,),
                    )
                    await cursor.execute(
                        "INSERT INTO playerbot_llm_conversation_turn "
                        "(bot_guid, role, content, created_at) "
                        "VALUES (42, 'assistant', 'must survive rollback', %s)",
                        (NOW,),
                    )
                    await cursor.execute(
                        "INSERT INTO playerbot_llm_career_decision "
                        "(bot_guid, career_version, candidate_token, spending_style, updated_at) "
                        "VALUES (42, 2, 'blacksmithing', 'careful', %s)",
                        (NOW,),
                    )
                    await cursor.execute("INSERT INTO playerbot_llm_bot_purge (bot_guid) VALUES (42)")
                    await cursor.execute(
                        "CREATE TRIGGER refuse_career_purge BEFORE DELETE ON playerbot_llm_career_decision "
                        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'forced purge failure'"
                    )
                await connection.commit()

            with pytest.raises(OperationalError, match="forced purge failure"):
                await state.purge_pending_bot_data()

            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    counts: dict[str, int] = {}
                    for table in (
                        "playerbot_llm_profile",
                        "playerbot_llm_conversation_turn",
                        "playerbot_llm_career_decision",
                        "playerbot_llm_bot_purge",
                    ):
                        await cursor.execute(f"SELECT COUNT(*) FROM `{table}`")  # noqa: S608
                        counts[table] = int((await cursor.fetchone())[0])
                    await cursor.execute(
                        "SELECT acknowledged_at IS NULL FROM playerbot_llm_bot_purge WHERE bot_guid = 42"
                    )
                    intent_is_pending = bool((await cursor.fetchone())[0])

            assert set(counts.values()) == {1}
            assert intent_is_pending
        finally:
            await state_module.close_pool(pool)


async def test_admission_and_settlement_write_the_deployed_columns(clean_ledger) -> None:
    """Definition of Done 7: the ledger works against the deployed schema, not its own.

    Every column asserted here is read back by the name the mod-playerbots revision gives
    it. The sidecar used to carry a second, incompatible definition of these two tables in
    its own DDL, with different column names, a different money type, and a circuit
    breaker on the daily row. This is the test that says which one is real.
    """

    book, connection = clean_ledger

    decision, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None
    # The opaque identity contract: a kind prefix and 32 lowercase hex.
    assert reservation.public_id.startswith("req_")
    assert len(reservation.public_id) == 36

    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT public_id, budget_date, request_kind, priority_lane, model, max_cost_usd, "
            "actual_cost_usd, state, expires_at FROM playerbot_llm_budget_reservation "
            "WHERE id = %s",
            (reservation.reservation_id,),
        )
        row = await cursor.fetchone()
        await cursor.execute(
            "SELECT reserved_usd, spent_usd FROM playerbot_llm_daily_budget WHERE budget_date = %s",
            (NOW.date(),),
        )
        day = await cursor.fetchone()

    assert row is not None
    assert row[0] == reservation.public_id
    assert row[1] == NOW.date()
    assert row[2] == "chat_response"
    # Not a guess at the lane. The sidecar is never told it, and says so.
    assert row[3] == "unspecified"
    assert row[4] == anthropic_provider.MODEL_ID
    assert row[5] == Decimal("1.000000")
    assert row[6] is None
    assert row[7] == "reserved"
    assert row[8] == NOW.replace(tzinfo=None) + ledger.RESERVATION_EXPIRY

    # The daily row carries the live reservation, and nothing is spent until it settles.
    assert day is not None
    assert day[0] == Decimal("1.000000")
    assert day[1] == Decimal("0.000000")

    settled = await book.settle(
        connection,
        reservation=reservation,
        actual_cost_nano=budget.usd_to_nano("0.40"),
        now=NOW,
    )
    assert settled == ledger.SettlementReceipt(
        completed=True,
        stored_cost_nano=budget.usd_to_nano("0.40"),
        breach=False,
        saturated=False,
    )

    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT state, actual_cost_usd, settled_at FROM playerbot_llm_budget_reservation WHERE id = %s",
            (reservation.reservation_id,),
        )
        after = await cursor.fetchone()
        await cursor.execute(
            "SELECT reserved_usd, spent_usd FROM playerbot_llm_daily_budget WHERE budget_date = %s",
            (NOW.date(),),
        )
        day_after = await cursor.fetchone()

    assert after is not None
    # 'completed', which is the deployed vocabulary. The sidecar's own DDL said 'settled'.
    assert after[0] == "completed"
    assert after[1] == Decimal("0.400000")
    assert after[2] == NOW.replace(tzinfo=None)

    # The reservation is no longer outstanding and the real cost is what was charged, so
    # the unused 0.60 went back to the day rather than staying committed until midnight.
    assert day_after is not None
    assert day_after[0] == Decimal("0.000000")
    assert day_after[1] == Decimal("0.400000")


@pytest.mark.parametrize(
    ("actual_cost_nano", "expected_receipt"),
    (
        (0, ledger.SettlementReceipt(True, 0, False, False)),
        (1, ledger.SettlementReceipt(True, budget.STORAGE_SCALE_NANO, False, False)),
        (
            budget.STORAGE_SCALE_NANO,
            ledger.SettlementReceipt(True, budget.STORAGE_SCALE_NANO, False, False),
        ),
        (-1, ledger.SettlementReceipt(True, 0, True, False)),
        (
            budget.MAX_STORABLE_NANO + 1,
            ledger.SettlementReceipt(True, budget.MAX_STORABLE_NANO, True, False),
        ),
    ),
)
async def test_settlement_receipt_reports_exact_stored_boundaries(
    clean_ledger, actual_cost_nano: int, expected_receipt: ledger.SettlementReceipt
) -> None:
    book, connection = clean_ledger
    _, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert reservation is not None

    assert (
        await book.settle(
            connection,
            reservation=reservation,
            actual_cost_nano=actual_cost_nano,
            now=NOW,
        )
        == expected_receipt
    )


async def test_a_reservation_is_recorded_and_counted(clean_ledger) -> None:
    book, connection = clean_ledger

    decision, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None

    state = await book.snapshot(connection, now=NOW)
    assert state.outstanding_nano == budget.usd_to_nano("1.00")
    assert state.settled_nano == 0


async def test_two_concurrent_reservations_cannot_jointly_exceed_the_ceiling(clean_ledger) -> None:
    """Definition of Done 1, against a real lock.

    Both transactions are opened before either commits, so this genuinely exercises
    SELECT ... FOR UPDATE rather than a sequence that happens to be ordered. A mock
    that serializes them would be assuming the thing under test.
    """
    book, first_connection = clean_ledger
    second_connection = await _connect()

    try:
        six = budget.usd_to_nano("6.00")

        # A barrier, because asyncio.gather alone does not guarantee contention: the
        # first coroutine can finish and commit before the second begins, which would
        # make this pass while proving only that two sequential reservations behave.
        #
        # Both connections take a real write lock on the SAME day row here, before either
        # calls reserve, so the second reserve genuinely has to wait on the first.
        barrier = asyncio.Barrier(2)

        # The day row is created first, outside the barrier, so what the two coroutines
        # actually contend on is the row lock rather than the insert.
        await first_connection.begin()
        async with first_connection.cursor() as cursor:
            await cursor.execute(
                "INSERT IGNORE INTO playerbot_llm_daily_budget (budget_date) VALUES (%s)",
                (schema.utc_day(NOW),),
            )
        await first_connection.commit()

        async def reserve(connection):
            await barrier.wait()
            return await book.reserve(
                connection,
                request_kind=RequestKind.CHAT_RESPONSE,
                model=anthropic_provider.MODEL_ID,
                max_cost_nano=six,
                priority=RequestPriority.IMMEDIATE_HUMAN,
                now=NOW,
            )

        first, second = await asyncio.gather(reserve(first_connection), reserve(second_connection))
        decisions = {first[0], second[0]}

        # Six plus six is twelve and the ceiling is ten, so exactly one may win.
        assert decisions == {AdmissionDecision.ADMITTED, AdmissionDecision.DENIED_CEILING}

        state = await book.snapshot(first_connection, now=NOW)
        assert state.outstanding_nano == six
    finally:
        second_connection.close()


async def test_background_work_is_denied_at_the_reserve_while_a_human_proceeds(clean_ledger) -> None:
    """Definition of Done 2."""
    book, connection = clean_ledger

    await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("7.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    one = budget.usd_to_nano("1.00")
    background, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.BACKGROUND,
        now=NOW,
    )
    human, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert background is AdmissionDecision.DENIED_RESERVE
    assert human is AdmissionDecision.ADMITTED


async def test_every_retry_gets_its_own_reservation_and_cost(clean_ledger) -> None:
    """Definition of Done 3. Two reservations for one logical request are two rows.

    The unique key is the minted public_id, so nothing about the caller has to be unique
    for this to hold, which is the point: the worldserver's request ids are not.
    """
    book, connection = clean_ledger
    one = budget.usd_to_nano("1.00")

    first_decision, first = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    second_decision, second = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert first_decision is AdmissionDecision.ADMITTED
    assert second_decision is AdmissionDecision.ADMITTED
    assert first.reservation_id != second.reservation_id

    await book.settle(connection, reservation=first, actual_cost_nano=1000, now=NOW)
    await book.settle(connection, reservation=second, actual_cost_nano=2000, now=NOW)

    state = await book.snapshot(connection, now=NOW)
    assert state.settled_nano == 3000
    assert state.outstanding_nano == 0


async def test_a_crash_leaves_a_reservation_that_expiry_reclaims_without_double_charging(
    clean_ledger,
) -> None:
    """Definition of Done 4.

    The reservation is never settled, as if the sidecar died mid-request. A later
    transaction reclaims it, and the completion that eventually arrives is refused
    rather than charged against a reservation that no longer exists.
    """
    book, connection = clean_ledger
    one = budget.usd_to_nano("1.00")

    _, stranded = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    later = NOW + ledger.RESERVATION_EXPIRY + timedelta(seconds=1)

    # A later request reclaims it: the money is back in the day's budget.
    decision, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("10.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=later,
    )
    assert decision is AdmissionDecision.ADMITTED

    # And the late completion for the reclaimed reservation is refused, not charged.
    assert await book.settle(
        connection, reservation=stranded, actual_cost_nano=one, now=later
    ) == ledger.SettlementReceipt(False, None, False, False)

    state = await book.snapshot(connection, now=later)
    assert state.settled_nano == 0


async def test_an_impossible_reported_cost_opens_the_circuit_and_stops_admission(clean_ledger) -> None:
    """Definition of Done 7.

    The reported figure is stored truthfully rather than clamped to the maximum, because
    clamping would make the ledger agree with a bound that was actually broken.
    """
    book, connection = clean_ledger
    one = budget.usd_to_nano("1.00")

    _, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    impossible = one * 5
    assert await book.settle(
        connection, reservation=reservation, actual_cost_nano=impossible, now=NOW
    ) == ledger.SettlementReceipt(True, impossible, True, False)

    state = await book.snapshot(connection, now=NOW)
    assert state.circuit_open is True
    assert state.settled_nano == impossible  # truthful, not clamped

    for priority in RequestPriority:
        decision, _ = await book.reserve(
            connection,
            request_kind=RequestKind.CHAT_RESPONSE,
            model=anthropic_provider.MODEL_ID,
            max_cost_nano=1,
            priority=priority,
            now=NOW,
        )
        assert decision is AdmissionDecision.DENIED_CIRCUIT_OPEN


async def test_a_released_reservation_returns_its_money(clean_ledger) -> None:
    book, connection = clean_ledger
    nine = budget.usd_to_nano("9.00")

    _, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=nine,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    # Nothing else fits while it is outstanding.
    blocked, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=nine,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert blocked is AdmissionDecision.DENIED_CEILING

    assert await book.release(connection, reservation=reservation) is True
    # Releasing twice is not an error and does not credit twice.
    assert await book.release(connection, reservation=reservation) is False

    freed, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=nine,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert freed is AdmissionDecision.ADMITTED


async def test_the_ceiling_rolls_over_at_utc_midnight(clean_ledger) -> None:
    book, connection = clean_ledger
    ten = budget.usd_to_nano("10.00")

    _, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=ten,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    await book.settle(connection, reservation=reservation, actual_cost_nano=ten, now=NOW)

    exhausted, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=1,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert exhausted is AdmissionDecision.DENIED_CEILING

    # A new UTC day is a new row and a fresh ceiling.
    tomorrow = NOW + timedelta(days=1)
    fresh, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=ten,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=tomorrow,
    )
    assert fresh is AdmissionDecision.ADMITTED


# The non-budget durable state ------------------------------------------------------


@pytest.fixture
async def store(clean_ledger):
    _, connection = clean_ledger
    # Written out rather than looped over an interpolated name. A table name cannot be a
    # bound parameter, so a loop here means building SQL from a string, and a test that
    # does that teaches the pattern even when its own inputs are literals.
    async with connection.cursor() as cursor:
        await cursor.execute("DELETE FROM playerbot_llm_profile")
        await cursor.execute("DELETE FROM playerbot_llm_conversation_turn")
        await cursor.execute("DELETE FROM playerbot_llm_career_decision")
        await cursor.execute("DELETE FROM playerbot_llm_ambient_attempt")
    await connection.commit()
    return store_module.SidecarStore(), connection


async def test_a_profile_round_trips_and_the_latest_write_wins(store) -> None:
    book, connection = store

    assert await book.get_profile(connection, bot_guid=7) is None

    await book.record_profile(
        connection,
        bot_guid=7,
        profile_version=2,
        crafting_affinity=10,
        gathering_affinity=20,
        exploration_affinity=30,
        sociability=40,
        voice="wry",
        bot_name="Grimbold",
        now=NOW,
    )
    await book.record_profile(
        connection,
        bot_guid=7,
        profile_version=2,
        crafting_affinity=11,
        gathering_affinity=20,
        exploration_affinity=30,
        sociability=40,
        voice="earnest",
        bot_name="Grimbold",
        now=NOW,
    )

    profile = await book.get_profile(connection, bot_guid=7)
    assert profile["crafting_affinity"] == 11
    assert profile["voice"] == "earnest"


async def test_conversation_memory_is_trimmed_on_disk_not_merely_on_read(store) -> None:
    """An unbounded history is an unbounded prompt, which is an unbounded cost."""
    book, connection = store

    for index in range(store_module.CONVERSATION_TURN_LIMIT + 5):
        await book.append_turn(connection, bot_guid=7, role="user", content=f"turn {index}", now=NOW)

    turns = await book.recent_turns(connection, bot_guid=7)
    assert len(turns) == store_module.CONVERSATION_TURN_LIMIT
    # The OLDEST are the ones dropped, and order is preserved.
    assert turns[0][1] == "turn 5"
    assert turns[-1][1] == f"turn {store_module.CONVERSATION_TURN_LIMIT + 4}"

    # And the trim really happened in the table, not just in what the query returned.
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_conversation_turn WHERE bot_guid = 7")
        row = await cursor.fetchone()
    assert int(row[0]) == store_module.CONVERSATION_TURN_LIMIT


async def test_one_bots_memory_does_not_trim_another(store) -> None:
    book, connection = store

    await book.append_turn(connection, bot_guid=1, role="user", content="mine", now=NOW)
    for index in range(store_module.CONVERSATION_TURN_LIMIT + 3):
        await book.append_turn(connection, bot_guid=2, role="user", content=str(index), now=NOW)

    assert len(await book.recent_turns(connection, bot_guid=1)) == 1


async def test_an_unsupported_role_is_refused_rather_than_stored(store) -> None:
    book, connection = store

    with pytest.raises(schema.LedgerError):
        await book.append_turn(connection, bot_guid=7, role="system", content="x", now=NOW)

    # The name says "rather than stored", so the test has to check that rather than
    # settling for the exception having been raised.
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_conversation_turn WHERE bot_guid = 7")
        row = await cursor.fetchone()
    assert int(row[0]) == 0


async def test_a_career_decision_round_trips_and_is_one_per_bot(store) -> None:
    book, connection = store

    assert await book.get_career_decision(connection, bot_guid=7) is None

    await book.record_career_decision(
        connection,
        bot_guid=7,
        career_version=1,
        candidate_token="career-alpha",
        spending_style="minimal",
        now=NOW,
    )
    await book.record_career_decision(
        connection,
        bot_guid=7,
        career_version=1,
        candidate_token="career-beta",
        spending_style="progression",
        now=NOW,
    )

    decision = await book.get_career_decision(connection, bot_guid=7)
    assert decision["candidate_token"] == "career-beta"
    assert decision["spending_style"] == "progression"


async def test_the_ambient_rate_holds_across_a_rolling_hour(store) -> None:
    book, connection = store

    for _ in range(3):
        assert await book.try_begin_ambient(connection, messages_per_hour=3, now=NOW) is True

    assert await book.try_begin_ambient(connection, messages_per_hour=3, now=NOW) is False

    # An hour later the window has rolled and the slots are back.
    later = NOW + store_module.AMBIENT_WINDOW + timedelta(seconds=1)
    assert await book.try_begin_ambient(connection, messages_per_hour=3, now=later) is True


async def test_an_out_of_range_ambient_rate_consumes_nothing(store) -> None:
    book, connection = store

    assert await book.try_begin_ambient(connection, messages_per_hour=0, now=NOW) is False
    assert (
        await book.try_begin_ambient(
            connection, messages_per_hour=store_module.MAX_AMBIENT_MESSAGES_PER_HOUR + 1, now=NOW
        )
        is False
    )

    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_ambient_attempt")
        row = await cursor.fetchone()
    assert int(row[0]) == 0


# Contention on the paths the earlier tests did not reach ---------------------------


async def test_the_very_first_reservation_of_a_day_succeeds_with_no_row_present(clean_ledger) -> None:
    """The path the other concurrency test skips by pre-creating the day row.

    Two transactions both taking a locking READ on a MISSING row acquire compatible gap
    locks and then both insert into the gap they are each holding, which deadlocks. That
    is the mirror of the insert-then-lock deadlock, and it only appears when nothing
    exists yet.
    """
    book, first_connection = clean_ledger
    second_connection = await _connect()

    try:
        barrier = asyncio.Barrier(2)
        one = budget.usd_to_nano("1.00")

        async def reserve(connection):
            await barrier.wait()
            return await book.reserve(
                connection,
                request_kind=RequestKind.CHAT_RESPONSE,
                model=anthropic_provider.MODEL_ID,
                max_cost_nano=one,
                priority=RequestPriority.IMMEDIATE_HUMAN,
                now=NOW,
            )

        # Nothing is pre-created: the day row does not exist when both start.
        first, second = await asyncio.gather(reserve(first_connection), reserve(second_connection))

        assert first[0] is AdmissionDecision.ADMITTED
        assert second[0] is AdmissionDecision.ADMITTED

        state = await book.snapshot(first_connection, now=NOW)
        assert state.outstanding_nano == one * 2
    finally:
        second_connection.close()


async def test_repeated_appends_for_one_bot_from_two_connections_settle_correctly(clean_ledger) -> None:
    """Each append inserts and then scans and deletes the same id range.

    Without per bot serialization two writers interleave those scans and deadlock. The
    lock is per bot, so this also checks the trim still landed on the right rows.
    """
    _, first_connection = clean_ledger
    second_connection = await _connect()
    book = store_module.SidecarStore()

    try:
        async with first_connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_llm_conversation_turn")
        await first_connection.commit()

        barrier = asyncio.Barrier(2)

        async def append_many(connection, label):
            await barrier.wait()
            for index in range(store_module.CONVERSATION_TURN_LIMIT):
                await book.append_turn(
                    connection, bot_guid=7, role="user", content=f"{label}{index}", now=NOW
                )

        await asyncio.gather(append_many(first_connection, "a"), append_many(second_connection, "b"))

        turns = await book.recent_turns(first_connection, bot_guid=7)
        assert len(turns) == store_module.CONVERSATION_TURN_LIMIT
    finally:
        second_connection.close()


async def test_two_ambient_attempts_for_the_last_slot_yield_exactly_one(clean_ledger) -> None:
    """A bare COUNT(*) FOR UPDATE either deadlocks or lets both callers read the same count.

    With one slot left, exactly one of two simultaneous callers may have it.
    """
    _, first_connection = clean_ledger
    second_connection = await _connect()
    book = store_module.SidecarStore()

    try:
        async with first_connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_llm_ambient_attempt")
        await first_connection.commit()

        # Two of the three slots are already gone.
        for _ in range(2):
            assert await book.try_begin_ambient(first_connection, messages_per_hour=3, now=NOW) is True

        barrier = asyncio.Barrier(2)

        async def attempt(connection):
            await barrier.wait()
            return await book.try_begin_ambient(connection, messages_per_hour=3, now=NOW)

        results = await asyncio.gather(attempt(first_connection), attempt(second_connection))

        assert sorted(results) == [False, True]
    finally:
        second_connection.close()


async def test_the_named_lock_really_blocks_a_second_holder(clean_ledger) -> None:
    """MySQL itself reports the block, rather than the test inferring it from a timeout.

    An earlier version asserted the contender had not finished within a second, which a
    slow or unscheduled task satisfies just as well as a blocked one. Setting a short
    innodb_lock_wait_timeout on the contender turns "it was blocked" into a positive
    error the database raises: with a working lock the contender times out waiting, and
    with a broken one it simply succeeds and the test fails.
    """
    _, holder = clean_ledger
    contender = await _connect()

    try:
        await holder.begin()
        async with holder.cursor() as cursor:
            await schema.acquire_named_lock(cursor, "test-key")

        async with contender.cursor() as cursor:
            await cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")

        await contender.begin()
        with pytest.raises(aiomysql.OperationalError) as caught:
            async with contender.cursor() as cursor:
                await schema.acquire_named_lock(cursor, "test-key")

        # 1205 is ER_LOCK_WAIT_TIMEOUT. The database is asserting the contention.
        assert caught.value.args[0] == 1205
        await contender.rollback()

        # And once the holder releases, the same acquisition goes through.
        await holder.commit()
        await contender.begin()
        async with contender.cursor() as cursor:
            await schema.acquire_named_lock(cursor, "test-key")
        await contender.commit()
    finally:
        contender.close()


async def test_a_different_key_is_not_blocked_by_a_held_one(clean_ledger) -> None:
    """Otherwise the bucketing below would be a global lock wearing a per bot name."""
    _, holder = clean_ledger
    other = await _connect()

    try:
        await holder.begin()
        async with holder.cursor() as cursor:
            await schema.acquire_named_lock(cursor, "key-a")

        async def take_other():
            await other.begin()
            async with other.cursor() as cursor:
                await schema.acquire_named_lock(cursor, "key-b")
            await other.commit()

        await asyncio.wait_for(take_other(), timeout=10.0)
        await holder.commit()
    finally:
        other.close()


async def test_the_lock_key_set_is_bounded(clean_ledger) -> None:
    """A per day or per bot key adds a permanent row forever.

    Deleting them instead is worse: removing a lock row while another transaction may
    still take that key reintroduces the missing-row race the helper exists to avoid.
    """
    book, connection = clean_ledger
    store = store_module.SidecarStore()

    async with connection.cursor() as cursor:
        await cursor.execute("DELETE FROM playerbot_llm_lock")
        await cursor.execute("DELETE FROM playerbot_llm_conversation_turn")
    await connection.commit()

    # Many bots and several days must not produce many keys.
    for bot_guid in range(0, CONVERSATION_LOCK_SAMPLE):
        await store.append_turn(connection, bot_guid=bot_guid, role="user", content="x", now=NOW)

    for offset in range(5):
        await book.reserve(
            connection,
            request_kind=RequestKind.CHAT_RESPONSE,
            model=anthropic_provider.MODEL_ID,
            max_cost_nano=1,
            priority=RequestPriority.IMMEDIATE_HUMAN,
            now=NOW + timedelta(days=offset),
        )

    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_lock")
        row = await cursor.fetchone()

    # One budget key plus at most one per conversation bucket, regardless of bot count
    # or how many days were touched.
    assert int(row[0]) <= 1 + schema.CONVERSATION_LOCK_BUCKETS
    assert int(row[0]) < CONVERSATION_LOCK_SAMPLE


async def test_retiring_superseded_locks_is_opt_in_and_leaves_live_keys_alone(clean_ledger) -> None:
    """Not run automatically, because a rolling restart makes that unsafe.

    An older sidecar still running against this database uses the per day and per bot
    keys as its live locks, so removing them mid flight strips its mutual exclusion.
    Leaving them is bounded anyway: nothing creates either shape any more.
    """
    _, connection = clean_ledger

    async with connection.cursor() as cursor:
        await cursor.execute("DELETE FROM playerbot_llm_lock")
        await cursor.executemany(
            "INSERT INTO playerbot_llm_lock (lock_key) VALUES (%s)",
            [
                ("budget_day:2026-08-01",),
                ("budget_day:2026-08-02",),
                ("conversation:9000",),
                ("conversation:5",),
                ("budget_day",),
                ("ambient",),
            ],
        )
    await connection.commit()

    # ensure_schema must NOT remove them on its own.
    await schema.ensure_schema(connection)
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_llm_lock")
        row = await cursor.fetchone()
    assert int(row[0]) == 6

    removed = await schema.retire_superseded_locks(connection)
    assert removed == 3  # two dated budget keys and the out of range conversation key

    async with connection.cursor() as cursor:
        await cursor.execute("SELECT lock_key FROM playerbot_llm_lock ORDER BY lock_key")
        rows = await cursor.fetchall()

    # The live keys survive, and so does the low numbered conversation key, which is
    # indistinguishable from a valid bucket.
    assert [r[0] for r in rows] == ["ambient", "budget_day", "conversation:5"]


# The façade the service actually talks to ------------------------------------------


@pytest.fixture
async def mysql_state() -> AsyncIterator[tuple[state_module.MySqlSidecarState, aiomysql.Pool]]:
    """A real MySqlSidecarState over a real pool.

    The unit suite substitutes an in-memory double for this interface, which proves the
    service's ordering but proves nothing about the delegation itself. A field passed to
    the wrong parameter, or a connection returned to the pool mid-transaction, is
    invisible there and visible here.
    """
    settings = _settings()
    state, pool = await state_module.open_state(settings, ceiling_nano=CEILING, reserve_ratio=QUARTER)
    # Every table this fixture's tests touch, not just the budget ones. Leaving the
    # ambient attempts behind made the first try_begin_ambient of a later test see a
    # rolling hour that was already full, which is a test failing for another test's
    # reason. Written out rather than looped over an interpolated name: a table name
    # cannot be a bound parameter, so a loop means building SQL from a string.
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_llm_budget_reservation")
            await cursor.execute("DELETE FROM playerbot_llm_daily_budget")
            await cursor.execute("DELETE FROM playerbot_social_runtime_control")
            await cursor.execute("DELETE FROM playerbot_llm_profile")
            await cursor.execute("DELETE FROM playerbot_llm_conversation_turn")
            await cursor.execute("DELETE FROM playerbot_llm_career_decision")
            await cursor.execute("DELETE FROM playerbot_llm_ambient_attempt")
        await connection.commit()
    try:
        yield state, pool
    finally:
        await state_module.close_pool(pool)


async def test_the_state_facade_carries_every_profile_field_to_the_right_column(mysql_state) -> None:
    """A swapped pair of affinities is a silent personality change, so read them back.

    Both the double and the real implementation take a whole request, so a mismatched
    unpacking would agree with itself everywhere except here.
    """
    state, _ = mysql_state

    request = protocol.parse_request(
        json.dumps(
            {
                "schema_version": protocol.SCHEMA_VERSION,
                "token": TOKEN,
                "request_id": 1,
                "channel": "whisper",
                "bot_guid": 4242,
                "speaker_guid": 9001,
                "bot_name": "Facadebot",
                "speaker_name": "Speaker",
                "profile_version": 2,
                # Deliberately all different, so any two swapped fields fail.
                "crafting_affinity": 11,
                "gathering_affinity": 22,
                "exploration_affinity": 33,
                "sociability": 44,
                "voice": "wry",
                "event_kind": 0,
                "subject_id": 0,
                "occurrence": 0,
                "message": "hello",
            }
        ).encode(),
        TOKEN,
    )

    await state.record_profile(request, now=NOW)

    assert await state.get_profile(bot_guid=4242) == {
        "profile_version": 2,
        "crafting_affinity": 11,
        "gathering_affinity": 22,
        "exploration_affinity": 33,
        "sociability": 44,
        "voice": "wry",
        "bot_name": "Facadebot",
    }


async def test_the_state_facade_reserves_settles_and_reports(mysql_state) -> None:
    state, _ = mysql_state
    one = budget.usd_to_nano("1.00")

    decision, reservation = await state.reserve(
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None
    assert (await state.budget_state(now=NOW)).outstanding_nano == one

    assert await state.settle(
        reservation=reservation, actual_cost_nano=one // 4, now=NOW
    ) == ledger.SettlementReceipt(True, one // 4, False, False)

    after = await state.budget_state(now=NOW)
    assert after.outstanding_nano == 0
    assert after.settled_nano == one // 4


async def test_the_state_facade_settles_decimal_costs_without_database_warnings(mysql_state) -> None:
    state, _ = mysql_state
    maximum = budget.usd_to_nano("1.00")
    costs = [budget.usd_to_nano("0.149573"), budget.usd_to_nano("0.001810")]

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for actual_cost in costs:
            decision, reservation = await state.reserve(
                request_kind=RequestKind.CHAT_RESPONSE,
                model=anthropic_provider.MODEL_ID,
                max_cost_nano=maximum,
                priority=RequestPriority.IMMEDIATE_HUMAN,
                now=NOW,
            )
            assert decision is AdmissionDecision.ADMITTED
            assert reservation is not None
            assert await state.settle(
                reservation=reservation, actual_cost_nano=actual_cost, now=NOW
            ) == ledger.SettlementReceipt(True, actual_cost, False, False)

    assert (await state.budget_state(now=NOW)).settled_nano == sum(costs)


async def test_the_state_facade_never_leaks_a_transaction_back_into_the_pool(mysql_state) -> None:
    """aiomysql DISCARDS a connection released mid-transaction, so a leak is invisible.

    It shows up only as connection churn: every read silently closes a socket and opens
    a new one. Reads are the risk, because autocommit is off and even a bare SELECT
    starts a transaction that nothing then closes.
    """
    state, pool = mysql_state

    for _ in range(4):
        await state.get_profile(bot_guid=999999)
        await state.recent_turns(bot_guid=999999)
        await state.budget_state(now=NOW)

    assert pool.size <= state_module.POOL_MAX_SIZE
    assert pool.freesize == pool.size, "a connection was discarded, so one was released mid-transaction"


async def test_a_repeated_request_gets_its_own_row_under_its_own_identity(
    mysql_state,
) -> None:
    """The worldserver's request ids restart at 1 on every process start.

    So request id 1 comes round again after a restart, and any key derived from it
    collides with the row the previous run left behind. The reservation's identity is
    minted here instead, which is what makes Definition of Done 3 hold for a repeat of
    any kind: a genuine retry, or a counter that wrapped over a restart.
    """
    state, pool = mysql_state
    one = budget.usd_to_nano("1.00")

    minted = []
    for _ in range(3):
        decision, reservation = await state.reserve(
            request_kind=RequestKind.CHAT_RESPONSE,
            model=anthropic_provider.MODEL_ID,
            max_cost_nano=one,
            priority=RequestPriority.IMMEDIATE_HUMAN,
            now=NOW,
        )
        assert decision is AdmissionDecision.ADMITTED
        assert reservation is not None
        minted.append(reservation.public_id)

    assert len(set(minted)) == 3, "two reservations shared an identity"

    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT public_id FROM playerbot_llm_budget_reservation WHERE budget_date = %s ORDER BY id",
                (NOW.date(),),
            )
            rows = await cursor.fetchall()
        await connection.commit()

    # Three rows, each carrying the identity the caller was handed back.
    assert [row[0] for row in rows] == minted


async def test_the_state_facade_carries_every_remaining_operation(mysql_state) -> None:
    """The methods the reserve and settle test does not touch.

    Each is a delegation with its own parameter list, and a delegation is exactly where a
    misrouted argument hides: the in-memory double the unit suite uses takes the same
    arguments and would agree with a wrong mapping. Read back rather than assumed.
    """
    state, pool = mysql_state

    assert await state.try_begin_ambient(messages_per_hour=2, now=NOW) is True
    assert await state.try_begin_ambient(messages_per_hour=2, now=NOW) is True
    assert await state.try_begin_ambient(messages_per_hour=2, now=NOW) is False

    await state.append_turn(bot_guid=7788, role="user", content="first", now=NOW)
    await state.append_turn(bot_guid=7788, role="assistant", content="second", now=NOW)
    assert await state.recent_turns(bot_guid=7788) == [("user", "first"), ("assistant", "second")]
    # A different bot's memory is its own.
    assert await state.recent_turns(bot_guid=7789) == []

    await state.record_career_decision(
        bot_guid=7788,
        career_version=1,
        candidate_token="career-def456",
        spending_style="progression",
        now=NOW,
    )
    # Read straight from the table rather than through a facade accessor. Nothing in the
    # sidecar reads a career decision back yet, and adding a method so a test can call it
    # is production surface that exists only for the test.
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT career_version, candidate_token, spending_style "
                "FROM playerbot_llm_career_decision WHERE bot_guid = %s",
                (7788,),
            )
            row = await cursor.fetchone()
        await connection.commit()
    assert row is not None
    assert (int(row[0]), row[1], row[2]) == (1, "career-def456", "progression")

    reserved = await state.reserve(
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.BACKGROUND,
        now=NOW,
    )
    assert reserved[1] is not None
    assert await state.release(reservation=reserved[1]) is True
    assert (await state.budget_state(now=NOW)).outstanding_nano == 0


class _StubAdapter(anthropic_provider.AnthropicProvider):
    """Stands in for the Anthropic SDK so no HTTP request is made. Everything else is real."""

    reply = "A fine day for fishing."

    def __init__(self) -> None:
        # Deliberately no super().__init__(): the stub never builds a real client.
        pass

    def count_input_tokens(self, request, history) -> int:
        return 100

    def generate_reply(self, request, history):
        return self.reply, provider.GenerationUsage(input_tokens=100, output_tokens=10)


def _service_request_payload(request_id: int, bot_guid: int) -> bytes:
    return json.dumps(
        {
            "schema_version": protocol.SCHEMA_VERSION,
            "token": TOKEN,
            "request_id": request_id,
            "channel": "whisper",
            "bot_guid": bot_guid,
            "speaker_guid": 9001,
            "bot_name": "Facadebot",
            "speaker_name": "Speaker",
            "profile_version": 2,
            "crafting_affinity": 11,
            "gathering_affinity": 22,
            "exploration_affinity": 33,
            "sociability": 44,
            "voice": "wry",
            "event_kind": 0,
            "subject_id": 0,
            "occurrence": 0,
            "message": "What do you enjoy doing?",
        }
    ).encode()


async def test_a_complete_service_request_runs_against_real_mysql(mysql_state) -> None:
    """The whole pipeline through the real facade, not the in-memory double.

    Every unit test of the service substitutes a double for the state, so the wiring
    between the service and MySQL is exercised by nothing else: a facade method that
    reached the wrong ledger call, or a connection returned mid-transaction under real
    load, would pass the entire unit suite. This reserves, generates, settles, and writes
    memory through the real pool, then reads the rows back.
    """
    state, pool = mysql_state
    request_id, bot_guid = 4242, 55555

    config = app.SidecarConfig(
        enable=True,
        bridge_port=0,
        daily_budget_usd="10.00",
        human_budget_reserve_ratio="0.25",
        input_usd_per_mtok="1.00",
        output_usd_per_mtok="5.00",
    )
    service = app.SidecarService(
        config=config, token=TOKEN, adapter=_StubAdapter(), store=state, now=lambda: NOW
    )

    payload = await service.process_payload(_service_request_payload(request_id, bot_guid))
    assert payload is not None
    assert json.loads(payload)["message"] == _StubAdapter.reply

    request = protocol.parse_request(_service_request_payload(request_id, bot_guid), TOKEN)
    assert await state.recent_turns(bot_guid=bot_guid) == [
        ("user", generation.build_user_message(request)),
        ("assistant", _StubAdapter.reply),
    ]

    profile = await state.get_profile(bot_guid=bot_guid)
    assert profile is not None
    assert profile["bot_name"] == "Facadebot"
    assert profile["gathering_affinity"] == 22

    after = await state.budget_state(now=NOW)
    assert after.outstanding_nano == 0
    # 100 input at $1/Mtok plus 10 output at $5/Mtok.
    assert after.settled_nano == budget.usd_to_nano("0.00015")

    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT state, actual_cost_usd, request_kind, model "
                "FROM playerbot_llm_budget_reservation WHERE budget_date = %s",
                (NOW.date(),),
            )
            rows = await cursor.fetchall()
        await connection.commit()

    assert len(rows) == 1
    assert rows[0][0] == "completed"
    assert budget.usd_to_nano(rows[0][1]) == after.settled_nano
    # A whisper is a chat response, and the model that served it is recorded because the
    # sidecar is the one that chose it.
    assert rows[0][2] == "chat_response"
    assert rows[0][3] == anthropic_provider.MODEL_ID

    # The pool is intact: nothing was discarded for holding a transaction open.
    assert pool.freesize == pool.size


async def test_a_cost_that_would_overflow_the_day_total_still_opens_the_circuit(clean_ledger) -> None:
    """The breaker must survive the exact report it exists to catch.

    spent_usd is DECIMAL(12, 6). Clamping only the incoming value still overflows the SUM
    once anything has been spent: MySQL rejects the statement in strict mode, the
    transaction rolls back, and the breaker stays shut while the reservation stays
    outstanding forever. That is the same failure mode as round 1's unstorable value, one
    level up, so this drives the addition rather than the addend.
    """
    book, connection = clean_ledger

    # Reserve first, while the day is empty and admission is possible.
    decision, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None

    # THEN put the day's total within a whisker of the column's ceiling. This is the
    # state a previous impossible report would have left behind, and it has to be set
    # after admission because a ledger holding that much would refuse every request.
    async with connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE playerbot_llm_daily_budget SET spent_usd = %s WHERE budget_date = %s",
            (budget.nano_to_usd_string(budget.MAX_STORABLE_NANO - 10_000), schema.utc_day(NOW)),
        )
    await connection.commit()

    # A report far above both the reservation and the remaining headroom.
    assert await book.settle(
        connection,
        reservation=reservation,
        actual_cost_nano=budget.MAX_STORABLE_NANO,
        now=NOW,
    ) == ledger.SettlementReceipt(
        True,
        budget.MAX_STORABLE_NANO,
        True,
        True,
    )

    state = await book.snapshot(connection, now=NOW)
    assert state.circuit_open is True, "the breaker must fire rather than the update failing"
    assert state.settled_nano == budget.MAX_STORABLE_NANO

    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT budget_circuit_reason, budget_circuit_opened_at "
            "FROM playerbot_social_runtime_control WHERE id = %s",
            (ledger.RUNTIME_CONTROL_ID,),
        )
        row = await cursor.fetchone()
    await connection.commit()

    assert row is not None
    # Both causes are named, because both happened: the report overran its reservation
    # AND the day total saturated. A reason that mentioned only one would send whoever
    # reads the incident after the wrong thing.
    assert f"over reservation {budget.nano_to_usd_string(reservation.max_cost_nano)}" in row[0]
    assert "saturated" in row[0]
    assert len(row[0]) <= ledger.CIRCUIT_REASON_LIMIT
    assert row[1] == NOW.replace(tzinfo=None)

    # And admission is closed afterwards.
    denied, _ = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=1,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert denied is AdmissionDecision.DENIED_CIRCUIT_OPEN


async def test_saturation_alone_is_reported_as_saturation_not_as_an_overrun(clean_ledger) -> None:
    """A reason must name the cause that actually occurred.

    A ceiling above MAX_STORABLE_NANO is refused at configuration time, so honest traffic
    cannot reach saturation without an overrun. This drives the ledger directly, past that
    guard, to prove the diagnostic is built from what happened rather than assuming the
    two always coincide. Reporting "cost exceeded reservation" for a cost that did not is
    how an incident investigation starts in the wrong place.
    """
    book, connection = clean_ledger

    reserve_amount = budget.usd_to_nano("1.00")
    decision, reservation = await book.reserve(
        connection,
        request_kind=RequestKind.CHAT_RESPONSE,
        model=anthropic_provider.MODEL_ID,
        max_cost_nano=reserve_amount,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None

    async with connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE playerbot_llm_daily_budget SET spent_usd = %s WHERE budget_date = %s",
            (budget.nano_to_usd_string(budget.MAX_STORABLE_NANO - 1_000), schema.utc_day(NOW)),
        )
    await connection.commit()

    # WITHIN its reservation, so circuit_should_open is false. Only saturation applies.
    assert await book.settle(
        connection, reservation=reservation, actual_cost_nano=reserve_amount, now=NOW
    ) == ledger.SettlementReceipt(
        True,
        reserve_amount,
        False,
        True,
    )

    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT budget_circuit_open, budget_circuit_reason "
            "FROM playerbot_social_runtime_control WHERE id = %s",
            (ledger.RUNTIME_CONTROL_ID,),
        )
        row = await cursor.fetchone()
    await connection.commit()

    assert row is not None
    assert bool(row[0]) is True, "an unrecordable total must still stop spending"
    assert "saturated" in row[1]
    assert "over reservation" not in row[1], "the cost did NOT exceed its reservation"
