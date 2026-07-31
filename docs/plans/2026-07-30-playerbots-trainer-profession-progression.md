# Playerbots Trainer Profession Progression Implementation Plan

Created: 2026-07-30
Author: magitekrr@gmail.com
Agent: Codex
Status: COMPLETE
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** Make Playerbots learn professions and recipes from legitimate world sources, then advance profession skills only by using them on a newly recreated bot population.

Random bots may receive a persistent career plan derived from independent crafting and gathering affinities. No profession remains a legal weighted choice at every affinity, secondary professions are optional candidate variants, and the selected career engagement controls how strongly profession goals compete with questing. Claude may choose among server generated legal career candidates, but the server validates and executes every decision. Profession ranks and trainer recipes cost real money at physical trainers. Vendor recipes, recipe drops, and Auction House recipes enter through their existing item paths. Crafting and gathering skill points increase only through AzerothCore's normal spell and gathering updates.

The same change corrects the current Auction House convergence. The profession personality change alone cannot do that because the economy timer currently treats almost every random bot as eligible, treats generic `ITEM_USAGE_AH` loot as a reason to travel, and retries every `RandomBotUpdateInterval` after finding no operation.

## Out of Scope

- Direct skill values, recipes, items, gold, auctions, or trainer results created outside AzerothCore's normal handlers.
- Claude choosing raw spell IDs, item IDs, NPC IDs, prices, coordinates, commands, or individual runtime actions.
- A Claude call for every trainer purchase, recipe, craft, gathering node, or economy cycle.
- Replacing the existing playerbot AI, travel system, trainer handler, loot system, vendor handler, Auction House handler, or budget values.
- A new database table or changes to AzerothCore core database schemas.
- A realm wide Auction House concurrency controller. Career eligibility, actionable inventory, stable staggering, and outcome backoff are the first bounded correction.
- A full server build, installation, restart, deployment, configuration change, or push without separate current permission.

## Feature Inventory

| Existing behavior | Final disposition |
|---|---|
| `PlayerbotFactory::InitTradeSkills` assigns profession skills directly and sets them to level scaled values | Replace with career initialization that grants no profession skill or skill points |
| `PlayerbotFactory::InitAvailableSpells` scans every trainer and teaches eligible trade recipes for free | Remove profession and trade recipe learning from this global factory path |
| Factory specialization helpers teach profession specializations | Remove them from automatic initialization |
| `TrainerAction` teaches spells from a physical trainer and charges the normal trainer cost | Retain as the authoritative rank and trainer recipe transaction, then filter purchases by career and spending style |
| Vendor purchase, recipe item use, and loot roll paths already acquire and consume recipe items | Retain and make career aware |
| Craft casts call `Player::UpdateCraftSkill`; gathering effects call `Player::UpdateGatherSkill` | Retain as the only skill advancement paths |
| Crafting affinity implicitly makes gathering its inverse | Replace with independent crafting and gathering affinities in personality API version 2 |
| Economy timer gives `economy cycle` relevance 100 for all autonomous random bots | Retain one timer action, but make usefulness require a due, market participating career with an actionable profession operation |
| Any generic `ITEM_USAGE_AH` item causes Auction House travel | Replace with profession output or profession material surplus that is safe after the career reserve |
| Any missing reagent causes Auction House travel before an eligible listing is known | Replace with a persisted recipe work order and an eligible live auction candidate |
| No candidate and failure outcomes retry on the same 20 second live interval | Replace with deterministic outcome based exponential backoff |
| Claude receives personality only for dialogue and cannot affect gameplay | Extend it with one bounded asynchronous career planning request. The deterministic server fallback remains complete when Claude is unavailable |

## Approach

**Chosen:** Add a versioned `PlayerbotCareerPlan` owned by `mod-playerbots`. The server always includes a no profession candidate, then generates legal one and two profession candidates from class, level, trainer availability, independent affinities, and the primary profession limit. Candidates with and without secondary professions remain separate choices. A career plan contains only selected skills from that candidate set, a recipe spending style, a career engagement value, and whether the bot participates in the Auction House.

The optional Claude module registers an asynchronous provider through a public playerbot career planner registry. This dependency points from `mod-playerbot-claude` to `mod-playerbots`, as it already does for personality data. `mod-playerbots` never includes or links Claude code. The provider can choose only a candidate token and one enumerated spending style. Playerbot code rejects stale, malformed, unavailable, or out of candidate results and uses a deterministic fallback after the existing response deadline. The world thread never waits for the sidecar.

Once a plan is persisted, deterministic actions travel to real trainers, buy permitted ranks and recipes with the existing tradeskill budget, use recipe items bought from vendors or the Auction House or received as drops, gather materials, and craft. Claude does not steer these actions.

