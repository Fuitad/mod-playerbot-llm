# Playerbot Personality and Claude Chat Module Implementation Plan

Created: 2026-07-29
Author: magitekrr@gmail.com
Agent: Codex
Status: COMPLETE
Approved: Yes
Iterations: 4
Worktree: No
Type: Feature

## Summary

**Goal:** Create a private `mod-playerbots` repository with stable deterministic personality preferences, plus a separate private Claude Haiku module that uses those same personalities for contextual dialogue without giving the model control over gameplay.

## Out of Scope

- Claude controlled combat, movement, spell casting, crafting, gathering, trading, inventory changes, quest choices, or travel targets.
- A new autonomous crafting loop. Existing playerbot crafting remains an explicit command and trade workflow.
- Global action relevance multipliers. Personality affects only initial profession selection, the existing noncombat exploration chance, and which eligible bot voices a social reaction.
- Rerolling professions already stored for existing bots. Personality profession bias applies when the existing factory first chooses or repairs an invalid profession assignment.
- Guild, raid, battleground, world, channel, proximity, and unprompted idle chatter.
- Retrieval augmented generation, external lore databases, web access, voice, and image generation.
- AzerothCore core changes. All C++ changes live in the two module repositories.
- Copying code from `mod-ollama-chat` or `mod-llm-chatter`. They remain design references only.
- Publishing or pushing either private repository. Both remotes remain empty until Pierre gives explicit push permission.

## Approach

**Chosen:** Versioned deterministic personality contract in a private playerbot repository, consumed by a standalone loopback Claude module.

**Why:** `Fuitad/mod-playerbots` owns a small pure `PlayerbotPersonality` API derived from stable character identity and applies its bounded deterministic preferences. `Fuitad/mod-playerbot-claude` copies the profile into immutable chat events, calls Claude through a loopback sidecar, and returns text to the world thread. This produces consistent behavior and dialogue without spending Claude credits on gameplay planning or allowing a model response to execute an action.

## Context for Implementer

The local `modules/mod-playerbots` branch contains commit `15fca0a76ea8bb86312eb4680257ef70212e4662`, which is one commit ahead of public `origin/master`. It becomes the source of the private `Fuitad/mod-playerbots` repository. The existing public remote is renamed `upstream`; the new private remote becomes `origin`.

Personality version 1 contains `craftingAffinity`, `explorationAffinity`, `sociability`, and one voice selected from `reserved`, `pragmatic`, `earnest`, `wry`, or `boisterous`. Scores are integers from 0 through 100. The authoritative identity input is the numeric value of `ObjectGuid::GetCounter()`, zero extended to `uint64`. Version 1 derives each field with the explicitly specified SplitMix64 arithmetic in Task 1. The same GUID counter and version must always produce the same literal profile on every platform.

`mod-playerbots` already consumes player whispers and party messages as commands through `PlayerbotAI::HandleCommand`. The Claude module recognizes only `llm <message>` in a direct whisper and `llm <bot-name> <message>` in party chat. At the pinned revision, unknown commands are silently ignored, but implementation still has a behavior gate before enabling the hooks.

## Runtime Environment

- **Sidecar start:** From `modules/mod-playerbot-claude/sidecar`, run `uv run playerbot-claude serve --config ../conf/mod_playerbot_claude.conf`.
- **Port:** `PlayerbotClaude.BridgePort` is required. Its distributed default is `0`, which disables both processes. The sidecar binds only to `127.0.0.1`.
- **Health check:** Run `uv run playerbot-claude doctor --config ../conf/mod_playerbot_claude.conf --json`.
- **Profile inspection:** Run `uv run playerbot-claude profile --config ../conf/mod_playerbot_claude.conf --bot-guid 1 --json`, replacing `1` with the numeric bot GUID reported by the server.
- **Restart:** Send `SIGTERM`, wait for the sidecar to exit, then rerun the start command.
- **Claude credentials:** `ANTHROPIC_API_KEY` is read from the environment by the Anthropic SDK and is never stored.
- **Bridge authentication:** `PLAYERBOT_CLAUDE_BRIDGE_TOKEN` is read from the environment by both worldserver and the sidecar. Both fail closed when it is missing or shorter than 32 bytes. Its value never enters configuration, logs, SQLite, errors, or `doctor`.
- **Local C++ test build:** Use the isolated `build-playerbot-claude-tests` directory. The existing `build` directory remains untouched.

## Assumptions

