# LLM Ambient Global Chatter Implementation Plan

Created: 2026-07-29
Author: magitekrr@gmail.com
Agent: Codex
Status: COMPLETE
Approved: Yes
Iterations: 3
Worktree: Yes
Type: Feature

## Summary

**Goal:** Replace canned playerbot broadcasts with personality shaped Claude ambient messages in the World channel. Requests are enqueued only while at least one human player is online, delivery requires a human to remain online, ambient generations are limited to six per rolling hour, and all Claude generations together may commit at most 5 USD in any rolling 24 hour window.

## Out of Scope

- Generated messages in General, Trade, Looking For Group, guild, party, or whisper channels. Existing Claude whisper, party, and milestone behavior remains unchanged.
- Model access to positions, inventory, combat state, quests, auctions, tools, other bot conversations, or autonomous game actions. Ambient generation receives only the selected bot identity and deterministic personality.
- Changes to AzerothCore core or `mod-playerbots`. The existing `AiPlayerbot.EnableBroadcasts` switch disables `BroadcastHelper`.
- Building, installing, or activating a new worldserver binary. Live scenarios require separate deployment authorization after source verification.
- A spending dashboard or replacement of the current observation tooling. The existing `doctor` command receives only the minimal rolling budget fields needed for safe operation, and the ledger remains append only for a future dashboard.

## Approach

**Chosen:** A module owned ambient scheduler in `ClaudeChatScripts.cpp`, backed by a persistent ambient rate gate and one rolling all Claude budget in the existing Python sidecar.

**Why:** `mod-playerbot-claude` already owns bot personality prompts, asynchronous World thread delivery, and the crash safe budget ledger. Reusing those boundaries keeps the model isolated from gameplay and avoids changes to `mod-playerbots`, at the cost of requiring `AiPlayerbot.EnableBroadcasts = 0` alongside the new ambient enable switch.

## Context for Implementer

`BroadcastHelper` already has one master switch, `AiPlayerbot.EnableBroadcasts`. Ambient mode must refuse to start unless that switch is off. It must not mutate another module's configuration at runtime. Provider failure, invalid output, rate denial, budget denial, no eligible bot, or no online human all result in silence.

Ambient requests must never load or append the selected bot's stored conversation history. That history can contain private whispers. Reusing it for World chat would create a privacy leak even if the prompt asked the model not to repeat it.

The World thread is the only place that may inspect players or call `PlayerbotAI::SayToWorld`. The bridge worker and sidecar receive immutable values only. Delivery re-resolves the bot by GUID and rechecks that a human is online.

Human presence is authoritative when the World thread enqueues and delivers an ambient request. A provider call already accepted by the sidecar can finish after the last human disconnects because the sidecar has no live game state channel. That attempt still consumes its conservative rate slot and budget reservation, but its response is dropped before speech. Plan approval explicitly accepts this bounded race instead of adding a second bidirectional presence protocol.

The rolling budget attributes each request to its reservation creation time. Settled requests count at actual cost, while unsettled requests count at maximum reserved cost. Both age out exactly 24 hours after reservation, including conservative reservations left by provider failures. Rows remain in the append only ledger after they stop blocking new work.

Pierre's observed day of about 7,700 input and 610 output tokens costs approximately 0.01075 USD at the configured 1 USD and 5 USD per million token rates. The 5 USD rolling limit is therefore an emergency circuit breaker at roughly 465 times that observed usage, not a target.

## Runtime Environment

