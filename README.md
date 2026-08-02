# mod-playerbot-claude

A private AzerothCore module that gives [mod-playerbots](https://github.com/Fuitad/mod-playerbots) bots short, in character Claude Haiku dialogue and one bounded profession career choice. The model has no tools and receives no live game state. Playerbot code supplies opaque legal candidates and validates the response. Chat failures end in silence. Career failures use the deterministic playerbot fallback.

## Compatibility

This module is developed against one exact revision of the private `Fuitad/mod-playerbots` repository. That revision is recorded in [`PLAYERBOTS_REVISION`](PLAYERBOTS_REVISION) and is the only supported combination.

Verify before building:

```bash
test "$(cat modules/mod-playerbot-claude/PLAYERBOTS_REVISION)" = "$(git -C modules/mod-playerbots rev-parse HEAD)"
```

The module consumes the versioned personality contract (`PLAYERBOT_PERSONALITY_API_VERSION == 2`) from mod-playerbots. Configuration fails with a clear error when mod-playerbots is missing, and compilation fails when the personality API version changes.

Requirements:

- AzerothCore (WotLK 3.3.5a), C++20 toolchain, CMake
- `Fuitad/mod-playerbots` at the pinned revision, checked out in `modules/`
- Python 3.12 and [uv](https://docs.astral.sh/uv/) for the sidecar
- An Anthropic API key

## Install

1. Clone this repository into `modules/mod-playerbot-claude`, next to `modules/mod-playerbots`.
2. Verify the pinned revision (command above).
3. Configure and build worldserver as usual. The module registers itself through `mod-playerbot-claude.cmake`.
4. Install the sidecar environment:

```bash
cd modules/mod-playerbot-claude/sidecar
uv sync --locked
```

5. Copy `conf/mod_playerbot_claude.conf.dist` to your config directory as `mod_playerbot_claude.conf` and edit it. Everything ships disabled.

## Configuration

All keys live in `mod_playerbot_claude.conf` and are read by both worldserver and the sidecar. See the `.dist` file for full documentation.

| Key | Default | Meaning |
| --- | --- | --- |
| `PlayerbotClaude.Enable` | `0` | Master switch for the worldserver hooks. |
| `PlayerbotClaude.AmbientWorldEnable` | `0` | Enables personality shaped ambient World chatter. |
| `PlayerbotClaude.AmbientMaxMessagesPerHour` | `6` | Persistent rolling hourly limit. Values above `60` disable ambient chatter. |
| `PlayerbotClaude.BridgePort` | `0` | Loopback TCP port shared by worldserver and sidecar. `0` disables both. |
| `PlayerbotClaude.DailyBudgetUsd` | `5` | The daily UTC ceiling shared by all Claude generation. The configured value is the only ceiling. |
| `PlayerbotClaude.HumanBudgetReserveRatio` | `0.25` | Share of the ceiling reserved for work a player is waiting on. Valid from `0` through `1`. |
| `PlayerbotClaude.InputUsdPerMTok` | `1.00` | Price per million input tokens (Claude Haiku 4.5). |
| `PlayerbotClaude.OutputUsdPerMTok` | `5.00` | Price per million output tokens (Claude Haiku 4.5). |
| `PlayerbotClaude.ResponseDeadlineMs` | `10000` | How long a pending conversation or career choice waits. |
| `PlayerbotClaude.QueueSize` | `16` | Bounded queue between the world thread and the bridge worker. |
| `PlayerbotClaude.GroupCooldownSeconds` | `120` | Minimum seconds between milestone reactions per group. |

Secrets are never read from configuration files:

- `PLAYERBOT_CLAUDE_BRIDGE_TOKEN` authenticates the loopback connection. Both processes require it and refuse to start when it is missing or shorter than 32 bytes. It never appears in logs, storage, or output.
- `MOD_PLAYERBOT_CLAUDE_APIKEY` is the Anthropic API key, read only by the sidecar. The global `ANTHROPIC_API_KEY` variable is deliberately ignored so a machine-wide key can never be used implicitly by this module.

## Operation

Start the sidecar before (or alongside) worldserver:

```bash
cd modules/mod-playerbot-claude/sidecar
export PLAYERBOT_CLAUDE_BRIDGE_TOKEN=...   # >= 32 bytes
export MOD_PLAYERBOT_CLAUDE_APIKEY=sk-ant-...
uv run playerbot-claude serve \
  --config /path/to/mod_playerbot_claude.conf \
  --playerbots-config /path/to/playerbots.conf
```

`--playerbots-config` points at the deployed `playerbots.conf` holding `PlayerbotsDatabaseInfo`. The sidecar reads the connection settings from there instead of keeping a second copy, so there is only one place to rotate the password. Every command needs it, because every command reads durable state.

Health check (never prints a secret, exits nonzero when misconfigured):

```bash
uv run playerbot-claude doctor \
  --config /path/to/mod_playerbot_claude.conf \
  --playerbots-config /path/to/playerbots.conf
```

The report includes today's settled, outstanding, and remaining amounts, the protected human reserve, whether the budget circuit breaker is open, and whether the database was reachable. It never includes prompts, connection settings, or secrets.

Inspect the observed personality profile of a bot that has spoken through the bridge:

```bash
uv run playerbot-claude profile \
  --config /path/to/mod_playerbot_claude.conf \
  --playerbots-config /path/to/playerbots.conf \
  --bot-guid 42
```

## Talking to bots

- **Whisper** a bot naturally: any whisper that mod-playerbots does not recognize as a bot command becomes Claude conversation. Known commands (`follow`, `grind`, item links, and every other chat trigger) still execute as commands and cost no tokens. Prefixing with `llm ` forces Claude even for command-shaped text (`llm follow` asks the bot about following instead of ordering it to).
- **Party chat**: `llm <bot-name> <message>` addresses one bot in your group. Party chat keeps the explicit prefix so ordinary group conversation never reaches the API.
- **Milestones**: after a quest completion, a level gain, or a rare or epic loot drop, one deterministically selected group bot may react, at most once per `GroupCooldownSeconds` per group.
- **Ambient World chatter**: set `PlayerbotClaude.AmbientWorldEnable = 1` and set `AiPlayerbot.EnableBroadcasts = 0` in `playerbots.conf`. The module refuses ambient mode while canned broadcasts remain enabled, but direct Claude conversations continue working. Ambient attempts occur only while a human player is connected, use an eligible online bot that is alive, outside combat, and able to speak in the human player's faction World channel, and are limited to the configured rolling hourly limit. Delivery checks the human, bot, and channel membership again before speech.

Replies are one short line (at most 240 bytes), in the bot's fixed voice. If anything fails (budget, timeout, provider error, invalid output), the bot simply stays silent.

## Personality and career behavior

Bots reuse the deterministic personality profiles defined by mod-playerbots (see its `docs/personality.md`). Version 2 provides independent crafting, gathering, exploration, and sociability scores from 0 to 100, plus one of five voices.

When playerbot code needs a new career plan, this module may submit one request containing only opaque legal candidate tokens, short candidate categories, maximum spending styles, market eligibility, and engagement. The model chooses one token and a permitted style. It cannot invent a profession, recipe, spell, destination, price, or runtime action. Playerbot code validates the correlation, versions, token, and style before persistence. Disabled, unavailable, invalid, or late responses use the same deterministic fallback as a server without this module.

## What is sent to the cloud

For a direct conversation or milestone, the sidecar sends the following to the Anthropic API:

- The bot's name and its personality numbers and voice (derived data, not player data)
- The channel kind (whisper or party) and the milestone kind, when applicable
- The name of the speaking player character
- The player's `llm` message text, marked as untrusted
- Up to 20 prior turns of that bot's stored conversation memory

An ambient request sends only the selected bot's name, personality numbers, voice, and a fixed instruction to offer one brief World observation. It sends no human identity, human text, or conversation history. Ambient input and output are never appended to conversation memory.

A career request sends the bot name, personality values, and opaque candidate descriptions. It sends no raw profession, skill, spell, item, or recipe identifiers and no conversation history. The returned career decision is diagnostic data only in the sidecar. The authoritative validated plan remains in mod-playerbots.

Never sent: account names, GUIDs, IP addresses, positions, inventories, combat state, the bridge token, recognized bot commands, or any party/group chat without the `llm ` prefix. Whispers are already one-to-one text addressed to a specific bot; every whisper the command system does not consume is treated as conversation with that bot and leaves the machine. If you want the stricter opt-in-per-message behavior back, whisper traffic is exactly the `llm `-prefixed subset.

## Budgeting

Cost per reply is `(input_tokens * InputUsdPerMTok + output_tokens * OutputUsdPerMTok) / 1,000,000`. Two tested examples at the default Haiku 4.5 rates:

- 2,500 input plus 80 output tokens: `0.0029 USD`
- 4,095 input plus 96 output tokens (the worst case; input is capped at 4,095 and output at 96 tokens): `0.004575 USD`

`PlayerbotClaude.DailyBudgetUsd = 5` is an emergency ceiling for all Claude features together. A measured day with about 7,700 input tokens and 610 output tokens costs approximately `0.01075 USD` at the configured rates, so the default is roughly 465 times observed usage and is not a spending target.

The configured value is the only ceiling. No policy maximum sits above it: a large configured budget is honoured as configured. The one hard limit is physical, not policy. The ledger records money in `BIGINT UNSIGNED` columns, so a ceiling above roughly 18.4 billion USD per day is one the ledger could not enforce, and it is refused rather than quietly clamped. Missing, negative, zero, or unrecordable values disable generation, as does a price of zero. The removed `PlayerbotClaude.BudgetUsd` key is ignored.

The ceiling is per UTC calendar day, rolling over at one instant regardless of server timezone or daylight saving. Money is tracked as integer nano-USD (1 USD = 1,000,000,000), so no float rounding can accumulate into the ledger.

How one request spends:

1. It is priced at its **maximum** possible cost, the counted input tokens plus the full 96 token output allowance, rounded up. An estimate that is ever low is a ceiling that can be crossed.
2. That maximum is reserved inside a transaction holding the day's row lock, so two concurrent requests cannot both see the same remaining budget and both fit.
3. On success the reservation settles at the real cost the provider reported.
4. On a failure, what happens depends on what can be proven:
   - An authentication or rate limit refusal was rejected before generation, so the reservation is released immediately.
   - A reply that came back and was rejected for its content was still generated and billed, so it settles at the exact cost the provider reported. The adapter reads the usage before it validates the content, so that figure is always available.
   - A timeout or a provider error carries no usage and nothing can be concluded, so the reservation is left alone.
5. Anything left outstanding, including from a sidecar that died mid-request, stays charged at its maximum until a later transaction reclaims it, ten minutes after it was created. A completion arriving after that reclaim is refused rather than charged twice.

`PlayerbotClaude.HumanBudgetReserveRatio` protects a share of the ceiling for work somebody is waiting on: whispers, party lines, and social replies. Ambient World chatter and career selection are background work and are denied once the remainder reaches the reserve, while a human request may still use it. Human work is protected from background work; it is not exempt from the ceiling itself. `0` disables the protection and `1` stops background generation entirely.

If the provider ever reports a cost above what was reserved, the budget circuit breaker opens: the ceiling has already been crossed by an amount nobody authorised, the true figure is recorded, and every later request is denied until the breaker is cleared.

## Data retention and deletion

The sidecar keeps its state in the shared `acore_playerbots` database, in tables prefixed `playerbot_claude_`:

| Table | Holds |
| --- | --- |
| `playerbot_claude_profile` | The latest observed personality profile per bot |
| `playerbot_claude_conversation_turn` | The most recent 12 conversation turns per bot, trimmed on every write |
| `playerbot_claude_budget_day` | One row per UTC day: settled total and circuit breaker state |
| `playerbot_claude_budget_reservation` | One row per attempt, with its maximum and settled cost |
| `playerbot_claude_ambient_attempt` | Accepted ambient attempt timestamps for the rolling hourly gate |
| `playerbot_claude_career_decision` | The current validated career decision per bot |
| `playerbot_claude_lock` | Named serialization points, from a bounded key set |

The conversation turns are the only table holding player text. No secrets are ever stored, and nothing here records account names, IP addresses, or positions.

Sharing the Playerbots database rather than a private file means the budget survives a sidecar restart, holds across two sidecars pointed at one realm, and is backed up by whatever already backs up that database.

To delete all retained data, stop the sidecar and drop those tables; the sidecar recreates them empty on next start. To delete only player text, `TRUNCATE TABLE playerbot_claude_conversation_turn`. Anthropic's own data handling is governed by their API terms.

Nothing migrates from a pre-existing `playerbot_claude.sqlite`. The old file can be deleted once the sidecar starts successfully against MySQL.

## Development

```bash
cd sidecar
uv sync --locked --dev
uv run pytest -q                # unit + socket integration tests, fully offline
bash scripts/run_ledger_mysql_tests.sh   # ledger + state against a real MySQL 8
uv run ruff format --check . && uv run ruff check .
uv run basedpyright src tests
```

`run_ledger_mysql_tests.sh` starts a throwaway MySQL container, runs the `mysql` marked
tests against it, and removes it afterwards. It never touches a MySQL server that is
already running. Those tests cannot be usefully mocked: what they prove is that a real
`SELECT ... FOR UPDATE` actually serializes two concurrent transactions, and a mock that
serializes them is a mock that assumes the answer. A bare `pytest` excludes them, so it
stays honest about what it verified.

C++ unit tests build inside the AzerothCore tree with `-DBUILD_TESTING=ON`; see `docs/architecture.md` for the protocol and trust boundary details.