- `Fuitad/mod-playerbots` and `Fuitad/mod-playerbot-claude` remain available and Pierre's authenticated `gh` account can create them privately. This was verified before planning.
- The existing playerbot profession storage keys `firstSkill`, `secondSkill`, and `professionRollType` remain authoritative. Task 2 preserves valid stored choices rather than rerolling them.
- Pierre will choose an unused loopback port and provide credentials only during deployment. Automated tests use operating system assigned ports and fake Anthropic responses.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Personality changes combat reliability | Low | High | Personality code has no combat action names or combat strategy hooks. Tests assert the only behavior adapters are profession weighting, exploration chance, and social responder weighting. |
| Hash or random behavior changes personalities across platforms | Medium | High | Use an explicit fixed width hash algorithm, version it, and test literal profiles for fixed GUID fixtures rather than recomputing expected values in tests. |
| Private playerbot and Claude module revisions drift | Medium | High | Commit playerbot first, record its exact SHA in the Claude module's `PLAYERBOTS_REVISION`, and make local verification check that exact revision. Future remote CI uses a read only deploy key and cannot claim success until both commits are pushed. |
| A live game object crosses into the slow bridge thread | Medium | High | Store GUIDs and immutable values only. Resolve and revalidate all objects on the world thread immediately before delivery. |
| A slow or unavailable sidecar stalls worldserver | Medium | High | Use bounded queues and a dedicated `std::jthread` with socket deadlines. Game hooks enqueue and never wait for network or disk I/O. |
| A crash loses billed usage and permits budget overrun | Medium | High | Commit a maximum cost reservation before provider submission. Unsettled reservations remain charged after restart until independently reconciled. |
| The `llm` syntax activates an existing playerbot command | Low | High | Test exact whisper and party forms against the committed private playerbot revision. Stop for a new design decision if either form emits a command response or changes AI state. |
| Operators do not realize chat leaves the server | Medium | Medium | Keep everything disabled by default and document every field sent to Anthropic before enablement steps. |

## File Structure

### Private playerbot repository

- `modules/mod-playerbots/mod-playerbots.cmake` (create): Register private module tests with AzerothCore.
- `modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.h` (create): Public versioned profile contract and pure preference helpers.
- `modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.cpp` (create): Stable profile derivation and bounded preference calculations.
- `modules/mod-playerbots/tests/PlayerbotPersonalityTest.cpp` (create): Literal fixture, bounds, profession weight, and exploration chance tests.
- `modules/mod-playerbots/src/Bot/Factory/PlayerbotFactory.h` (modify): Accept personality when calculating profession weights.
- `modules/mod-playerbots/src/Bot/Factory/PlayerbotFactory.cpp` (modify): Apply crafting affinity without rerolling valid stored professions.
- `modules/mod-playerbots/src/Ai/Base/Actions/ChooseTravelTargetAction.cpp` (modify): Replace the fixed exploration roll with the bounded profile chance.
- `modules/mod-playerbots/README.md` (modify): Describe deterministic personalities and the limits of behavior influence.
- `modules/mod-playerbots/docs/personality.md` (create): Contract, formulas, stability, and extension rules.
- `modules/mod-playerbots/docs/plans/2026-07-29-claude-playerbot-chat-module.md` (create): Approved coordinated plan.

### Standalone Claude module repository

- `modules/mod-playerbot-claude/.editorconfig` (create): Match AzerothCore formatting.
- `modules/mod-playerbot-claude/.gitattributes` (create): Normalize text files to LF.
- `modules/mod-playerbot-claude/.gitignore` (create): Exclude secrets, local configuration, caches, SQLite, and build artifacts.
- `modules/mod-playerbot-claude/.github/workflows/ci.yml` (create): Verify against the recorded private playerbot revision.
- `modules/mod-playerbot-claude/PLAYERBOTS_REVISION` (create): Exact compatible private playerbot commit SHA.
- `modules/mod-playerbot-claude/README.md` (create): Installation, privacy, configuration, budget, operation, and chat syntax.
- `modules/mod-playerbot-claude/docs/architecture.md` (create): Threads, protocol, trust boundaries, persistence, and failures.
- `modules/mod-playerbot-claude/docs/plans/2026-07-29-claude-playerbot-chat-module.md` (create): Approved coordinated plan.
- `modules/mod-playerbot-claude/conf/mod_playerbot_claude.conf.dist` (create): Disabled safe defaults and runtime limits.
- `modules/mod-playerbot-claude/mod-playerbot-claude.cmake` (create): Require playerbots and register module tests.
- `modules/mod-playerbot-claude/src/mod_playerbot_claude_loader.cpp` (create): Register module scripts.
- `modules/mod-playerbot-claude/src/ClaudeChat.h` (create): Immutable types, protocol, queues, bridge, and policy interfaces.
- `modules/mod-playerbot-claude/src/ClaudeChat.cpp` (create): Framing, validation, queues, deadlines, reconnects, and worker lifecycle.
- `modules/mod-playerbot-claude/src/ClaudeChatScripts.cpp` (create): Hooks, personality snapshots, responder selection, and world thread delivery.
- `modules/mod-playerbot-claude/tests/ClaudeChatTest.cpp` (create): Protocol, bounds, selection, expiry, and policy tests.
- `modules/mod-playerbot-claude/sidecar/pyproject.toml` (create): Python 3.12 package and console entry point.
- `modules/mod-playerbot-claude/sidecar/uv.lock` (create): Reproducible dependencies.
- `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/__init__.py` (create): Package metadata.
- `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/protocol.py` (create): Strict async framing and models.
- `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/app.py` (create): Loopback server, configuration, lifecycle, `doctor`, and profile inspection.
- `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/claude.py` (create): Haiku adapter, prompt, token preflight, response validation, and usage.
- `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/storage.py` (create): Observed profiles, bounded memory, reservations, and usage ledger.
- `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py` (create): Offline protocol, SDK, validation, storage, and budget tests.
- `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_integration.py` (create): Offline socket workflow with a fake provider.