- **C++ tests:** The existing `build-playerbot-claude-tests` tree is present. Approval of this plan authorizes incremental compilation of its `unit_tests` target only. It does not authorize CMake reconfiguration, a full server build, installation, or deployment.
- **Python tests:** Run from `modules/mod-playerbot-claude/sidecar` with the locked `uv` environment. Tests remain fully offline through fake Anthropic clients.
- **Installed server:** `/Users/pierre/azeroth-server/bin/worldserver` and the installed module configuration files are present, but they do not contain this unbuilt change.
- **Live verification gate:** Source work may reach `COMPLETE` through C++ unit tests, Python tests, static checks, and review. It cannot reach `VERIFIED`, and TS-001 through TS-004 cannot be claimed, until Pierre separately authorizes build, installation, configuration, and a WoW client run.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Private whisper history appears in World chat | Low | High | Ambient requests use no stored history and create no conversation turns. Tests place a unique private marker in history and prove it never reaches the provider payload. |
| A restart permits more than six messages in one hour | Medium | High | The sidecar records accepted ambient attempts in SQLite and applies a rolling 3600 second window across process restarts. |
| Concurrent requests commit more than 5 USD in one rolling day | Low | High | Every whisper, party, milestone, and ambient request uses one SQLite transaction that checks rolling settled plus unsettled commitment before reservation. |
| Delivery prerequisites change while generation is pending | Medium | Medium | Human presence and World channel availability are checked before enqueue and delivery. `SayToWorld` failure is inspected and fails silent after consuming the conservative accepted attempt. |
| Ambient and canned chatter run together | Medium | Medium | Ambient startup fails closed unless `AiPlayerbot.EnableBroadcasts` is disabled, and the failure is logged without disabling existing whisper or party Claude behavior. |

## E2E Test Scenarios

These scenarios use the WoW 3.3.5a client, worldserver logs, and sidecar logs. There is no browser target.

### TS-001: One personality shaped World message reaches an online human
**Priority:** Critical
**Preconditions:** The ambient feature is enabled, `AiPlayerbot.EnableBroadcasts = 0`, the sidecar is healthy, at least one eligible random bot is online, and one human player character is in the world.
**Mapped Tasks:** Task 1, Task 2, Task 3

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Log in as the human and join the configured World channel | The human sees no canned `BroadcastHelper` lines. |
| 2 | Keep one eligible random bot alive and out of combat until the next ambient interval | The sidecar accepts one ambient request containing that bot's deterministic personality and no human identity or text. |
| 3 | Observe the World channel after the response returns | The selected bot posts one short message through `SayToWorld`, in its configured voice, with no action promise or private conversation content. |

### TS-002: No human means no generation and no spend
**Priority:** Critical
**Preconditions:** Ambient mode and the sidecar are running, eligible bots are online, and no human player character is in the world.
**Mapped Tasks:** Task 2, Task 4

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Leave the server without a human through one complete ambient interval | No ambient request reaches the sidecar, no reservation is created, and no World message is sent. |
| 2 | Log in as a human before the next interval | Ambient eligibility resumes without a catch up burst. |
| 3 | Disconnect the last human after a request is enqueued but before delivery | An already accepted provider call may finish and remains charged, but the response is dropped and no World message is sent. |

### TS-003: The rolling cap survives restart
**Priority:** Critical
**Preconditions:** A human and eligible bots remain online, the first six ambient generations in the current rolling hour have been accepted, and the sidecar database is preserved.
**Mapped Tasks:** Task 1, Task 4

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Attempt a seventh ambient generation inside the same rolling 3600 second window | The sidecar rejects it before token counting, reservation, and provider generation. |
| 2 | Restart the sidecar and attempt again before the oldest accepted attempt expires | The request is still rejected because the persisted window remains full. |
| 3 | Attempt after the oldest accepted attempt leaves the window | Exactly one new attempt is accepted. No interval contains more than six accepted attempts. |

### TS-004: The rolling daily budget governs every Claude feature
**Priority:** Critical
**Preconditions:** `PlayerbotClaude.DailyBudgetUsd` is set to 5 and rolling committed cost across ambient and direct conversations has reached 5 USD.
**Mapped Tasks:** Task 3, Task 4

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Reach the next ambient interval with a human and an eligible bot online | The ambient request is rejected before provider generation because the rolling all Claude budget is exhausted. |
| 2 | Whisper a bot and trigger an eligible party or milestone request | Every request type is rejected before provider generation by the same rolling budget gate. |
| 3 | Wait until the oldest reservation reaches 24 hours, then submit one request | Its commitment ages out, exactly one fitting request can reserve, and no rolling 24 hour window exceeds 5 USD. |
| 4 | Run the sidecar doctor command | The report shows rolling spent, reserved, remaining, and next expiry values without secrets. |

## File Structure

