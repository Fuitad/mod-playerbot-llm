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
| `PlayerbotClaude.AmbientWorldEnable` | `0` | **Legacy.** Ambient World chatter, for a server running with `AiPlayerbot.SocialChat.Enable = 0`. Inert while social chat is on. |
| `PlayerbotClaude.AmbientMaxMessagesPerHour` | `6` | **Legacy.** Rolling hourly limit for the setting above. Effective range is `1` through `6`: worldserver accepts up to `60`, but the sidecar declines every attempt above `6`, so a larger value yields silence rather than more chatter. |
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

Which of the two modes below is in force is decided entirely by `AiPlayerbot.SocialChat.Enable` in `playerbots.conf`, never by a setting in this module. The two never run together: whichever one is off contributes nothing at all rather than falling back.

### With interactive social chat enabled (`AiPlayerbot.SocialChat.Enable = 1`)

mod-playerbots owns the conversation. Its coordinator watches zone General, say, party, and whispers, decides which opportunities are worth answering and who answers them, and asks this module for the line. There is no prefix and no command: bots take part in ordinary conversation, and a whisper to a bot is answered because it was addressed to that bot.

This module's own whisper, party, and ambient hooks stand down completely in this mode, so one message can never produce two unrelated answers chosen by two different rules. `PlayerbotClaude.AmbientWorldEnable` is reported as ignored in the log rather than silently skipped, and no bot chatter reaches the World channel from either module.

See the interactive social chat section of the mod-playerbots README for the surfaces, the opt out commands, retention, and moderation.

### With interactive social chat disabled (`AiPlayerbot.SocialChat.Enable = 0`)

Legacy compatibility only. This is what a realm that has not turned interactive social chat on still gets, described so an operator upgrading from it knows what changes. It is not the intended configuration and nothing below is part of Social.

- **Whispers** reach Claude the same way they always did: a whisper mod-playerbots does not recognize as a bot command becomes conversation, while known commands (`follow`, `grind`, item links, and every other chat trigger) still execute as commands and cost no tokens.
- **Party chat** reaches Claude only when a message is explicitly marked for it, so ordinary group conversation never leaves the machine. Interactive social chat replaces that boundary entirely: it reads party chat directly and decides for itself, and no marker exists or is needed.
- **World chatter** is the one surface Social never uses. The legacy ambient path can speak in the World channel, gated on `PlayerbotClaude.AmbientWorldEnable` and on canned broadcasts being off, bounded by a rolling hourly limit, and re-checked against the human, the bot, and channel membership immediately before speech. With interactive social chat on it is inert and the setting is logged as ignored. Zone General, say, party, and whispers are the only surfaces Social ever uses.

### Either way

- **Milestones**: after a quest completion, a level gain, or a rare or epic loot drop, one deterministically selected group bot may react, at most once per `GroupCooldownSeconds` per group.
- **Career choices**: unaffected by the social gate. They have their own request kind and their own budget priority.

Replies are one short line (at most 240 bytes), in the bot's fixed voice. If anything fails (budget, timeout, provider error, invalid output), the bot simply stays silent.

## Personality and career behavior

Bots reuse the deterministic personality profiles defined by mod-playerbots (see its `docs/personality.md`). Version 2 provides independent crafting, gathering, exploration, and sociability scores from 0 to 100, plus one of five voices.

When playerbot code needs a new career plan, this module may submit one request containing only opaque legal candidate tokens, short candidate categories, maximum spending styles, market eligibility, and engagement. The model chooses one token and a permitted style. It cannot invent a profession, recipe, spell, destination, price, or runtime action. Playerbot code validates the correlation, versions, token, and style before persistence. Disabled, unavailable, invalid, or late responses use the same deterministic fallback as a server without this module.

## What is sent to the cloud

For a direct conversation or milestone, the sidecar sends the following to the Anthropic API:

- The bot's name and its personality numbers and voice (derived data, not player data)
- The channel kind (whisper or party) and the milestone kind, when applicable
- The name of the speaking player character
- The player's message text, marked as untrusted
- Up to 20 prior turns of that bot's stored conversation memory

An ambient request sends only the selected bot's name, personality numbers, voice, and a fixed instruction to offer one brief World observation. It sends no human identity, human text, or conversation history. Ambient input and output are never appended to conversation memory.

A social request, when `AiPlayerbot.SocialChat.Enable = 1`, sends the speaking bot's name, the other character's name, which of the four channels the answer will be spoken on, an opaque thread identity, and a bounded context the coordinator assembled: the bot's persona, its relationship with that character, who is nearby, recent lines from the thread, the subject when it is opening a conversation, and the memories it is allowed to draw on. Everything in it is bounded per field and in total, and all of it is marked untrusted.

Memories are filtered by the channel's privacy scope, twice. The coordinator filters before sending and the sidecar filters again on arrival, so something a bot learned in a whisper cannot be repeated in a zone even if the producer has a bug. Identity in a social prompt is the character name the coordinator supplied, and the answer is validated against that name rather than against anything the model wrote.