The economy lifecycle becomes a career service rather than a universal background errand. Career engagement determines how often profession work can outrank questing, while a no profession career never schedules profession work. A bot travels to an Auction House only when its persisted career permits market use and it has one actionable profession sale, reagent purchase, or unknown recipe purchase. Empty attempts and failed preconditions back off rather than sending the bot back every timer tick.

## Career Semantics

| Affinity shape | Candidate emphasis | Deterministic fallback |
|---|---|---|
| Low crafting and low gathering | No profession is the only candidate | `none`, no market participation |
| Crafting stronger than gathering | No profession remains possible. Crafting candidates receive more weight | `progression`, market use only when affordable work exists |
| Gathering stronger than crafting | No profession remains possible. Gathering candidates receive more weight | `minimal`, sell only safe profession surplus |
| Both high | No profession remains possible. Mixed or complementary pairs receive the highest profession weights | `completionist` only at the high crafting threshold, otherwise `progression` |

Cooking and First Aid use crafting affinity. Fishing uses gathering affinity. They are learned through their trainers and advanced through use, but they do not consume the two primary profession slots. Secondary professions are never attached automatically. Candidate generation may offer variants with or without each eligible secondary profession, including none.

The selected career engagement is zero for no profession. A crafting career uses crafting affinity, a gathering career uses gathering affinity, and a mixed career uses the stronger relevant affinity. Deterministic profession actions use this value to compete with questing. It changes scheduling preference, not legality, cost, or skill gains.

Recipe spending styles have these exact meanings:

- `completionist`: Buy every affordable eligible trainer recipe during a legitimate visit. It may also buy affordable unknown vendor and Auction House recipe items for the selected career.
- `progression`: Buy profession ranks and only recipes that can raise the current skill or unlock the next trainer progression band.
- `minimal`: Buy required profession ranks and the cheapest recipe that preserves a path to the next skill band. Do not collect optional recipes.
- `none`: Do not learn a profession or buy profession recipes.

All styles remain constrained by the existing `free money for tradeskill` budget and live price checks. A style expresses preference, not permission to overspend.

## Clean Bot Recreation

Pierre will wipe and recreate the random bot population after this implementation. The feature therefore does not add a one time destructive profession migration for existing characters.

New bots begin with no profession skill, profession recipes, or persisted legacy selection. Career records include personality and career versions. A missing, malformed, or stale record is discarded and regenerated before any profession action. This defensive regeneration changes career metadata only. It does not erase skills or recipes from a surviving character.

## Runtime Environment

- **Incremental tests:** `/Users/pierre/Workspace/azerothcore-wotlk/build-playerbot-claude-tests` and its `unit_tests` target exist. Approval authorizes only the listed incremental unit test build. It does not authorize CMake reconfiguration or a full build.
- **Playerbot tests:** `/Users/pierre/Workspace/azerothcore-wotlk/build-playerbot-claude-tests/src/test/unit_tests`.
- **Claude sidecar:** Run from `modules/mod-playerbot-claude/sidecar` with `uv run pytest -q`.
- **Installed server:** `/Users/pierre/azeroth-server/bin/worldserver`.
- **Worldserver configuration:** `/Users/pierre/azeroth-server/etc/worldserver.conf`.
- **Playerbot configuration:** `/Users/pierre/azeroth-server/etc/modules/playerbots.conf`.
- **Claude configuration:** `/Users/pierre/azeroth-server/etc/modules/mod_playerbot_claude.conf`.
- **Live verification gate:** Source implementation can become `COMPLETE` after tests and static checks. It cannot become `VERIFIED` until Pierre separately authorizes a worldserver build and installation, cleanup execution against disposable or explicitly approved bots, and the game client scenarios below.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| An old bot survives the planned population wipe | Low | Medium | Stale career metadata regenerates safely, but operators must complete the planned wipe before evaluating profession progression |
| Claude returns an invalid or expensive career | Medium | High | Send opaque candidate tokens, validate schema and membership, keep purchases under server budgets, and fall back deterministically |
| Claude latency blocks the world thread | Low | High | Use the existing bounded bridge queue and response deadline. Poll results on the world thread and never wait synchronously |
| Bots still crowd one Auction House | Medium | High | Require career market participation plus an actionable profession operation, stable staggering, and outcome backoff |
| Bots repeatedly travel for unavailable recipes or reagents | Medium | Medium | Persist one work order, require an eligible listing before travel, and apply no candidate backoff |
| Completionist bots spend all available gold | Medium | High | Every purchase must fit `free money for tradeskill`; style changes selection breadth, not the budget |
| Removing factory learning also removes class spell behavior | Low | High | Split class spell initialization from profession spell initialization and cover both paths independently |
| Optional Claude module becomes mandatory | Low | High | Keep candidate generation, validation, fallback, persistence, and execution entirely in `mod-playerbots` |

