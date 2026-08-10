-- Rename the provider-backed Playerbot tables without creating a second store.
--
-- The two budget tables belong to mod-playerbots. The five operational tables belong to
-- the sidecar and may be absent on a fresh database. Only three table layouts are valid:
-- the two legacy budget tables, all seven legacy tables, or all seven neutral tables.
-- Every other layout is refused before the first DDL statement.

SET @old_budget_tables := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_daily_budget', 'playerbot_claude_budget_reservation')
);
SET @old_sidecar_tables := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN (
      'playerbot_claude_lock',
      'playerbot_claude_profile',
      'playerbot_claude_conversation_turn',
      'playerbot_claude_career_decision',
      'playerbot_claude_ambient_attempt'
    )
);
SET @new_budget_tables := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_llm_daily_budget', 'playerbot_llm_budget_reservation')
);
SET @new_sidecar_tables := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN (
      'playerbot_llm_lock',
      'playerbot_llm_profile',
      'playerbot_llm_conversation_turn',
      'playerbot_llm_career_decision',
      'playerbot_llm_ambient_attempt'
    )
);

SET @fresh_legacy_tables := (
  @old_budget_tables = 2 AND @old_sidecar_tables = 0
  AND @new_budget_tables = 0 AND @new_sidecar_tables = 0
);
SET @complete_legacy_tables := (
  @old_budget_tables = 2 AND @old_sidecar_tables = 5
  AND @new_budget_tables = 0 AND @new_sidecar_tables = 0
);
SET @complete_neutral_tables := (
  @old_budget_tables = 0 AND @old_sidecar_tables = 0
  AND @new_budget_tables = 2 AND @new_sidecar_tables = 5
);
SET @table_layout_valid := (
  @fresh_legacy_tables OR @complete_legacy_tables OR @complete_neutral_tables
);