## E2E Test Scenarios

These runtime scenarios use the WoW 3.3.5a client rather than browser automation because the user interface is the game client.

### TS-001: Stable personality across restarts
**Priority:** Critical
**Preconditions:** The private playerbot revision and Claude module are built, the sidecar is healthy, and one online playerbot has completed a Claude conversation.
**Mapped Tasks:** Task 1, Task 6, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run the sidecar `profile` command for the bot GUID | JSON reports profile version 1, three bounded affinities, and one allowed voice. |
| 2 | Restart worldserver and the sidecar, then run the same command | Every profile field is identical to step 1. |
| 3 | Rename no character and make no data changes, then converse again | The sidecar records the same trusted profile and Claude dialogue is conditioned on it. |

### TS-002: Personality affects only bounded noncombat choices
**Priority:** Critical
**Preconditions:** Fixed test bots expose a high crafting affinity and a high exploration affinity through the profile API.
**Mapped Tasks:** Task 2

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Initialize a new random bot with high crafting affinity and no stored profession | Crafting profession weights are higher than gathering weights before the existing random selection. |
| 2 | Load a bot with a valid stored profession pair | The pair remains unchanged regardless of personality. |
| 3 | Request a new noncombat travel target for a high exploration bot | The exploration branch uses its documented chance, while combat strategies and action relevance are unchanged. |

### TS-003: Direct personality aware conversation
**Priority:** Critical
**Preconditions:** The sidecar is healthy, budget is available, and a real player can whisper an online playerbot.
**Mapped Tasks:** Task 5, Task 6, Task 8, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Whisper `llm What do you enjoy doing?` | No playerbot command response appears and worldserver remains responsive. |
| 2 | Wait for the configured response deadline | The same bot sends one private response consistent with its trusted affinities and voice. |
| 3 | Observe the bot after the response | No movement, combat, spell, inventory, profession, or travel action occurs because of model output. |

### TS-004: Sociability selects one milestone speaker
**Priority:** High
**Preconditions:** A real player is grouped with two eligible bots whose sociability scores differ, the sidecar is healthy, and the group cooldown is clear.
**Mapped Tasks:** Task 6, Task 8

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Complete a quest while grouped | Exactly one bot is selected by deterministic weighted choice and sends one short reaction. |
| 2 | Gain a level before the group cooldown expires | No second request is created. |
| 3 | Repeat the fixed selection fixture outside the game | The same GUID set and event identifier select the same bot on every run. |

### TS-005: Fail closed without sidecar or budget
**Priority:** Critical
**Preconditions:** The C++ module is enabled and a real player can address an online playerbot.
**Mapped Tasks:** Task 5, Task 6, Task 8, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Stop the sidecar and send an `llm` whisper | Worldserver remains responsive, the request expires, and no fallback response is fabricated. |
| 2 | Restart with exhausted budget and send another request | The sidecar makes no generation call and the bot remains silent. |
| 3 | Run `doctor --json` | JSON reports spent, reserved, and remaining budget without secrets. |

## Progress Tracking

- [x] Task 1: Create the private playerbot repository and stable personality contract.
- [x] Task 2: Apply bounded deterministic profession and exploration preferences.
- [x] Task 3: Document, verify, and commit the private playerbot revision.
- [x] Task 4: Create the private standalone Claude module shell.
- [x] Task 5: Implement the bounded C++ bridge.
- [x] Task 6: Capture personality aware events and deliver on the world thread.
- [x] Task 7: Implement the Python protocol package.
- [x] Task 8: Add the loopback Claude service.
- [x] Task 9: Persist profiles, memory, and crash safe budget usage.
- [x] Task 10: Prove the socket workflow and document standalone operation.
- [x] Task 11: Add reproducible CI and commit the standalone module.

## Implementation Tasks

### Task 1: Create the private playerbot repository and personality contract

**Objective:** Convert the current local playerbot repository into Pierre's private upstream while retaining the public project as `upstream`, then add a pure versioned personality contract. Fixed GUID fixtures establish platform stable profiles before any existing behavior changes.

**Files:**

- Create: `modules/mod-playerbots/mod-playerbots.cmake`
- Create: `modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.h`
- Create: `modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.cpp`
- Create: `modules/mod-playerbots/tests/PlayerbotPersonalityTest.cpp`

**Key Decisions / Notes:**