## E2E Test Scenarios

These scenarios use the WoW 3.3.5a client, worldserver logs, character inspection, and the database only for read only confirmation. Browser automation does not apply to the game client.

### TS-001: Recreated bot starts clean

**Priority:** Critical
**Mapped Tasks:** Task 3, Task 8

| Step | Action | Expected Result |
|---|---|---|
| 1 | Complete the separately authorized bot population wipe and recreation | The selected new bot has no profession skills, profession recipes, or legacy career events |
| 2 | Run factory initialization and ordinary maintenance | No profession skill or recipe is granted and a versioned legal career record is persisted |
| 3 | Restart or refresh the bot | The valid career remains stable and factory initialization still grants no profession state |

### TS-002: Career choice becomes real trainer learning

**Priority:** Critical
**Mapped Tasks:** Task 1, Task 2, Task 3, Task 4

| Step | Action | Expected Result |
|---|---|---|
| 1 | Observe bots representing low, crafting dominant, gathering dominant, and high mixed affinity profiles | Their persisted plans come only from their legal candidate sets. No profession appears among active profiles, secondary professions are not universal, and the population includes zero, crafting, gathering, and mixed careers |
| 2 | Allow a planned bot to start its career | It travels normally to a compatible trainer and has no profession before the interaction |
| 3 | Observe the trainer transaction | The normal trainer cost is removed, only plan permitted ranks and recipes are learned, and the skill starts at the trainer granted value rather than a level scaled maximum |
| 4 | Repeat with insufficient tradeskill budget | The unaffordable purchase is skipped and no spell or skill is granted |
| 5 | Compare a no profession bot, a low engagement career bot, and a high engagement career bot over several goal selections | The no profession bot never selects profession work. The high engagement bot chooses profession work more often than the low engagement bot, while both can still quest |

### TS-003: Skills advance only through use

**Priority:** Critical
**Mapped Tasks:** Task 3, Task 4

| Step | Action | Expected Result |
|---|---|---|
| 1 | Record a trained crafting profession skill, then let the bot craft an eligible recipe | The skill changes only when the normal craft spell awards a point |
| 2 | Record a trained gathering profession skill, then let the bot complete an eligible gathering interaction | The skill changes only when the normal gathering effect awards a point |
| 3 | Run factory refresh, login, level up maintenance, trainer search, and ordinary economy cycles without using the profession | None of those paths increases the skill directly |

### TS-004: Recipes come from legitimate sources

**Priority:** Critical
**Mapped Tasks:** Task 4, Task 5

| Step | Action | Expected Result |
|---|---|---|
| 1 | Let completionist, progression, and minimal bots visit the same trainer with the same budget | Each pays normal cost and learns only the recipes permitted by its spending style |
| 2 | Offer a career recipe from a vendor, a loot source, and a player Auction House listing | The recipe enters inventory through the normal source and is learned only by using the recipe item |
| 3 | Trigger factory refresh and level up maintenance | No unknown trainer or world catalog recipe appears for free |

### TS-005: Auction House crowding is bounded

**Priority:** Critical
**Mapped Tasks:** Task 5, Task 8

| Step | Action | Expected Result |
|---|---|---|
| 1 | Observe a representative random bot population for at least three economy eligibility windows | Bots without market careers and bots without actionable profession operations do not select an auctioneer travel target |
| 2 | Give one market participant generic sale loot and another safe profession surplus | Generic loot does not trigger profession Auction House travel. The profession surplus can trigger one staggered visit |
| 3 | Give a market participant a recipe work order with no eligible listing | It does not travel blindly and enters the no candidate backoff |
| 4 | Add an affordable eligible reagent or unknown career recipe listing | One due bot travels normally, performs one core Auction House operation, then yields |
| 5 | Leave the candidate unavailable after a failed precondition | Retry spacing increases according to policy instead of repeating every 20 seconds |

### TS-006: Claude is optional and bounded

**Priority:** High
**Mapped Tasks:** Task 2, Task 6, Task 7

| Step | Action | Expected Result |
|---|---|---|
| 1 | Run with Claude enabled and return a valid candidate token and spending style | The validated plan is persisted once and later dialogue describes that same career |
| 2 | Return an unknown token, invalid enum, stale personality version, timeout, queue rejection, and budget rejection | Each case records no unvalidated choice and produces the deterministic fallback plan |
| 3 | Disable or stop the sidecar | New bots still receive deterministic careers and all profession gameplay continues |

## Progress Tracking