-- A valid set of names is not enough. CREATE TABLE IF NOT EXISTS and RENAME TABLE preserve an
-- incompatible existing definition, so every present provider table must match the canonical
-- columns, defaults, indexes, and checks before the first DDL statement runs.
SET @expected_provider_columns := IF(@fresh_legacy_tables, 17, 39);
SELECT COUNT(1), COALESCE(SUM(
      CASE
        WHEN table_suffix = 'daily_budget' AND COLUMN_NAME = 'budget_date'
          AND COLUMN_TYPE = 'date' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'daily_budget' AND COLUMN_NAME IN ('reserved_usd', 'spent_usd')
          AND COLUMN_TYPE = 'decimal(12,6)' AND IS_NULLABLE = 'NO'
          AND CAST(COLUMN_DEFAULT AS DECIMAL(20, 6)) = 0
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'daily_budget' AND COLUMN_NAME = 'created_at'
          AND COLUMN_TYPE = 'timestamp' AND IS_NULLABLE = 'NO'
          AND LOWER(REPLACE(COLUMN_DEFAULT, '()', '')) = 'current_timestamp'
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'daily_budget' AND COLUMN_NAME = 'updated_at'
          AND COLUMN_TYPE = 'timestamp' AND IS_NULLABLE = 'NO'
          AND LOWER(REPLACE(COLUMN_DEFAULT, '()', '')) = 'current_timestamp'
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA LIKE '%on update CURRENT_TIMESTAMP%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'id'
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'public_id'
          AND COLUMN_TYPE = 'char(36)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'budget_date'
          AND COLUMN_TYPE = 'date' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'request_kind'
          AND COLUMN_TYPE = CONCAT(
            'enum(''chat_response'',''backstory_generation'',''memory_extraction'',',
            '''moderation_classification'',''career_generation'')'
          )
          AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'priority_lane'
          AND COLUMN_TYPE = CONCAT(
            'enum(''unspecified'',''direct_human'',''mixed_human_bot'',',
            '''career_generation'',''bot_only_continuation'',''new_starter'',''background_extraction'')'
          )
          AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = 'unspecified'
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'model'
          AND COLUMN_TYPE = 'varchar(64)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'max_cost_usd'
          AND COLUMN_TYPE = 'decimal(12,6)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'actual_cost_usd'
          AND COLUMN_TYPE = 'decimal(12,6)' AND IS_NULLABLE = 'YES' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'state'
          AND COLUMN_TYPE = 'enum(''reserved'',''completed'',''released'',''expired'')'
          AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = 'reserved'
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'created_at'
          AND COLUMN_TYPE = 'timestamp' AND IS_NULLABLE = 'NO'
          AND LOWER(REPLACE(COLUMN_DEFAULT, '()', '')) = 'current_timestamp'
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'expires_at'
          AND COLUMN_TYPE = 'datetime' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'budget_reservation' AND COLUMN_NAME = 'settled_at'
          AND COLUMN_TYPE = 'datetime' AND IS_NULLABLE = 'YES' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'lock' AND COLUMN_NAME = 'lock_key'
          AND COLUMN_TYPE = 'varchar(64)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'profile' AND COLUMN_NAME = 'bot_guid'
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'profile' AND COLUMN_NAME = 'profile_version'
          AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'profile'
          AND COLUMN_NAME IN ('crafting_affinity', 'gathering_affinity', 'exploration_affinity', 'sociability')
          AND COLUMN_TYPE = 'tinyint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'profile' AND COLUMN_NAME = 'voice'
          AND COLUMN_TYPE = 'varchar(32)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'profile' AND COLUMN_NAME = 'bot_name'
          AND COLUMN_TYPE = 'varchar(48)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'profile' AND COLUMN_NAME = 'updated_at'
          AND COLUMN_TYPE = 'datetime' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'conversation_turn' AND COLUMN_NAME = 'id'
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'conversation_turn' AND COLUMN_NAME = 'bot_guid'
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'conversation_turn' AND COLUMN_NAME = 'role'
          AND COLUMN_TYPE = 'enum(''user'',''assistant'')'
          AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'conversation_turn' AND COLUMN_NAME = 'content'
          AND COLUMN_TYPE = 'text' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'conversation_turn' AND COLUMN_NAME = 'created_at'
          AND COLUMN_TYPE = 'datetime' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'career_decision' AND COLUMN_NAME = 'bot_guid'
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'career_decision' AND COLUMN_NAME = 'career_version'
          AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'career_decision' AND COLUMN_NAME = 'candidate_token'
          AND COLUMN_TYPE = 'varchar(64)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'career_decision' AND COLUMN_NAME = 'spending_style'
          AND COLUMN_TYPE = 'varchar(32)' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'career_decision' AND COLUMN_NAME = 'updated_at'
          AND COLUMN_TYPE = 'datetime' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'ambient_attempt' AND COLUMN_NAME = 'id'
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        WHEN table_suffix = 'ambient_attempt' AND COLUMN_NAME = 'created_at'
          AND COLUMN_TYPE = 'datetime' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL
          AND EXTRA NOT LIKE '%auto_increment%' AND EXTRA NOT LIKE '%on update%' THEN 1
        ELSE 0
      END
    ), 0)
  INTO @provider_column_count, @provider_column_match_count
  FROM (
    SELECT
      CASE
        WHEN TABLE_NAME LIKE 'playerbot_claude_%'
          THEN REPLACE(TABLE_NAME, 'playerbot_claude_', '')
        ELSE REPLACE(TABLE_NAME, 'playerbot_llm_', '')
      END AS table_suffix,
      COLUMN_NAME,
      LOWER(COLUMN_TYPE) AS COLUMN_TYPE,
      IS_NULLABLE,
      COLUMN_DEFAULT,
      EXTRA
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME IN (
        'playerbot_claude_daily_budget', 'playerbot_claude_budget_reservation',
        'playerbot_claude_lock', 'playerbot_claude_profile',
        'playerbot_claude_conversation_turn', 'playerbot_claude_career_decision',
        'playerbot_claude_ambient_attempt', 'playerbot_llm_daily_budget',
        'playerbot_llm_budget_reservation', 'playerbot_llm_lock', 'playerbot_llm_profile',
        'playerbot_llm_conversation_turn', 'playerbot_llm_career_decision',
        'playerbot_llm_ambient_attempt'
      )
  ) AS provider_columns;
SET @provider_column_shape_valid := (
  @provider_column_count = @expected_provider_columns
  AND @provider_column_match_count = @expected_provider_columns
);