- Run `git remote rename origin upstream`, then `gh repo create Fuitad/mod-playerbots --private --source=. --remote=origin`. Do not pass `--push`.
- Expose `PLAYERBOT_PERSONALITY_API_VERSION = 1`. The Claude module compiles only when this public constant remains `1`.
- Use the numeric `ObjectGuid::GetCounter()` value zero extended to `uint64`. All arithmetic is unsigned modulo 2 to the power of 64, so there is no byte serialization or platform endianness.
- Implement SplitMix64 exactly: add `0x9E3779B97F4A7C15`; xor with a right shift of 30 and multiply by `0xBF58476D1CE4E5B9`; xor with a right shift of 27 and multiply by `0x94D049BB133111EB`; then return the value xor its right shift of 31. Apply modulo 2 to the power of 64 after each addition and multiplication.
- Use namespace `0x5042504552534F4E`, crafting field `0x4352414654563031`, exploration field `0x4558504C4F524531`, sociability field `0x534F4349414C3031`, and voice field `0x564F494345563031`.
- Calculate `base = SplitMix64(guidCounter xor namespace)`. Each score is `SplitMix64(base xor fieldConstant) modulo 101`. The voice index is `SplitMix64(base xor voiceConstant) modulo 5`, mapped in order to `reserved`, `pragmatic`, `earnest`, `wry`, and `boisterous`.
- Return a value type containing version, three `uint8` affinities, and the voice enum. Reject no GUID because every playerbot has one.
- Write literal expected profiles without calling production derivation in the expected value: GUID `1` gives `29, 9, 34, pragmatic`; GUID `2` gives `21, 8, 26, earnest`; GUID `42` gives `65, 91, 82, earnest`; GUID `1000` gives `96, 72, 8, reserved`; GUID `4294967295` gives `23, 81, 74, pragmatic`.

**Definition of Done:**

- [ ] `gh repo view Fuitad/mod-playerbots --json nameWithOwner,isPrivate` reports the requested private repository.
- [ ] The local repository retains public `upstream`, uses private `origin`, preserves commit `15fca0a76ea8bb86312eb4680257ef70212e4662`, and has no pushed private branch.
- [ ] The same GUID and personality version produce the same literal profile across repeated calls and test runs.
- [ ] All five literal GUID fixtures match the specified scores and voice on the configured platform.
- [ ] Every score is within 0 through 100 and every voice is one of the five documented values.
- [ ] Verify: `cmake -S . -B build-playerbot-claude-tests -DCMAKE_INSTALL_PREFIX=/Users/pierre/azeroth-server -DCMAKE_BUILD_TYPE=RelWithDebInfo -DSCRIPTS=static -DMODULES=static -DBUILD_TESTING=ON && cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotPersonality*'`

### Task 2: Apply bounded profession and exploration preferences

**Objective:** Use the stable profile in two existing noncombat decisions only. Crafting affinity changes profession candidate weights when the factory already needs a choice, and exploration affinity changes the existing exploration roll without touching combat strategies or valid stored professions, as verified by TS-002.

**Files:**

- Modify: `modules/mod-playerbots/src/Bot/Factory/PlayerbotFactory.h`
- Modify: `modules/mod-playerbots/src/Bot/Factory/PlayerbotFactory.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/ChooseTravelTargetAction.cpp`
- Modify: `modules/mod-playerbots/tests/PlayerbotPersonalityTest.cpp`

**Key Decisions / Notes:**

- When both single profession pools are nonempty, choose the pool with top level weights `50 + craftingAffinity` for crafting and `150 - craftingAffinity` for gathering. Then preserve the existing weighted selection inside the chosen pool. If only one pool is nonempty, use it.
- For profession pairs, multiply each original pair weight by `50 + craftingAffinity` for two crafting skills, by `150 - craftingAffinity` for two gathering skills, and by `100` for a mixed pair. Use a sufficiently wide unsigned intermediate. A score of 50 multiplies every original pair weight by 100 and therefore preserves the original distribution.
- Use `5 + explorationAffinity / 10` as the existing explore branch percentage, producing a bounded 5 through 15 percent chance and preserving 10 percent at the neutral score.
- Extract pure weight and chance helpers into `PlayerbotPersonality`; test exact transformed weights and boundaries rather than statistical random outcomes.
- Existing `ItemUsageValue::IsItemUsefulForSkill` already retains materials used by learned professions. Do not add a second retention mechanism.

**Definition of Done:**

- [ ] Crafting score 0 yields pool weights 50 to 150, exactly 25 percent craft and 75 percent gather. Score 50 yields 100 to 100, exactly 50 percent each. Score 100 yields 150 to 50, exactly 75 percent craft and 25 percent gather.
- [ ] Pair score 0, 50, and 100 produce factors of 50, 100, and 150 for all crafting pairs, plus 150, 100, and 50 for all gathering pairs. Mixed pairs remain at 100.
- [ ] Exploration score 0, 50, and 100 produce exact chances of 5, 10, and 15 percent.
- [ ] Valid stored profession choices remain unchanged, while missing or invalid choices use personality weighted candidates.
- [ ] No combat strategy, combat action, global multiplier, inventory mutation, or crafting execution path is modified.
- [ ] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotPersonality*:*PlayerbotProfessionPreference*'`