- [x] Task 1: Add personality API version 2 and deterministic career candidates.
- [x] Task 2: Add career plan persistence, provider registry, validation, and fallback.
- [x] Task 3: Replace factory profession grants and support clean bot recreation.
- [x] Task 4: Implement trainer travel, paid learning, and natural skill progression.
- [x] Task 5: Route recipe acquisition and fix Auction House eligibility and retries.
- [x] Task 6: Add the optional Claude career planner bridge.
- [x] Task 7: Update the Claude sidecar protocol, storage, prompt, and tests.
- [ ] Task 8: Document, verify, synchronize plans, and pin revisions.

## Implementation Deviations

| Planned item | Implemented result | Reason |
|---|---|---|
| Modify `AutoMaintenanceOnLevelupAction.cpp` | No direct change | Its calls now reach factory methods that no longer grant professions or recipes |
| Keep `PlayerbotFactoryProfessionTest.cpp` | Retired the obsolete cleanup test | Pierre will recreate the bot population, so the tested destructive profession replacement behavior is intentionally removed |
| Add `PlayerbotTrainerProfessionTest.cpp` | Extended `PlayerbotCareerPlanTest.cpp` | Trainer selection, spending style, affordability, and no profession travel are one policy surface and do not require a second test class |
| Modify `NonCombatStrategy.cpp` | No direct change | The existing economy timer remains registered, while career eligibility and engagement cadence are enforced inside `EconomyCycleAction` |
| Preserve commit `611ec477cbfda4addb8e3aabbe802979f75afce0` | Preserved as an ancestor | Its CMake integration remains intact for current tests, while its obsolete cleanup test was replaced by career policy coverage |

## File Structure

### Private playerbot repository

- `src/Bot/Personality/PlayerbotPersonality.h` and `.cpp`: Personality API version 2 and independent affinities.
- `src/Bot/Personality/PlayerbotCareerPlan.h` and `.cpp`: Legal career candidates, persisted plan values, provider registry, validation, and deterministic fallback.
- `src/Bot/Factory/PlayerbotFactory.h` and `.cpp`: Remove profession grants and invoke career initialization for clean bots.
- `src/Ai/Base/Actions/TrainerAction.h` and `.cpp`: Career filtered paid trainer purchases.
- `src/Ai/World/Rpg/Action/RpgSubActions.h` and `.cpp`: Career trainer targeting and trainer interaction.
- `src/Ai/Base/Actions/AutoMaintenanceOnLevelupAction.cpp`: Remove profession catalog refresh from level maintenance.
- `src/Ai/Base/Actions/BuyAction.cpp`, `UseItemAction.cpp`, and `LootRollAction.cpp`: Career aware vendor, recipe use, and recipe loot behavior.
- `src/Bot/Economy/PlayerbotEconomyPolicy.h` and `.cpp`: Career market eligibility, recipe purchase candidates, profession surplus, and outcome backoff.
- `src/Ai/Base/Actions/EconomyAction.h` and `.cpp`: Actionable pretravel snapshots and career recipe purchases.
- `tests/PlayerbotPersonalityTest.cpp`: Personality version 2 fixtures.
- `tests/PlayerbotCareerPlanTest.cpp`: Candidate, validation, fallback, persistence value, and provider tests.
- `tests/PlayerbotFactoryProfessionTest.cpp`: Factory and clean bootstrap contracts.
- `tests/PlayerbotTrainerProfessionTest.cpp`: Separate class and profession budget behavior at trainers.
- `tests/PlayerbotEconomyPolicyTest.cpp`: Auction eligibility, recipe, surplus, and retry fixtures.
- `mod-playerbots.cmake`: Register every new source and test.

### Standalone Claude module repository

- `src/ClaudeChat.h` and `.cpp`: Career request and response wire records.
- `src/ClaudeChatScripts.cpp`: Provider registration, asynchronous request lifecycle, and shared career dialogue context.
- `src/mod_playerbot_claude_loader.cpp`: Personality API version 2 compatibility assertion.
- `tests/ClaudeChatTest.cpp`: Serialization, parsing, timeout, and fallback boundary tests.
- `sidecar/src/playerbot_claude/protocol.py`: Version 2 profile and strict career request and response models.
- `sidecar/src/playerbot_claude/claude.py`: Structured output call and career prompt.
- `sidecar/src/playerbot_claude/storage.py`: Add gathering affinity and persisted career fields through an idempotent SQLite migration.
- `sidecar/src/playerbot_claude/app.py`: Route career requests separately from dialogue requests.
- `sidecar/tests/test_sidecar_unit.py` and `test_sidecar_integration.py`: Protocol, storage migration, strict output, and failure tests.

## Implementation Tasks