SET @expected_provider_index_rows := IF(@fresh_legacy_tables, 7, 15);
SET @provider_index_shape_valid := (
  SELECT COUNT(1) = @expected_provider_index_rows
    AND COALESCE(SUM(
      CASE
        WHEN table_suffix = 'daily_budget' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'budget_date' THEN 1
        WHEN table_suffix = 'budget_reservation' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'id' THEN 1
        WHEN table_suffix = 'budget_reservation' AND index_name = 'uk_llm_reservation_public_id'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'public_id' THEN 1
        WHEN table_suffix = 'budget_reservation' AND index_name = 'ix_llm_reservation_day_state'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'budget_date' THEN 1
        WHEN table_suffix = 'budget_reservation' AND index_name = 'ix_llm_reservation_day_state'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'state' THEN 1
        WHEN table_suffix = 'budget_reservation' AND index_name = 'ix_llm_reservation_expiry'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'state' THEN 1
        WHEN table_suffix = 'budget_reservation' AND index_name = 'ix_llm_reservation_expiry'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'expires_at' THEN 1
        WHEN table_suffix = 'lock' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'lock_key' THEN 1
        WHEN table_suffix = 'profile' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'bot_guid' THEN 1
        WHEN table_suffix = 'conversation_turn' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'id' THEN 1
        WHEN table_suffix = 'conversation_turn' AND index_name = 'ix_bot'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'bot_guid' THEN 1
        WHEN table_suffix = 'conversation_turn' AND index_name = 'ix_bot'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'id' THEN 1
        WHEN table_suffix = 'career_decision' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'bot_guid' THEN 1
        WHEN table_suffix = 'ambient_attempt' AND index_name = 'PRIMARY'
          AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'id' THEN 1
        WHEN table_suffix = 'ambient_attempt' AND index_name = 'ix_created'
          AND NON_UNIQUE = 1 AND SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'created_at' THEN 1
        ELSE 0
      END
    ), 0) = @expected_provider_index_rows
  FROM (
    SELECT
      CASE
        WHEN TABLE_NAME LIKE 'playerbot_claude_%'
          THEN REPLACE(TABLE_NAME, 'playerbot_claude_', '')
        ELSE REPLACE(TABLE_NAME, 'playerbot_llm_', '')
      END AS table_suffix,
      REPLACE(INDEX_NAME, '_claude_', '_llm_') AS index_name,
      NON_UNIQUE,
      SEQ_IN_INDEX,
      COLUMN_NAME
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME IN (
        'playerbot_claude_daily_budget', 'playerbot_claude_budget_reservation',
        'playerbot_claude_lock', 'playerbot_claude_profile',
        'playerbot_claude_conversation_turn', 'playerbot_claude_career_decision',
        'playerbot_claude_ambient_attempt', 'playerbot_llm_daily_budget',
        'playerbot_llm_budget_reservation', 'playerbot_llm_lock', 'playerbot_llm_profile',
        'playerbot_llm_conversation_turn', 'playerbot_llm_career_decision',
        'playerbot_llm_ambient_attempt'
      )
  ) AS provider_indexes
);

SET @provider_check_shape_valid := (
  SELECT COUNT(1) = 4
    AND COALESCE(SUM(
      CASE
        WHEN table_suffix = 'daily_budget' AND constraint_name = 'ck_llm_daily_budget_reserved'
          AND check_clause = 'reserved_usd>=0' THEN 1
        WHEN table_suffix = 'daily_budget' AND constraint_name = 'ck_llm_daily_budget_spent'
          AND check_clause = 'spent_usd>=0' THEN 1
        WHEN table_suffix = 'budget_reservation' AND constraint_name = 'ck_llm_reservation_max_cost'
          AND check_clause = 'max_cost_usd>=0' THEN 1
        WHEN table_suffix = 'budget_reservation' AND constraint_name = 'ck_llm_reservation_actual_cost'
          AND check_clause = 'actual_cost_usdisnulloractual_cost_usd>=0' THEN 1
        ELSE 0
      END
    ), 0) = 4
  FROM (
    SELECT
      CASE
        WHEN tc.TABLE_NAME LIKE 'playerbot_claude_%'
          THEN REPLACE(tc.TABLE_NAME, 'playerbot_claude_', '')
        ELSE REPLACE(tc.TABLE_NAME, 'playerbot_llm_', '')
      END AS table_suffix,
      REPLACE(tc.CONSTRAINT_NAME, '_claude_', '_llm_') AS constraint_name,
      LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cc.CHECK_CLAUSE,
        '`', ''), ' ', ''), '(', ''), ')', '')) AS check_clause
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
    JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS AS cc
      ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
      AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
    WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
      AND tc.CONSTRAINT_TYPE = 'CHECK'
      AND tc.TABLE_NAME IN (
        'playerbot_claude_daily_budget', 'playerbot_claude_budget_reservation',
        'playerbot_claude_lock', 'playerbot_claude_profile',
        'playerbot_claude_conversation_turn', 'playerbot_claude_career_decision',
        'playerbot_claude_ambient_attempt', 'playerbot_llm_daily_budget',
        'playerbot_llm_budget_reservation', 'playerbot_llm_lock', 'playerbot_llm_profile',
        'playerbot_llm_conversation_turn', 'playerbot_llm_career_decision',
        'playerbot_llm_ambient_attempt'
      )
  ) AS provider_checks
);