### Task 3: Document and commit the private playerbot revision

**Objective:** Document the exact personality contract and upgrade boundaries, copy the approved coordinated plan, run the complete playerbot checks, and commit the private revision. This commit provides the immutable SHA consumed by the standalone Claude module.

**Files:**

- Modify: `modules/mod-playerbots/README.md`
- Create: `modules/mod-playerbots/docs/personality.md`
- Create: `modules/mod-playerbots/docs/plans/2026-07-29-claude-playerbot-chat-module.md`

**Key Decisions / Notes:**

- Explain that affinity changes probabilities, never guarantees an activity, and does not make crafting autonomous.
- State that changing hash inputs, formulas, or voice ordering requires a new personality version and migration decision.
- Run the `$commit` skill in `modules/mod-playerbots`, include the plan with the code, and record `git rev-parse HEAD` for Task 10. Do not push.

**Definition of Done:**

- [ ] Documentation names every field, formula, affected decision, unaffected system, and versioning rule.
- [ ] The full configured CTest unit target passes with both modules present or the environment blocker is reported with exact output.
- [ ] The committed private playerbot revision contains the existing telemetry commit plus personality changes and the approved plan.
- [ ] Verify: `ctest --test-dir build-playerbot-claude-tests --output-on-failure -R '^unit$' && git -C modules/mod-playerbots status --short && git -C modules/mod-playerbots rev-parse HEAD`

### Task 4: Create the private standalone Claude module shell

**Objective:** Initialize `modules/mod-playerbot-claude` as a nested repository, create `Fuitad/mod-playerbot-claude` privately without pushing, and add the minimum disabled AzerothCore module shell. Configuration fails clearly when the compatible private playerbot module is absent.

**Files:**

- Create: `modules/mod-playerbot-claude/.gitignore`
- Create: `modules/mod-playerbot-claude/conf/mod_playerbot_claude.conf.dist`
- Create: `modules/mod-playerbot-claude/mod-playerbot-claude.cmake`
- Create: `modules/mod-playerbot-claude/src/mod_playerbot_claude_loader.cpp`

**Key Decisions / Notes:**

- Run `git init -b main`, then `gh repo create Fuitad/mod-playerbot-claude --private --source=. --remote=origin`. Do not pass `--push`.
- Follow the loader mapping from directory `mod-playerbot-claude` to `Addmod_playerbot_claudeScripts`.
- Distributed defaults are `Enable = 0`, `BridgePort = 0`, and `BudgetUsd = 0`.
- Read `PLAYERBOT_CLAUDE_BRIDGE_TOKEN` from the environment in both processes and fail closed when missing or shorter than 32 bytes.
- Include the private playerbot personality header and use `static_assert(PLAYERBOT_PERSONALITY_API_VERSION == 1)` so an incompatible personality API fails compilation even when the header still exists.

**Definition of Done:**

- [ ] GitHub reports `Fuitad/mod-playerbot-claude` as private and the local nested repository has the correct `origin`.
- [ ] The parent AzerothCore repository still ignores the standalone module contents.
- [ ] CMake discovers the loader with private playerbots present, compilation enforces personality API version 1, and configuration gives a clear compatibility error without the private playerbot module.
- [ ] No branch has been pushed and no secret or local configuration is tracked.
- [ ] Verify: `gh repo view Fuitad/mod-playerbot-claude --json nameWithOwner,isPrivate && git -C modules/mod-playerbot-claude remote get-url origin && cmake -S . -B build-playerbot-claude-tests -DCMAKE_INSTALL_PREFIX=/Users/pierre/azeroth-server -DCMAKE_BUILD_TYPE=RelWithDebInfo -DSCRIPTS=static -DMODULES=static -DBUILD_TESTING=ON`

### Task 5: Implement the bounded C++ bridge

**Objective:** Add immutable request and response types, strict length prefixed JSON framing, bounded queues, and one reconnecting bridge worker. No live game object or game API crosses into the worker, and no game hook waits for socket activity.

**Files:**

- Create: `modules/mod-playerbot-claude/src/ClaudeChat.h`
- Create: `modules/mod-playerbot-claude/src/ClaudeChat.cpp`
- Create: `modules/mod-playerbot-claude/tests/ClaudeChatTest.cpp`

**Key Decisions / Notes:**

- Write failing tests first for frame round trips, malformed lengths, invalid schemas, full queues, expiry, and shutdown.
- Use a 4 byte network order length, UTF-8 JSON, a 64 KiB hard limit, and a required schema version.
- Use Boost.Asio synchronously inside one `std::jthread`; close the socket during stop so shutdown never waits for a read deadline.
- Store GUID values, profiles, strings, numeric snapshots, and timestamps only. Never store or capture live game pointers.