### Task 1: Add personality API version 2 and deterministic career candidates

**Objective:** Give each bot independent crafting and gathering affinities, then generate a bounded legal career candidate set without granting any skill.

**Files:**

- Modify: `modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.h`
- Modify: `modules/mod-playerbots/src/Bot/Personality/PlayerbotPersonality.cpp`
- Create: `modules/mod-playerbots/src/Bot/Personality/PlayerbotCareerPlan.h`
- Create: `modules/mod-playerbots/src/Bot/Personality/PlayerbotCareerPlan.cpp`
- Modify: `modules/mod-playerbots/tests/PlayerbotPersonalityTest.cpp`
- Create: `modules/mod-playerbots/tests/PlayerbotCareerPlanTest.cpp`
- Modify: `modules/mod-playerbots/mod-playerbots.cmake`

**Key Decisions / Notes:**

- Bump `PLAYERBOT_PERSONALITY_API_VERSION` from 1 to 2. Preserve the version 1 derivation of crafting, exploration, sociability, and voice, then derive gathering from a new documented namespace constant so existing identities do not reshuffle.
- Replace inverse gathering weights with helpers that accept both affinities. Candidate generation always includes no profession, then legal single professions, legal primary pairs, and optional personality aligned secondary variants.
- Persist a career engagement value. No profession uses zero. Crafting and gathering careers use their matching affinity, and mixed careers use the stronger relevant affinity.
- Expose opaque candidate tokens plus descriptive categories to providers. Raw skill and spell identifiers remain private to playerbot validation.
- Use deterministic weighted selection as the complete fallback. Money is enforced at each purchase through existing budget values, not frozen into the career identity.

**Definition of Done:**

- [x] RED tests fail before version 2, independent gathering, and career candidates exist.
- [x] Literal GUID fixtures preserve every version 1 field and prove gathering varies independently.
- [x] Candidate fixtures enforce the primary profession limit, keep no profession possible at every affinity, make secondary professions optional, and produce crafting, gathering, and mixed careers for representative profiles.
- [x] Candidate generation mutates no `Player`, skill, spell, inventory, money, or database state.
- [x] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotPersonality*:*PlayerbotCareerPlan*'`

### Task 2: Add career persistence, provider registration, validation, and fallback

**Objective:** Persist one validated career plan per personality and progression version while allowing an optional nonblocking provider.

**Files:**

- Modify: `modules/mod-playerbots/src/Bot/Personality/PlayerbotCareerPlan.h`
- Modify: `modules/mod-playerbots/src/Bot/Personality/PlayerbotCareerPlan.cpp`
- Modify: `modules/mod-playerbots/src/Bot/RandomPlayerbotMgr.h`
- Modify: `modules/mod-playerbots/src/Bot/RandomPlayerbotMgr.cpp`
- Modify: `modules/mod-playerbots/tests/PlayerbotCareerPlanTest.cpp`

**Key Decisions / Notes:**

- Store plan version, candidate token, selected skills, spending style, and market participation through existing random bot event values. Do not add a table.
- Provide one process lifetime provider registry with `TrySubmit` and `Poll` behavior. Registration occurs during module startup and cannot be replaced after world startup.
- Keep a bounded pending state per bot. If there is no provider, submission fails, the result misses the existing response deadline, or validation fails, persist the deterministic fallback.
- Validate personality version, career plan version, bot GUID, candidate token membership, spending style enum, and market permission before persistence.
- Never persist provider supplied raw IDs or text.

**Definition of Done:**

- [ ] A persisted valid plan survives refresh, logout, and restart without a new provider call.
- [ ] Disabled, absent, busy, timed out, stale, and invalid providers all select the same deterministic fallback for the same bot.
- [ ] The world thread performs no socket or model wait.
- [ ] A response for another bot or an older request cannot change the plan.
- [ ] Verify: `build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotCareerPlan*'`

### Task 3: Replace factory profession grants and support clean bot recreation

**Objective:** Remove all direct profession skill, recipe, and specialization initialization so recreated random bots begin clean.

**Files:**

- Modify: `modules/mod-playerbots/src/Bot/Factory/PlayerbotFactory.h`
- Modify: `modules/mod-playerbots/src/Bot/Factory/PlayerbotFactory.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/AutoMaintenanceOnLevelupAction.cpp`
- Modify: `modules/mod-playerbots/tests/PlayerbotFactoryProfessionTest.cpp`
- Modify: `modules/mod-playerbots/mod-playerbots.cmake`

**Key Decisions / Notes:**

