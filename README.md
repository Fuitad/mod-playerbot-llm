# mod-playerbot-claude

A private AzerothCore module that gives [mod-playerbots](https://github.com/Fuitad/mod-playerbots) bots short, in-character Claude Haiku dialogue. The language model only ever produces chat text. It has no tools, no game state access, and no influence over any bot decision. Every failure path ends in silence, never in fabricated text.

## Compatibility

This module is developed against one exact revision of the private `Fuitad/mod-playerbots` repository. That revision is recorded in [`PLAYERBOTS_REVISION`](PLAYERBOTS_REVISION) and is the only supported combination.

Verify before building:

```bash
test "$(cat modules/mod-playerbot-claude/PLAYERBOTS_REVISION)" = "$(git -C modules/mod-playerbots rev-parse HEAD)"
```

The module consumes the versioned personality contract (`PLAYERBOT_PERSONALITY_API_VERSION == 1`) from mod-playerbots. Configuration fails with a clear error when mod-playerbots is missing, and compilation fails when the personality API version changes.

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
| `PlayerbotClaude.AmbientMaxMessagesPerHour` | `6` | Persistent rolling hourly limit. Values above `6` disable ambient chatter. |
| `PlayerbotClaude.BridgePort` | `0` | Loopback TCP port shared by worldserver and sidecar. `0` disables both. |
| `PlayerbotClaude.DailyBudgetUsd` | `5` | Rolling 24 hour circuit breaker shared by all Claude generation. |
| `PlayerbotClaude.InputUsdPerMTok` | `1.00` | Price per million input tokens (Claude Haiku 4.5). |
| `PlayerbotClaude.OutputUsdPerMTok` | `5.00` | Price per million output tokens (Claude Haiku 4.5). |
| `PlayerbotClaude.SidecarDatabase` | `playerbot_claude.sqlite` | SQLite path used by the sidecar. |
| `PlayerbotClaude.ResponseDeadlineMs` | `10000` | How long a pending conversation waits before expiring silently. |
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
uv run playerbot-claude serve --config /path/to/mod_playerbot_claude.conf
```

Health check (never prints a secret, exits nonzero when misconfigured):

```bash
uv run playerbot-claude doctor --config /path/to/mod_playerbot_claude.conf
```

The report includes rolling spent, reserved, remaining, and next expiry values. It never includes prompts, database paths, or secrets.

Inspect the observed personality profile of a bot that has spoken through the bridge:

```bash
uv run playerbot-claude profile --config /path/to/mod_playerbot_claude.conf --bot-guid 42
```

## Talking to bots

- **Whisper** a bot naturally: any whisper that mod-playerbots does not recognize as a bot command becomes Claude conversation. Known commands (`follow`, `grind`, item links, and every other chat trigger) still execute as commands and cost no tokens. Prefixing with `llm ` forces Claude even for command-shaped text (`llm follow` asks the bot about following instead of ordering it to).
- **Party chat**: `llm <bot-name> <message>` addresses one bot in your group. Party chat keeps the explicit prefix so ordinary group conversation never reaches the API.
- **Milestones**: after a quest completion, a level gain, or a rare or epic loot drop, one deterministically selected group bot may react, at most once per `GroupCooldownSeconds` per group.
- **Ambient World chatter**: set `PlayerbotClaude.AmbientWorldEnable = 1` and set `AiPlayerbot.EnableBroadcasts = 0` in `playerbots.conf`. The module refuses ambient mode while canned broadcasts remain enabled, but direct Claude conversations continue working. Ambient attempts occur only while a human player is connected, use an eligible online bot that is alive, outside combat, and able to speak in World, and are limited to six accepted attempts in any rolling hour. Delivery checks the human and bot again before speech.

Replies are one short line (at most 240 bytes), in the bot's fixed voice. If anything fails (budget, timeout, provider error, invalid output), the bot simply stays silent.

## Personality behavior

Bots reuse the deterministic personality profiles defined by mod-playerbots (see its `docs/personality.md`): crafting affinity, exploration affinity, sociability (each 0 to 100), and one of five voices, all derived from the bot GUID under a versioned contract. The same bot always has the same personality, and the dialogue personality always matches the bot's in-game profession and travel preferences, because both come from the same profile.

## What is sent to the cloud

For a direct conversation or milestone, the sidecar sends the following to the Anthropic API:

- The bot's name and its personality numbers and voice (derived data, not player data)
- The channel kind (whisper or party) and the milestone kind, when applicable
- The name of the speaking player character
- The player's `llm` message text, marked as untrusted
- Up to 20 prior turns of that bot's stored conversation memory

An ambient request sends only the selected bot's name, personality numbers, voice, and a fixed instruction to offer one brief World observation. It sends no human identity, human text, or conversation history. Ambient input and output are never appended to conversation memory.

Never sent: account names, GUIDs, IP addresses, positions, inventories, combat state, the bridge token, recognized bot commands, or any party/group chat without the `llm ` prefix. Whispers are already one-to-one text addressed to a specific bot; every whisper the command system does not consume is treated as conversation with that bot and leaves the machine. If you want the stricter opt-in-per-message behavior back, whisper traffic is exactly the `llm `-prefixed subset.

## Budgeting

Cost per reply is `(input_tokens * InputUsdPerMTok + output_tokens * OutputUsdPerMTok) / 1,000,000`. Two tested examples at the default Haiku 4.5 rates:

- 2,500 input plus 80 output tokens: `0.0029 USD`
- 4,095 input plus 96 output tokens (the worst case; input is capped at 4,095 and output at 96 tokens): `0.004575 USD`

`PlayerbotClaude.DailyBudgetUsd = 5` is an emergency circuit breaker for all Claude features together. A measured day with about 7,700 input tokens and 610 output tokens costs approximately `0.01075 USD` at the configured rates. The circuit breaker is roughly 465 times that observed usage, so it is not a spending target.

The limit uses a rolling 24 hour window attributed to reservation creation time. Every request reserves its maximum possible cost before the provider is called, and successful settlement replaces that maximum with actual usage. Unsettled reservations keep their maximum charge. Both settled and unsettled commitment leave the active window exactly 24 hours after reservation, while their rows remain available for later reporting. Missing, negative, zero, or above `5` values disable generation. The removed `PlayerbotClaude.BudgetUsd` key is ignored.

## Data retention and deletion

The sidecar's SQLite database (`PlayerbotClaude.SidecarDatabase`) holds:

- The latest observed personality profile per bot
- The most recent 20 conversation turns per bot (older turns are deleted automatically)
- The budget ledger: reservations and an append-only usage log with price snapshots
- Accepted ambient attempt timestamps used by the rolling hourly gate

No secrets are ever stored. To delete all retained data, stop the sidecar and delete the SQLite file (including its `-wal` and `-shm` companions). Anthropic's own data handling is governed by their API terms.

## Development

```bash
cd sidecar
uv sync --locked --dev
uv run pytest -q                # unit + socket integration tests, fully offline
uv run ruff format --check . && uv run ruff check .
uv run basedpyright src tests
```

C++ unit tests build inside the AzerothCore tree with `-DBUILD_TESTING=ON`; see `docs/architecture.md` for the protocol and trust boundary details.