SET @provider_shape_valid := (
  @provider_column_shape_valid
  AND @provider_index_shape_valid
  AND @provider_check_shape_valid
);

-- Each coupled budget identifier must exist exactly once under either its old or new name.
-- This admits recovery after an interrupted identifier rename, but rejects missing identifiers
-- and old/new collisions before changing another object.
SET @daily_reserved_identifiers := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_daily_budget', 'playerbot_llm_daily_budget')
    AND CONSTRAINT_TYPE = 'CHECK'
    AND CONSTRAINT_NAME IN ('ck_claude_daily_budget_reserved', 'ck_llm_daily_budget_reserved')
);
SET @daily_spent_identifiers := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_daily_budget', 'playerbot_llm_daily_budget')
    AND CONSTRAINT_TYPE = 'CHECK'
    AND CONSTRAINT_NAME IN ('ck_claude_daily_budget_spent', 'ck_llm_daily_budget_spent')
);
SET @reservation_public_id_identifiers := (
  SELECT COUNT(DISTINCT INDEX_NAME)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_budget_reservation', 'playerbot_llm_budget_reservation')
    AND INDEX_NAME IN ('uk_claude_reservation_public_id', 'uk_llm_reservation_public_id')
);
SET @reservation_day_state_identifiers := (
  SELECT COUNT(DISTINCT INDEX_NAME)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_budget_reservation', 'playerbot_llm_budget_reservation')
    AND INDEX_NAME IN ('ix_claude_reservation_day_state', 'ix_llm_reservation_day_state')
);
SET @reservation_expiry_identifiers := (
  SELECT COUNT(DISTINCT INDEX_NAME)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_budget_reservation', 'playerbot_llm_budget_reservation')
    AND INDEX_NAME IN ('ix_claude_reservation_expiry', 'ix_llm_reservation_expiry')
);
SET @reservation_max_cost_identifiers := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_budget_reservation', 'playerbot_llm_budget_reservation')
    AND CONSTRAINT_TYPE = 'CHECK'
    AND CONSTRAINT_NAME IN ('ck_claude_reservation_max_cost', 'ck_llm_reservation_max_cost')
);
SET @reservation_actual_cost_identifiers := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('playerbot_claude_budget_reservation', 'playerbot_llm_budget_reservation')
    AND CONSTRAINT_TYPE = 'CHECK'
    AND CONSTRAINT_NAME IN ('ck_claude_reservation_actual_cost', 'ck_llm_reservation_actual_cost')
);

SET @identifier_layout_valid := (
  @daily_reserved_identifiers = 1
  AND @daily_spent_identifiers = 1
  AND @reservation_public_id_identifiers = 1
  AND @reservation_day_state_identifiers = 1
  AND @reservation_expiry_identifiers = 1
  AND @reservation_max_cost_identifiers = 1
  AND @reservation_actual_cost_identifiers = 1
);