- Split class spell initialization from profession spell initialization. Class auto learning retains its existing configuration behavior.
- Delete or disconnect `SetRandomSkill`, profession starter spell grants, specialization grants, and global trade trainer scanning from create, refresh, and level up paths.
- Reject missing, malformed, or stale career metadata and regenerate only the plan. Do not mutate profession skills or recipes as part of metadata recovery.
- Rely on Pierre's separately performed population wipe to remove historical factory granted profession state.
- Preserve the follow-up integration fix in commit `611ec477cbfda4addb8e3aabbe802979f75afce0`, which already registers `PlayerbotFactoryProfessionTest.cpp`.

**Definition of Done:**

- [ ] RED tests demonstrate that factory creation or refresh currently grants or maximizes a profession.
- [ ] New factory creation, refresh, and level maintenance never grant or increase profession skills or recipes.
- [ ] A newly recreated random bot begins without profession skills, recipes, or legacy career selections.
- [ ] Missing, malformed, and stale career metadata regenerates without mutating skills, recipes, inventory, or money.
- [ ] Class spells covered by existing auto learning behavior remain unchanged.
- [ ] Verify: `build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotFactoryProfession*'`

### Task 4: Implement trainer travel, paid learning, and use based advancement

**Objective:** Make planned bots reach compatible trainers and pay for permitted profession ranks and trainer recipes.

**Files:**

- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/TrainerAction.h`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/TrainerAction.cpp`
- Modify: `modules/mod-playerbots/src/Ai/World/Rpg/Action/RpgSubActions.h`
- Modify: `modules/mod-playerbots/src/Ai/World/Rpg/Action/RpgSubActions.cpp`
- Modify: `modules/mod-playerbots/tests/PlayerbotCareerPlanTest.cpp`
- Create: `modules/mod-playerbots/tests/PlayerbotTrainerProfessionTest.cpp`
- Modify: `modules/mod-playerbots/mod-playerbots.cmake`

**Key Decisions / Notes:**

- Select a physical trainer that teaches an unlearned planned skill or permitted recipe and travel through the existing RPG target system.
- Keep `CanTeachSpell`, trainer prerequisites, primary profession limits, money checks, and the core trainer transaction authoritative.
- Split the trainer affordability decision by lesson type. Class trainer lessons retain `NeedMoneyFor::spells`. Career profession ranks and recipes require `free money for tradeskill`. Both paths use the reputation discounted trainer cost, actual money deduction, and existing cheat behavior.
- Always consider an affordable required rank. Filter recipes by the persisted spending style definitions.
- Do not call `SetSkill` to advance a learned profession. Normal `UpdateCraftSkill` and `UpdateGatherSkill` calls remain the only progression.

**Definition of Done:**

- [ ] A bot without a planned profession never travels to its trainer or buys its spells.
- [ ] A planned bot reaches a compatible trainer, pays the exact core cost, and learns only permitted entries.
- [ ] Insufficient budget changes no skill, spell, or money.
- [ ] A bot with spell budget but no tradeskill budget may buy a class lesson but not a profession lesson. A bot with tradeskill budget but no spell budget may buy the permitted profession lesson but not the class lesson.
- [ ] No factory, trainer selection, maintenance, or career plan code sets profession skill points.
- [ ] Verify: `python apps/codestyle/codestyle-cpp.py`

### Task 5: Route recipes through legitimate sources and bound Auction House behavior

**Objective:** Make trainer, vendor, loot, and Auction House recipe acquisition career aware while preventing universal Auction House travel.

**Files:**

- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/BuyAction.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/UseItemAction.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/LootRollAction.cpp`
- Modify: `modules/mod-playerbots/src/Bot/Economy/PlayerbotEconomyPolicy.h`
- Modify: `modules/mod-playerbots/src/Bot/Economy/PlayerbotEconomyPolicy.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/EconomyAction.h`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/EconomyAction.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Strategy/NonCombatStrategy.cpp`
- Modify: `modules/mod-playerbots/tests/PlayerbotEconomyPolicyTest.cpp`

**Key Decisions / Notes:**

- Vendor buying and loot rolling consider only unknown recipe items compatible with the career and spending style. Learning still occurs through the existing recipe item use action.
- Add unknown career recipe items as an Auction House purchase candidate under the same affordability and safety checks as reagents.
- `EconomyCycleAction::isUseful` requires all of: autonomous random bot, persisted market participation, due policy time, and one live actionable profession operation.
- Sale candidates must be crafted outputs or profession materials beyond the existing reserve. Generic `ITEM_USAGE_AH` loot is not a profession economy reason.
- Build the purchase side of the pretravel snapshot from the bot's faction compatible `AuctionHouseObject` even when no auctioneer is nearby. This read only discovery selects an exact eligible listing but performs no remote bid. The selected auction is revalidated after physical arrival before the core handler receives the packet.
- Reagent travel requires a persisted learned recipe work order and an eligible listing from that snapshot. Unknown recipe travel requires an eligible recipe listing. A cold snapshot, missing Auction House object, empty result, or cache miss returns no candidate and enters backoff. Do not travel merely because a reagent or recipe is absent.
- Retain stable GUID staggering. After a successful operation use the normal interval. After no candidate or failed precondition, double the previous interval through five bounded steps, then hold at that cap until an actionable snapshot resets it.
- Keep one operation per cycle and the existing core Auction House handlers. No direct auction, item, or money mutation.