**Definition of Done:**

- [ ] Valid request and response frames round trip without changing trusted routing or personality fields.
- [ ] Oversized, truncated, malformed, wrong version, and wrong token frames are rejected within the frame limit.
- [ ] A full queue rejects immediately, expired work is discarded, and the worker stops without a network wait.
- [ ] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='ClaudeChatProtocol*:*ClaudeChatQueue*:*ClaudeChatBridge*'`

### Task 6: Capture personality aware events and deliver on the world thread

**Objective:** Capture explicit conversations and milestone events with trusted personality snapshots, then drain completed replies on `WorldScript::OnUpdate`. Resolve GUIDs again and deliver text only after world, group, combat, expiry, and cooldown policy passes, as verified by TS-003 through TS-005.

**Files:**

- Create: `modules/mod-playerbot-claude/src/ClaudeChatScripts.cpp`
- Modify: `modules/mod-playerbot-claude/src/mod_playerbot_claude_loader.cpp`
- Modify: `modules/mod-playerbot-claude/tests/ClaudeChatTest.cpp`

**Key Decisions / Notes:**

- Capture `llm <message>` whispers and `llm <bot-name> <message>` party messages only. Leave the original message unchanged.
- Milestones are quest completion, level gain, and rare or epic item loot.
- Snapshot the versioned playerbot profile alongside bounded current context. The sidecar cannot supply or override affinities.
- Expose `MILESTONE_SELECTION_VERSION = 1`. An event identifier contains kind, actor GUID counter, subject ID, and occurrence. Kind is `1` for quest completion, `2` for level gain, and `3` for rare or epic loot. Subject ID is the quest ID, new level, or item entry respectively. All numeric inputs are zero extended to `uint64`.
- Assign occurrence from a world thread, per actor monotonic `uint64` counter for each eligible milestone. Restart resets the counter. An exact identifier duplicate is rejected by a bounded recent identifier set before enqueue, while a later occurrence remains a distinct event.
- Sort candidates by numeric bot GUID counter. Give each candidate weight `1 + sociability`. Derive the roll with the Task 1 SplitMix64 function and namespace `0x4D494C4553544F4E`: start with `SplitMix64(actorGuid xor namespace xor MILESTONE_SELECTION_VERSION)`, then apply `SplitMix64(seed xor value)` in order for kind, subject ID, occurrence, and each sorted candidate GUID. The roll is the final seed modulo total weight. Select the first cumulative weight strictly greater than the roll.
- Test literal fixtures without calculating expected selection through the production helper: quest actor `9001`, subject `12345`, occurrence `0`, candidates `(10, 0)` and `(20, 100)` selects `20`; level actor `9001`, subject `80`, occurrence `1` with the same candidates selects `20`; loot actor `9001`, subject `19019`, occurrence `2`, candidates `(30, 34)`, `(10, 91)`, and `(20, 8)` selects `10`; quest actor `42`, subject `7`, occurrence `0`, candidates `(10, 50)`, `(20, 50)`, and `(30, 50)` selects `30`.
- Before enabling hooks, prove both `llm` forms are inert in the committed private playerbot command handler. Stop if either triggers an action or response.

**Definition of Done:**

- [ ] Only explicit `llm` syntax and the three milestones create requests.
- [ ] Named party chat targets exactly one bot, and fixed milestone fixtures select the same eligible bot on every run.
- [ ] Reordering the same candidates does not change selection, all candidates retain a nonzero chance, and an exact event identifier duplicate cannot enqueue twice.
- [ ] Stale or invalid delivery state produces no chat packet.
- [ ] Valid text is delivered once through the original trusted whisper or party channel on the world thread.
- [ ] No model response can invoke a playerbot action, and both `llm` forms remain inert to deterministic command handling.
- [ ] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='ClaudeChatPolicy*:*ClaudeChatSelection*'`

### Task 7: Implement the Python protocol package

**Objective:** Create a Python 3.12 package with strict request and response models plus safe async frame I/O matching the C++ contract. Protocol tests are fully offline.

**Files:**

- Create: `modules/mod-playerbot-claude/sidecar/pyproject.toml`
- Create: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/__init__.py`
- Create: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/protocol.py`
- Create: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py`

**Key Decisions / Notes:**

- Use standard library `asyncio`, `json`, and `struct`, plus Pydantic. Do not add a web framework.
- Mirror the 4 byte network order frame, 64 KiB limit, schema version, trusted profile, and bounded text rules.
- Compare the bridge token with `hmac.compare_digest` and exclude it from models passed to Claude.
- Cover fragmented reads, consecutive frames, oversized lengths, invalid UTF-8, extra fields, missing fields, invalid GUIDs, profile bounds, and event kinds.

**Definition of Done:**

- [ ] Python accepts every valid C++ fixture and emits responses accepted by C++.
- [ ] Fragmented reads are reassembled, while malformed input closes only its connection.
- [ ] Invalid tokens and profiles are rejected without leaking expected values.
- [ ] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest tests/test_sidecar_unit.py -q`