After a conversation ends, a separate memory extraction request may send that finished thread and ask what is worth remembering. It is the one request that sends character GUIDs, and only as the closed list of characters a memory may be attributed to, alongside their names. That is deliberate: the model must choose one of a fixed set, and the deterministic gate afterwards refuses any value outside it, so an invented attribution cannot become a stored memory. The instructions require a paraphrase rather than a reproduced line and forbid recording any real world detail, and the gate re-checks the result rather than trusting it.

A career request sends the bot name, personality values, and opaque candidate descriptions. It sends no raw profession, skill, spell, item, or recipe identifiers and no conversation history. The returned career decision is diagnostic data only in the sidecar. The authoritative validated plan remains in mod-playerbots.

Never sent, on any path: account names, IP addresses, positions, inventories, combat state, the bridge token, or recognized bot commands. Character GUIDs are sent on exactly one path, memory extraction, for the reason given above; no other request carries one.

Whispers are already one-to-one text addressed to a specific bot; every whisper the command system does not consume is treated as conversation with that bot and leaves the machine. With interactive social chat off, party and group chat leave only when explicitly marked for Claude, so whisper traffic is the only thing that leaves unmarked. With it on, mod-playerbots decides what is sent on all four surfaces and a character can opt out of the feature entirely with `.playerbots social off`.

## Budgeting

Cost per reply is `(input_tokens * InputUsdPerMTok + output_tokens * OutputUsdPerMTok) / 1,000,000`. Two tested examples at the default Haiku 4.5 rates:

- 2,500 input plus 80 output tokens: `0.0029 USD`
- 4,095 input plus 96 output tokens (the worst case; input is capped at 4,095 and output at 96 tokens): `0.004575 USD`

`PlayerbotClaude.DailyBudgetUsd = 5` is an emergency ceiling for all Claude features together. A measured day with about 7,700 input tokens and 610 output tokens costs approximately `0.01075 USD` at the configured rates, so the default is roughly 465 times observed usage and is not a spending target.

The configured value is the only ceiling. No policy maximum sits above it: a large configured budget is honoured as configured. The one hard limit is physical, not policy. The ledger records money in `DECIMAL(12, 6)` columns, so a ceiling above `999999.999999` USD per day is one the ledger could not enforce, and it is refused rather than quietly clamped. Missing, negative, zero, or unrecordable values disable generation, as does a price of zero. The removed `PlayerbotClaude.BudgetUsd` key is ignored.

The ceiling is per UTC calendar day, rolling over at one instant regardless of server timezone or daylight saving. Arithmetic runs in integer nano-USD (1 USD = 1,000,000,000), so no float rounding can accumulate into the ledger, and amounts are stored to six decimal places. A single reservation or settlement is rounded up to the nearest millionth of a dollar, always in the direction that protects the budget.

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

If the provider ever reports a cost above what was reserved, the budget circuit breaker opens: the ceiling has already been crossed by an amount nobody authorised, the true figure is recorded, and every later request is denied until the breaker is cleared. The breaker lives on the social runtime control row rather than on the day, so it does not reopen by itself at UTC rollover. An impossible cost report is not a fact about one calendar day.

## Data retention and deletion

The sidecar keeps its state in the shared `acore_playerbots` database. It creates and owns five tables:

| Table | Holds |
| --- | --- |
| `playerbot_claude_profile` | The latest observed personality profile per bot |
| `playerbot_claude_conversation_turn` | The most recent 12 conversation turns per bot, trimmed on every write |
| `playerbot_claude_ambient_attempt` | Accepted ambient attempt timestamps for the rolling hourly gate |
| `playerbot_claude_career_decision` | The current validated career decision per bot |
| `playerbot_claude_lock` | Named serialization points, from a bounded key set |

Three more tables it writes to belong to mod-playerbots and are created by that module's SQL revisions, not by the sidecar:

| Table | Holds |
| --- | --- |
| `playerbot_claude_daily_budget` | One row per UTC day: the reserved and spent decimal totals |
| `playerbot_claude_budget_reservation` | One row per attempt, with its request kind, model, maximum, and settled cost |
| `playerbot_social_runtime_control` | The operator controls, including the budget circuit breaker |

The sidecar refuses to start when those three are missing, naming them, rather than creating its own version. Apply `modules/mod-playerbots/data/sql/playerbots/updates` to the Playerbots database first.

The conversation turns are the only table holding player text. No secrets are ever stored, and nothing here records account names, IP addresses, or positions.

Sharing the Playerbots database rather than a private file means the budget survives a sidecar restart, holds across two sidecars pointed at one realm, and is backed up by whatever already backs up that database.

To delete the data the sidecar owns, stop it and drop the five tables in the first list; it recreates them empty on next start. Dropping the three in the second list needs the module's SQL revisions reapplied before the sidecar will start again. To delete only player text, `TRUNCATE TABLE playerbot_claude_conversation_turn`. Anthropic's own data handling is governed by their API terms.

Upgrading from a version that kept its own file: nothing is migrated, and no code reads it. Delete it once the sidecar starts successfully against MySQL. A leftover `PlayerbotClaude.SidecarDatabase` line in the config is ignored.

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