**Definition of Done:**

- [ ] RED policy tests reproduce generic loot travel, blind missing reagent travel, and identical 20 second retries.
- [ ] Nonmarket bots and bots with no actionable profession work never select auctioneer travel.
- [ ] Generic loot does not trigger profession Auction House travel.
- [ ] An away from Auction House snapshot with an affordable eligible listing can select that exact candidate. A cold, missing, or empty snapshot cannot trigger travel.
- [ ] Eligible reagent, unknown recipe, and profession surplus candidates each produce at most one normal operation.
- [ ] No candidate and failed precondition fixtures prove deterministic increasing backoff and reset after a valid operation.
- [ ] Verify: `build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotEconomyPolicy*:*PlayerbotCareerPlan*'`

### Task 6: Add the optional Claude career planner bridge

**Objective:** Let the Claude module submit one bounded career choice without giving it runtime action authority.

**Files:**

- Modify: `modules/mod-playerbot-claude/src/ClaudeChat.h`
- Modify: `modules/mod-playerbot-claude/src/ClaudeChat.cpp`
- Modify: `modules/mod-playerbot-claude/src/ClaudeChatScripts.cpp`
- Modify: `modules/mod-playerbot-claude/src/mod_playerbot_claude_loader.cpp`
- Modify: `modules/mod-playerbot-claude/tests/ClaudeChatTest.cpp`

**Key Decisions / Notes:**

- Register the optional provider with the playerbot career planner registry at startup and unregister it during shutdown.
- Reuse the existing bounded queue, loopback authenticated bridge, daily budget, and `ResponseDeadlineMs`.
- Send personality version 2 values, opaque legal candidate tokens with human readable summaries, and no raw gameplay identifiers.
- Parse only candidate token and spending style. Market participation is derived and validated by the selected candidate and style on the playerbot side.
- Add the validated persisted career to later dialogue context. Dialogue still cannot issue gameplay actions.

**Definition of Done:**

- [ ] C++ tests cover exact request serialization, strict response parsing, correlation, timeout, queue rejection, stale version, and invalid token handling.
- [ ] The existing conversation and ambient chat paths remain compatible and within their queue and budget limits.
- [ ] No playerbot source includes a Claude header or requires the Claude module.
- [ ] Verify: `build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='ClaudeChat*:*PlayerbotCareerPlan*'`

### Task 7: Update the Claude sidecar protocol, storage, prompt, and tests

**Objective:** Support personality version 2 and strict structured career decisions while preserving dialogue behavior.

**Files:**

- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/protocol.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/claude.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/storage.py`
- Modify: `modules/mod-playerbot-claude/sidecar/src/playerbot_claude/app.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_unit.py`
- Modify: `modules/mod-playerbot-claude/sidecar/tests/test_sidecar_integration.py`

**Key Decisions / Notes:**

- Accept profile version 2 only after the C++ compatibility bump lands.
- Use the Anthropic SDK structured JSON schema output for the bounded candidate token and spending style response.
- Route career planning separately from chat so career output cannot become player visible dialogue and chat text cannot become a career decision.
- Migrate existing SQLite profile rows idempotently to gathering affinity and optional persisted career context. Do not discard conversation history.
- When the SDK, schema, model, budget, or storage operation fails, return an explicit failure to C++ and let playerbot fallback decide.

**Definition of Done:**

- [ ] Unit tests reject extra fields, raw IDs, unknown tokens, wrong versions, and invalid spending styles.
- [ ] Storage migration preserves existing profiles and conversation history while adding version 2 values.
- [ ] Integration tests prove one structured career response and existing chat responses through the loopback protocol.
- [ ] No test makes a real Anthropic request.
- [ ] Verify: `cd modules/mod-playerbot-claude/sidecar && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

### Task 8: Document, verify, synchronize plans, and pin revisions

**Objective:** Align operator documentation, run all available evidence, commit coordinated nested repository changes with the plan, and leave live destructive verification behind an explicit gate.

**Files:**