### Task 8: Add the loopback Claude service

**Objective:** Add the loopback server, serialized worker, graceful shutdown, health and profile commands, and a Claude Haiku adapter that returns one validated chat line. Every automated test remains offline.

**Files:**

- Create: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/app.py`
- Create: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/claude.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py`
- Create: `modules/mod-playerbot-claude/sidecar/uv.lock`

**Key Decisions / Notes:**

- Pin `anthropic==0.120.2`, verified from PyPI on 2026-07-29. Contract tests exercise `messages.parse`, `messages.count_tokens`, structured response, and all metered usage fields through a mock HTTP transport.
- Use model `claude-haiku-4-5-20251001`. The structured output contains only `message`; routing and personality stay trusted.
- Include affinity values and voice in the stable system prompt. Player text remains a separate explicitly untrusted field and the model receives no tools.
- Limit output to 96 tokens and one line of at most 240 UTF-8 bytes.
- Do not enable prompt caching. Haiku 4.5 requires a 4,096 token cacheable prefix, while requests are capped at 4,095 counted input tokens.

**Definition of Done:**

- [ ] A valid request produces one schema validated response conditioned on the trusted personality.
- [ ] Timeouts, authentication failures, rate limits, malformed output, and shutdown return bounded errors without leaked tasks.
- [ ] `doctor` and `profile` return JSON without a generation call or secret.
- [ ] Tests make no real HTTP requests and fail if the pinned SDK contract changes.
- [ ] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest tests/test_sidecar_unit.py -q`

### Task 9: Persist profiles, memory, and crash safe budget usage

**Objective:** Store observed trusted profiles, bounded recent conversation, durable reservations, and actual token usage in SQLite. Preserve state across restarts and prevent concurrent or crash interrupted requests from exceeding the configured ceiling, as verified by TS-001 and TS-005.

**Files:**

- Create: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/storage.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/claude.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/app.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py`

**Key Decisions / Notes:**

- SQLite stores the latest trusted profile, 20 conversation turns per bot, durable reservations, and append only usage rows.
- Require positive budget and rates. Haiku 4.5 distributed rates are `1.00 USD` per million input tokens and `5.00 USD` per million output tokens.
- Count the exact prompt, reject input above 4,095 tokens, and commit a reservation for counted input plus 96 output tokens before provider submission.
- Mark submitted before the provider call and atomically settle actual usage after success. Unsettled reservations remain charged at maximum after restart.
- Serialize count, reserve, generation, and settlement so socket concurrency cannot race budget.

**Definition of Done:**

- [x] Restart preserves profiles, bounded conversation, actual spend, and unresolved reservations.
- [x] A request that cannot fit performs no generation call.
- [x] Cost matches stored usage and price snapshots exactly.
- [x] Crash tests cover failure before reservation, after reservation, after submission, and before settlement.
- [x] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest tests/test_sidecar_unit.py -q`

### Task 10: Prove the socket workflow and document operation

**Objective:** Add a real loopback integration test with a fake provider, document deployment and trust boundaries, and pin the exact committed private playerbot SHA. This proves the cross process behavior and gives operators complete setup and recovery instructions before repository finalization.

**Files:**

- Create: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_integration.py`
- Create: `modules/mod-playerbot-claude/PLAYERBOTS_REVISION`
- Create: `modules/mod-playerbot-claude/README.md`
- Create: `modules/mod-playerbot-claude/docs/architecture.md`

**Key Decisions / Notes:**

- `PLAYERBOTS_REVISION` contains only the output of `git -C modules/mod-playerbots rev-parse HEAD`.
- The integration server uses an operating system assigned loopback port and injected fake adapter. Do not add a production fake provider.
- Document the formula `(input_tokens * 1.00 + output_tokens * 5.00) / 1,000,000` and tested examples: 2,500 plus 80 tokens costs `0.0029 USD`; 4,095 plus 96 costs `0.004575 USD`.

**Definition of Done:**

