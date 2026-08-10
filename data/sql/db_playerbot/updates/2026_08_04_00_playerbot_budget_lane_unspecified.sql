-- Interactive Playerbot Social Chat: admit that the budget ledger does not know the priority lane.
--
-- Adds one enumerator to `playerbot_claude_budget_reservation`.`priority_lane` and defaults the
-- column to it. It creates no table, drops nothing, and backfills nothing.
--
-- Why this column changes.
--
-- The lane is computed on the C++ side, in `PlayerbotSocialAdmissionLane`. It is collapsed to a
-- four value queue priority by `PlayerbotSocialPriorityForLane` and then discarded. It is never
-- encoded onto a bridge request, so the sidecar, which is the only writer of this table, has no
-- way to learn it. The column as first written was `NOT NULL` with no interim value, which left
-- the writer a choice between refusing to insert and inventing a lane. Inventing one is worse than
-- leaving it blank: a wrong lane in a telemetry column reads exactly like a right one.
--
-- `unspecified` is not a guess about which lane a request was in. It is an accurate statement that
-- the row's producer does not know. That is the same shape the plan already accepted for the other
-- producer facts in this telemetry row. Its Telemetry Coverage table records model, latency, and
-- token counts as having no producer today and routes them to a separate telemetry task; the
-- priority lane is named in that same row and is not materially different from the three beside it.
-- The truthful lane arrives when that task gives this column a producer.
--
-- A named enumerator rather than a NULLable column, deliberately. NULL cannot distinguish "the
-- producer does not know" from "nothing wrote this row's lane at all", and the difference matters
-- to whoever reads the column after the telemetry producer lands.
--
-- The enumerator is listed first so it sorts ahead of the real lanes, and is the column default so
-- a writer that omits the field records the honest value rather than falling through to whichever
-- lane happens to be first in the list.
--
-- Idempotent by INFORMATION_SCHEMA guard rather than by a bare MODIFY, which would succeed every
-- time and rewrite the table on each run. Follows the pattern used by the revisions beside it.

SET @budget_lane_has_unspecified := (
  SELECT COUNT(1)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'playerbot_claude_budget_reservation'
    AND COLUMN_NAME = 'priority_lane'
    AND COLUMN_TYPE LIKE '%\'unspecified\'%'
);

SET @ddl := IF(@budget_lane_has_unspecified = 0,
  'ALTER TABLE `playerbot_claude_budget_reservation`
     MODIFY `priority_lane` ENUM(''unspecified'', ''direct_human'', ''mixed_human_bot'',
       ''career_generation'', ''bot_only_continuation'', ''new_starter'', ''background_extraction'')
       NOT NULL DEFAULT ''unspecified''
       COMMENT ''Admission lane; unspecified until a producer sends it'';',
  'SELECT "Column priority_lane already admits unspecified.";'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