- Modify: `modules/mod-playerbots/conf/playerbots.conf.dist`
- Modify: `modules/mod-playerbots/README.md`
- Modify: `modules/mod-playerbots/docs/personality.md`
- Modify: `modules/mod-playerbots/docs/economy.md`
- Create: `modules/mod-playerbots/docs/plans/2026-07-30-playerbots-trainer-profession-progression.md`
- Modify: `modules/mod-playerbot-claude/conf/mod_playerbot_claude.conf.dist`
- Modify: `modules/mod-playerbot-claude/README.md`
- Modify: `modules/mod-playerbot-claude/docs/architecture.md`
- Create: `modules/mod-playerbot-claude/docs/plans/2026-07-30-playerbots-trainer-profession-progression.md`
- Modify: `modules/mod-playerbot-claude/PLAYERBOTS_REVISION`
- Modify: `docs/plans/2026-07-30-playerbots-trainer-profession-progression.md`

**Key Decisions / Notes:**

- Clarify that `AiPlayerbot.AllowLearnTrainerSpells` and `AiPlayerbot.AutoLearnTrainerSpells` do not grant trade professions or recipes from the global catalog. Document physical trainer, vendor, drop, and Auction House sources.
- Document personality version 2, optional participation, career engagement, spending styles, clean bot recreation, Claude fallback, and Auction House eligibility and backoff.
- Run the focused suites, full configured unit test binary, C++ codestyle, sidecar tests and quality checks, diff checks, and required change reviews before source status becomes `COMPLETE`.
- Commit `mod-playerbots` with its plan copy first. Pin that exact 40 character revision in `mod-playerbot-claude`, then commit the Claude module with its plan copy. Commit the root plan last. Use the required commit workflow for each repository.
- Do not touch the unrelated `modules/mod-playerbots/docs/plans/2026-07-30-playerbots-deeprun-tram.md`.
- Do not install, restart, mutate deployed configuration, wipe or recreate live bots, push, or mark the plan `VERIFIED` without separate current permission.

**Definition of Done:**

- [ ] Focused tests for personality, career, factory professions, economy, and Claude exit with zero failures.
- [ ] The full configured `unit_tests` binary exits with zero failures.
- [ ] Playerbot C++ codestyle, sidecar Pytest and Ruff, and every changed repository diff check exit zero.
- [ ] Changed non Markdown files contain no decorative non ASCII characters.
- [ ] Documentation matches the shipped behavior and names the required clean bot recreation.
- [ ] `PLAYERBOTS_REVISION` equals the committed playerbot revision.
- [ ] No plan created by this feature remains untracked in its owning repository.
- [ ] TS-001 through TS-006 remain explicitly pending until separately authorized live verification completes.

## Verification Results

Source status is `COMPLETE`. Live world behavior remains pending because this plan does not authorize configure, install, worldserver lifecycle changes, bot deletion, bot recreation, or client interaction.

| Check | Result |
|-------|--------|
| Incremental C++ build | `cmake --build build-playerbot-claude-tests --target unit_tests -j4` completed successfully. Compilation was limited to four jobs at Pierre's request. |
| Focused C++ tests | Personality, career, economy, and Claude filters ran 81 tests with 81 passed, including invalid persisted career regeneration. |
| Full configured C++ tests | 11,507 tests ran. 6,021 passed, 5,486 data dependent tests skipped, and one test remained disabled. Exit status was zero. |
| Playerbot C++ codestyle | Running `python ../../apps/codestyle/codestyle-cpp.py` from `modules/mod-playerbots` passed every check. |
| Claude C++ codestyle | Running the same checker from `modules/mod-playerbot-claude` passed every check. |
| Root C++ codestyle | The root scan still reports existing issues in unrelated core files and generated `src/graphify-out/graph.html`. None is part of this change. |
| Sidecar quality | Ruff lint and format checks passed. Basedpyright analyzed seven files with zero errors and zero warnings. Pytest ran 80 tests with 80 passed. |
| Diff integrity | Working diff checks passed in the root and both nested repositories. Staged diff checks are repeated immediately before commit. |
| Source review | The initial changes review found stale career validation, mixed service trainer usefulness, missing Task 8 artifacts, and missing direct recovery coverage. All findings were fixed. The focused follow up review passed with no remaining findings. |
| Decorative Unicode | Added non Markdown lines contain no decorative non ASCII characters. Existing accented test fixtures remain required Unicode data. |
| File size | New `PlayerbotCareerPlan.cpp` is 800 lines. Other new or materially expanded files remain below the project split threshold. The existing 5,096 line factory file shrank in this change. |

TS-001 through TS-006 remain pending. Pierre will separately wipe and recreate the random bot population before live verification. The clean restart replaces destructive migration logic. Until that authorization and client evidence exist, this plan must remain `COMPLETE`, not `VERIFIED`.