- [x] Socket integration covers authentication, success, oversized input, exhausted budget, reconnect, and graceful shutdown.
- [x] README documents install, compatibility, personality behavior, cloud fields, `52.95 USD` budgeting, operation, retention, and deletion.
- [x] The recorded SHA matches the committed private playerbot repository and is used in every compatibility instruction.
- [x] Unit tests reproduce every documented cost example from literal token counts and rates.
- [x] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest tests/test_sidecar_integration.py -q && test "$(cat ../PLAYERBOTS_REVISION)" = "$(git -C ../../mod-playerbots rev-parse HEAD)"`

### Task 11: Add reproducible CI and commit the standalone module

**Objective:** Add repository formatting rules and CI that checks out the exact private playerbot revision, copy the approved plan, and run complete verification. Commit the standalone repository only after all planned artifacts and checks are accounted for.

**Files:**

- Create: `modules/mod-playerbot-claude/.editorconfig`
- Create: `modules/mod-playerbot-claude/.gitattributes`
- Create: `modules/mod-playerbot-claude/.github/workflows/ci.yml`
- Create: `modules/mod-playerbot-claude/docs/plans/2026-07-29-claude-playerbot-chat-module.md`

**Key Decisions / Notes:**

- Local verification uses the adjacent committed `modules/mod-playerbots` repository and requires its HEAD to equal `PLAYERBOTS_REVISION`.
- Future remote CI reads `PLAYERBOTS_REVISION`, checks out `Fuitad/mod-playerbots` at that exact SHA, and fails before configure when the file is malformed or does not resolve.
- Future remote CI uses a read only deploy key installed on `Fuitad/mod-playerbots`; its private key is stored only as the `PLAYERBOTS_DEPLOY_KEY` Actions secret in `Fuitad/mod-playerbot-claude`. The workflow checks that the secret is present without printing it and passes it to the private checkout.
- Do not create the deploy key, configure the Actions secret, push either commit, or claim a remote CI result in this spec. Those outward actions wait for explicit push authorization. Until then, the workflow is present but recorded as `UNEXECUTED_NO_PUSH`.
- CI runs the full CTest unit target, sidecar tests, Ruff formatting and linting, and basedpyright.
- Run the `$commit` skill in the standalone repository and include every planned file plus the approved plan. Do not push.

**Definition of Done:**

- [x] Local verification uses the exact SHA recorded in `PLAYERBOTS_REVISION`; the future workflow pins the same SHA and declares the read only `PLAYERBOTS_DEPLOY_KEY` contract.
- [x] C++ unit tests, full CTest, Python tests, Ruff, basedpyright, and diff checks pass or exact environmental blockers are reported.
- [x] Both nested repositories include the approved plan, contain no secrets or runtime state, and are committed but not pushed.
- [x] The verification report labels remote CI `UNEXECUTED_NO_PUSH` and does not imply that an unpushed private SHA resolved on GitHub.
- [x] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && ctest --test-dir build-playerbot-claude-tests --output-on-failure -R '^unit$' && cd modules/mod-playerbot-claude/sidecar && uv sync --locked --dev && uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run basedpyright src tests && cd .. && git diff --check`

## Deviations

- **2026-07-29, Task 6:** The `llm` inertness gate passed with static evidence against the committed private playerbot revision `f7c31cb203747096edcb10a12ad03dfc353ebeb9`: `git grep '"llm' HEAD -- src/` returns zero matches, so no trigger, action, or strategy name beginning with `llm` exists, and `ExternalEventHelper::ParseChatCommand` (the only consumer of unknown chat text) fails every prefix lookup and drops the text silently (the "Unknown command" whisper reply is commented out at the pinned revision). Also: the pure milestone-selection, dedup, parsing, and policy helpers were placed in the existing `ClaudeChat.h/.cpp` (protocol and logic layer) rather than `ClaudeChatScripts.cpp`, so they stay unit-testable; the scripts file holds only world-thread glue.
- **2026-07-29, Task 1:** `BUILD_TESTING=ON` was broken at the pinned parent revision before any plan change: `src/test/mocks/WorldMock.h` in the playerbots AzerothCore fork was missing overrides for `IWorld::AddQueryHolderCallback` and the `MOD_PLAYERBOTS`-gated `IWorld::GetPlayerbotsDBRevision`, so every core test instantiating `NiceMock<WorldMock>` failed to compile. Fixed inline by adding the two missing `MOCK_METHOD` entries (test-only, parent repository working tree; not part of either module commit). The plan's "AzerothCore core changes" exclusion targets runtime code; without this repair no C++ test in this plan can build.

- **2026-07-29, Task 9 (user-directed):** Pierre asked mid-run for the sidecar to read its Anthropic API key from `MOD_PLAYERBOT_CLAUDE_APIKEY` instead of the global `ANTHROPIC_API_KEY`, so a machine-wide key can never be used implicitly by this module. This supersedes the "Claude credentials" line in Constraints. `ClaudeAdapter` now passes `api_key` explicitly (an unset variable yields an empty key that fails authentication rather than falling back to the SDK's `ANTHROPIC_API_KEY` lookup), `serve` refuses to start when the variable is missing, and `doctor` reports presence of the module-scoped variable only. Covered by `test_adapter_ignores_global_anthropic_api_key` and `test_doctor_ignores_global_anthropic_api_key`.

## Deferred Ideas

- Autonomous crafting, gathering, trading, and travel lifestyles built deterministically on the versioned profile.
- Additional bounded noncombat preference adapters after each has a specific existing decision point and behavioral test.
- Prompt caching after the stable prefix reaches Haiku 4.5's 4,096 token minimum.
- Natural party name detection after explicit `llm` syntax proves conflict free.
- Idle party banter driven by measured per group rates and a separate budget allocation.
- Compact long term memory summaries after the 20 turn context proves useful.