SET @ddl := IF(
  NOT @table_layout_valid,
  'CALL playerbot_llm_migration_refused_due_to_mixed_or_colliding_schema();',
  IF(
    NOT @provider_shape_valid,
    'CALL playerbot_llm_migration_refused_due_to_unexpected_table_shape();',
    IF(
      @identifier_layout_valid,
      'SELECT "Playerbot LLM table migration preflight passed.";',
      'CALL playerbot_llm_migration_refused_due_to_mixed_or_colliding_schema();'
    )
  )
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl := IF(
  @fresh_legacy_tables,
  'RENAME TABLE
    `playerbot_claude_daily_budget` TO `playerbot_llm_daily_budget`,
    `playerbot_claude_budget_reservation` TO `playerbot_llm_budget_reservation`;',
  IF(
    @complete_legacy_tables,
    'RENAME TABLE
      `playerbot_claude_daily_budget` TO `playerbot_llm_daily_budget`,
      `playerbot_claude_budget_reservation` TO `playerbot_llm_budget_reservation`,
      `playerbot_claude_lock` TO `playerbot_llm_lock`,
      `playerbot_claude_profile` TO `playerbot_llm_profile`,
      `playerbot_claude_conversation_turn` TO `playerbot_llm_conversation_turn`,
      `playerbot_claude_career_decision` TO `playerbot_llm_career_decision`,
      `playerbot_claude_ambient_attempt` TO `playerbot_llm_ambient_attempt`;',
    'SELECT "Playerbot LLM tables already use neutral names.";'
  )
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_daily_budget'
    AND CONSTRAINT_NAME = 'ck_claude_daily_budget_reserved'
);
SET @ddl := IF(
  @has_old_identifier = 1,
  'ALTER TABLE `playerbot_llm_daily_budget`
    DROP CHECK `ck_claude_daily_budget_reserved`,
    ADD CONSTRAINT `ck_llm_daily_budget_reserved` CHECK (`reserved_usd` >= 0);',
  'SELECT "Daily reserved constraint already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_daily_budget'
    AND CONSTRAINT_NAME = 'ck_claude_daily_budget_spent'
);
SET @ddl := IF(
  @has_old_identifier = 1,
  'ALTER TABLE `playerbot_llm_daily_budget`
    DROP CHECK `ck_claude_daily_budget_spent`,
    ADD CONSTRAINT `ck_llm_daily_budget_spent` CHECK (`spent_usd` >= 0);',
  'SELECT "Daily spent constraint already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_budget_reservation'
    AND INDEX_NAME = 'uk_claude_reservation_public_id'
);
SET @ddl := IF(
  @has_old_identifier > 0,
  'ALTER TABLE `playerbot_llm_budget_reservation`
    RENAME INDEX `uk_claude_reservation_public_id` TO `uk_llm_reservation_public_id`;',
  'SELECT "Reservation public identity index already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_budget_reservation'
    AND INDEX_NAME = 'ix_claude_reservation_day_state'
);
SET @ddl := IF(
  @has_old_identifier > 0,
  'ALTER TABLE `playerbot_llm_budget_reservation`
    RENAME INDEX `ix_claude_reservation_day_state` TO `ix_llm_reservation_day_state`;',
  'SELECT "Reservation day state index already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_budget_reservation'
    AND INDEX_NAME = 'ix_claude_reservation_expiry'
);
SET @ddl := IF(
  @has_old_identifier > 0,
  'ALTER TABLE `playerbot_llm_budget_reservation`
    RENAME INDEX `ix_claude_reservation_expiry` TO `ix_llm_reservation_expiry`;',
  'SELECT "Reservation expiry index already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_budget_reservation'
    AND CONSTRAINT_NAME = 'ck_claude_reservation_max_cost'
);
SET @ddl := IF(
  @has_old_identifier = 1,
  'ALTER TABLE `playerbot_llm_budget_reservation`
    DROP CHECK `ck_claude_reservation_max_cost`,
    ADD CONSTRAINT `ck_llm_reservation_max_cost` CHECK (`max_cost_usd` >= 0);',
  'SELECT "Reservation maximum cost constraint already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_old_identifier := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_budget_reservation'
    AND CONSTRAINT_NAME = 'ck_claude_reservation_actual_cost'
);
SET @ddl := IF(
  @has_old_identifier = 1,
  'ALTER TABLE `playerbot_llm_budget_reservation`
    DROP CHECK `ck_claude_reservation_actual_cost`,
    ADD CONSTRAINT `ck_llm_reservation_actual_cost`
      CHECK (`actual_cost_usd` IS NULL OR `actual_cost_usd` >= 0);',
  'SELECT "Reservation actual cost constraint already uses the neutral name.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @daily_comment_neutral := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_daily_budget'
    AND TABLE_COMMENT = 'Locked daily aggregate for generation provider spend admission'
);
SET @ddl := IF(
  @daily_comment_neutral = 0,
  'ALTER TABLE `playerbot_llm_daily_budget`
    COMMENT = ''Locked daily aggregate for generation provider spend admission'';',
  'SELECT "Daily budget comment already uses neutral terminology.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @reservation_comment_neutral := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_llm_budget_reservation'
    AND TABLE_COMMENT = 'One conservative maximum reservation per generation request attempt'
);
SET @ddl := IF(
  @reservation_comment_neutral = 0,
  'ALTER TABLE `playerbot_llm_budget_reservation`
    COMMENT = ''One conservative maximum reservation per generation request attempt'';',
  'SELECT "Budget reservation comment already uses neutral terminology.";'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
