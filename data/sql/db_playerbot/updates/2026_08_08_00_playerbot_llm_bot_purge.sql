CREATE TABLE IF NOT EXISTS `playerbot_llm_bot_purge` (
  `bot_guid` BIGINT UNSIGNED NOT NULL,
  `acknowledged_at` TIMESTAMP NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`bot_guid`),
  KEY `ix_llm_bot_purge_pending` (`acknowledged_at`, `bot_guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Durable bot cohort purge intents consumed by the LLM sidecar';