- `src/ClaudeChat.h` and `src/ClaudeChat.cpp`: Version 2 wire values, World delivery policy, deterministic ambient candidate selection, and local cadence.
- `src/ClaudeChatScripts.cpp`: Human gating, eligible bot snapshotting, ambient enqueue, World delivery, and legacy broadcast configuration guard.
- `sidecar/src/playerbot_claude/protocol.py` and `claude.py`: Version 2 validation and a history free ambient prompt.
- `sidecar/src/playerbot_claude/storage.py` and `app.py`: Persistent ambient rate gate, rolling all Claude commitment, and doctor reporting.
- `tests/ClaudeChatTest.cpp` and `sidecar/tests/`: Literal protocol, cadence, delivery, privacy, rate, budget, restart, and socket integration fixtures.
- `README.md`, `docs/architecture.md`, and `conf/mod_playerbot_claude.conf.dist`: Operator contract, privacy boundary, hard ceilings, and activation sequence.

## Progress Tracking

- [x] Task 1: Extend the C++ contract and deterministic ambient policy.
- [x] Task 2: Schedule and deliver human gated World chatter.
- [x] Task 3: Add the ambient protocol and privacy isolated prompt.
- [x] Task 4: Enforce the persistent ambient rate and rolling all Claude budget.
- [x] Task 5: Document activation and verify the complete change.

## Implementation Tasks

### Task 1: Extend the C++ contract and ambient policy

**Objective:** Add the value types and pure decisions required for ambient World requests without touching live game state. Establish the protocol version, deterministic personality weighted speaker selection, local cadence, and World delivery gates before wiring them into the World script.

**Files:**

- Modify: `modules/mod-playerbot-claude/src/ClaudeChat.h`
- Modify: `modules/mod-playerbot-claude/src/ClaudeChat.cpp`
- Modify: `modules/mod-playerbot-claude/tests/ClaudeChatTest.cpp`

**Key Decisions / Notes:**

- Bump the strict request and response schema to version 2. Add `ChatChannel::World` and event kind 4 for ambient chatter.
- Version 2 ambient requests carry `speaker_guid = 0`, an empty speaker name, no human message, and a fixed trusted ambient event marker. Conversation and milestone requests retain their current nonempty speaker contract.
- Add a pure deterministic speaker selector that weights each eligible bot by `1 + sociability`, sorts candidates by GUID, and derives the roll from a fixed ambient namespace plus an occurrence counter.
- Add a steady clock cadence that permits at most one enqueue slot per `3600 / configuredRate` seconds. The sidecar uses the same configured rate for its persisted rolling window. Configuration may lower the rate, but values above the hard maximum of 6 disable ambient mode.
- Extend `DeliverySnapshot` so World delivery requires a current human, an online machine bot, no combat, and an unexpired response. Whisper and party behavior must remain byte for byte compatible apart from schema version.

**Definition of Done:**

- [ ] RED tests fail because World protocol values, ambient selection, cadence, and delivery policy do not exist.
- [ ] Literal fixtures prove stable weighted selection for a fixed occurrence and candidate set, including empty candidates and reordered input.
- [ ] Cadence fixtures prove no startup burst and no more than six enqueue slots in any hour at the configured maximum.
- [ ] World delivery fixtures reject expiry, no human, offline bot, nonbot, and combat while existing whisper combat delivery remains allowed.
- [ ] C++ and Python version 2 fixtures remain byte for byte identical.
- [ ] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='ClaudeChat*'`

### Task 2: Schedule and deliver human gated World chatter

**Objective:** Extend the existing World thread state machine to enqueue one ambient request at each eligible cadence slot and deliver the response through the selected bot's World channel. Keep all player inspection and speech on the World thread, as verified by TS-001 and TS-002.

**Files:**

- Modify: `modules/mod-playerbot-claude/src/ClaudeChatScripts.cpp`
- Modify: `modules/mod-playerbot-claude/conf/mod_playerbot_claude.conf.dist`
- Modify: `modules/mod-playerbot-claude/tests/ClaudeChatTest.cpp`

**Key Decisions / Notes:**

- Add `PlayerbotClaude.AmbientWorldEnable`, default 0, and `PlayerbotClaude.AmbientMaxMessagesPerHour`, default 6. Enabled values must be from 1 through 6.
- Refuse to activate ambient scheduling when `sPlayerbotAIConfig.enableBroadcasts` is true. Log the exact required `AiPlayerbot.EnableBroadcasts = 0` correction and leave direct Claude conversations operational.
- On a due slot, first scan `ObjectAccessor::GetPlayers()` for at least one real player character. Only then scan `sRandomPlayerbotMgr.GetPlayers()` for online, alive, noncombat machine bots that can currently resolve the World channel.
- Advance the cadence after every due evaluation, including no human, no candidate, queue full, or sidecar failure. Reconnection never creates catch up messages.
- Store only the selected bot GUID for World delivery. Re-resolve the bot, repeat the human presence and World channel checks, call `GET_PLAYERBOT_AI(bot)->SayToWorld` only after `ShouldDeliver` passes, and inspect its boolean result.
- An accepted request can finish provider work after the last human disconnects. It still consumes its rate slot and budget reservation, but delivery fails silent. Do not add a sidecar game state heartbeat or cancellation protocol.

**Definition of Done:**

- [ ] C++ cadence and delivery tests prove no human causes no enqueue eligibility, a disconnect before delivery fails silent, and no catch up burst is created.
- [ ] Candidate policy fixtures exclude bots without a current World channel, and delivery result handling records a failed `SayToWorld` without fallback text.
- [ ] An eligibility policy fixture plus the fake socket bridge prove a declined World thread snapshot produces no outbound frame.
- [ ] Existing whisper, party, milestone, queue, and shutdown tests remain unchanged in behavior.
- [ ] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='ClaudeChat*'`

### Task 3: Add the ambient protocol and privacy isolated prompt

**Objective:** Teach the sidecar to validate version 2 ambient requests and generate a single personality shaped World line without any human identity, player text, or stored conversation history. Preserve the current structured output and fail silent behavior.

**Files:**

- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/protocol.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/claude.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/app.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py`

**Key Decisions / Notes:**

- Validate the ambient field combination as one cross field invariant. World plus event kind 4 requires zero speaker GUID, empty speaker name, and the fixed ambient marker. Every other channel rejects that combination.
- Build a trusted ambient system prompt from bot name and personality only. The user message requests one brief in character World observation without claiming current game facts or promising actions.
- Return no stored history for ambient requests and never append ambient input or output to `conversation_turns`.
- Keep the same model, output schema, token ceilings, timeout mapping, and one line UTF-8 validation.
- Tests place a unique private marker in existing bot history and assert it is absent from token counting and generation payloads for ambient.

**Definition of Done:**

- [ ] RED tests fail because version 2 World and ambient combinations are unknown.
- [ ] Protocol fixtures reject World requests with a human speaker, player text, wrong event kind, or any missing trusted ambient field.
- [ ] Provider payload fixtures contain the selected personality and ambient instruction but no human GUID, human name, player text, stored private marker, tools, or action access.
- [ ] Ambient success creates no conversation turns while normal whisper and milestone memory behavior remains unchanged.
- [ ] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest -q tests/test_sidecar_unit.py`

### Task 4: Enforce the persistent rate and rolling all Claude budget

**Objective:** Make the configured ambient limit, capped at six generations per rolling hour, and 5 USD of all Claude commitment per rolling 24 hours hard, crash safe limits. Apply the ambient rate check before token counting and the shared budget check after counting but before provider generation, as verified by TS-003 and TS-004.

**Files:**

- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/storage.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/app.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_integration.py`

**Key Decisions / Notes:**

- Add a persisted ambient attempt table. In one transaction, remove entries outside the rolling 3600 second window, reject when the configured number from 1 through 6 remains, or record the new accepted attempt.
- An accepted attempt consumes a rate slot even when token counting, provider generation, delivery, or settlement later fails. This conservative rule guarantees no more than six possible messages.
- Replace `PlayerbotClaude.BudgetUsd` with `PlayerbotClaude.DailyBudgetUsd`. Missing, negative, or above 5 USD values disable all generation. The distributed configuration documents 5 USD, while 0 remains the fail closed fallback for an installed configuration that has not migrated.
- Calculate rolling commitment from settled actual cost plus unsettled maximum reservations whose reservation creation time is inside the previous 24 hours. Use one transaction for every request type.
- Age commitment out at the exact 24 hour boundary without deleting reservations or usage rows. Restarts preserve both the active window and complete historical data.
- Extend doctor output with rolling spent, reserved, remaining, and the next commitment expiry. Never include prompts, database paths, API keys, or bridge tokens.

**Definition of Done:**

- [ ] RED storage tests fail because the persisted ambient rate and rolling budget queries do not exist.
- [ ] Literal timestamp fixtures accept six attempts, reject the seventh, accept exactly after the oldest leaves the window, and preserve the rejection across store reopen.
- [ ] A configured rate of one accepts one attempt, rejects the second for the full rolling hour across store reopen, and uses the same transaction as the hard maximum of six.
- [ ] Configuration fixtures cover 0, 1, and 6 ambient messages per hour plus rejection above 6, and daily budgets of 0 and 5 USD plus rejection below 0 or above 5 USD.
- [ ] Literal clock fixtures combine ambient, whisper, party, and milestone reservations, count settled actual and unsettled maximum cost once, and release each commitment exactly 24 hours after reservation.
- [ ] Store reopen and concurrent request fixtures prove no restart or race can admit a seventh ambient attempt or cross 5 USD in any rolling 24 hour window.
- [ ] Historical usage rows remain queryable after aging out of the active budget window.
- [ ] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest -q`

### Task 5: Document activation and verify the complete change

**Objective:** Update the operator, privacy, architecture, and cost documentation so the feature can be enabled without dual chatter or accidental private context reuse. Run the complete available source verification while keeping deployment outside this plan.

**Files:**

- Modify: `modules/mod-playerbot-claude/README.md`
- Modify: `modules/mod-playerbot-claude/docs/architecture.md`
- Modify: `modules/mod-playerbot-claude/conf/mod_playerbot_claude.conf.dist`
- Create: `modules/mod-playerbot-claude/docs/plans/2026-07-29-llm-ambient-global-chatter.md`

**Key Decisions / Notes:**

- Document the required pair: `PlayerbotClaude.AmbientWorldEnable = 1` and `AiPlayerbot.EnableBroadcasts = 0`.
- Document `PlayerbotClaude.DailyBudgetUsd = 5`, the rolling 24 hour semantics, conservative unsettled accounting, exact expiry boundary, removal of `PlayerbotClaude.BudgetUsd`, and doctor fields.
- Update the cloud disclosure list to state that ambient requests contain no human identity, human text, or conversation history.
- Record that live verification needs a newly built and installed worldserver plus a WoW client. Do not claim TS-001 through TS-004 from unit tests.
- Copy the approved root specification into the module repository so its code and plan are committed together.

**Definition of Done:**

- [ ] README and configuration describe identical defaults, hard maximums, fail closed conditions, and activation order.
- [ ] Architecture documents protocol version 2, World thread checks, persistent rate state, rolling all Claude budget accounting, retained history, and privacy isolation.
- [ ] All changed non Markdown files contain no decorative non ASCII characters.
- [ ] Python tests, Ruff, BasedPyright, the incremental C++ test target, and C++ codestyle complete with zero failures.
- [ ] Verify: `(cd modules/mod-playerbot-claude/sidecar && uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run basedpyright src tests) && cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='ClaudeChat*' && python apps/codestyle/codestyle-cpp.py`

## Deferred Ideas

- Replace or extend the minimal `doctor` budget fields with a full spending dashboard that queries retained usage and reservation history.

## Verification Record

- The sidecar suite passed with 71 tests. Ruff formatting, Ruff lint, and BasedPyright also passed.
- The isolated C++ harness passed all 44 `ClaudeChat*` tests, and `ClaudeChatScripts.cpp` compiled from the isolated worktree.
- The existing CMake test tree targets the other checkout, so the approved incremental target was reproduced with its compile commands against the isolated sources instead of rebuilding the other session's files.
- The repository wide C++ style command still reports existing failures in unrelated AzerothCore core files. The module diff has no whitespace errors, decorative non ASCII additions, or unresolved `SHORTCUT` markers.
- Live TS-001 through TS-004 remain gated on separate authorization to build, install, configure, and exercise the change through a WoW client.
