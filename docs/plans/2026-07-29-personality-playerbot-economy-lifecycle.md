# Personality Playerbot Economy Lifecycle Implementation Plan

Created: 2026-07-29
Author: magitekrr@gmail.com
Agent: Codex
Status: COMPLETE
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** Random playerbots autonomously gather profession materials, keep the materials their learned recipes need, buy affordable missing reagents from real auctions, craft useful or skill raising items, list genuine surplus, and collect auction items or proceeds from mail.

## Out of Scope

- Installing or requiring `mod-ah-bot-plus`. Playerbot trading must work when that module is absent.
- Giving the Claude sidecar any control over economy decisions. The language model remains chat only.
- Creating items, gold, bids, sales, or mail outside AzerothCore's normal handlers.
- Direct bot to bot inventory transfers, coordinated price fixing, speculative flipping, cancel and relist behavior, or a separate simulated market.
- AzerothCore core code or database schema changes.
- Pushing, deploying, or enabling the optional liquidity module. Those actions require separate permission.

## Approach

**Chosen:** A pure `PlayerbotEconomyPolicy` plus one scheduled `EconomyCycleAction` that composes existing playerbot inventory, travel, crafting, and mail facilities with AzerothCore Auction House handlers.

**Why:** The current module already gives random bots gathering professions, the default `gather` strategy, learned recipe discovery, profession material retention through `ItemUsageValue`, city travel, and mailbox packet handling. The missing seam is a deterministic coordinator. Keeping selection and pricing in a pure policy makes the risky decisions directly testable while the runtime action remains thin glue around real game operations.

## Context for Implementer

The current Auction House code in `StoreLootAction::AuctionItem` is entirely commented legacy code and uses obsolete core fields. It must remain removed from the runtime path. New sales call `WorldSession::HandleAuctionSellItem`, and purchases call `WorldSession::HandleAuctionPlaceBid`, so deposits, ownership checks, money, persistence, achievements, auction hooks, and generated mail stay authoritative in AzerothCore.

`ItemUsageValue::Calculate` already marks profession materials below one stack as `ITEM_USAGE_SKILL`, keeps the second stack as `ITEM_USAGE_KEEP`, and only marks later tradeable surplus as `ITEM_USAGE_AH`. The economy action must consume that classification instead of adding a second reservation model.

Personality version 1 already shapes profession selection and gathering exploration. This lifecycle acts deterministically on those personality shaped professions and uses `PlayerbotPersonality::SplitMix64` with economy specific namespace constants only for stable candidate tie breaks. It does not change profile fields, field derivation, voice ordering, or `PLAYERBOT_PERSONALITY_API_VERSION`.

`mod-ah-bot-plus` currently documents separate non playerbot Auction House characters, an optional seller, and an optional buyer. There is no compile time or runtime API coupling. Its listings can satisfy playerbot reagent purchases, and its buyer can provide demand for playerbot surplus, but humans and playerbots can trade without it.

## Runtime Environment

- **Incremental tests:** The configured `/Users/pierre/Workspace/azerothcore-wotlk/build-playerbot-claude-tests` tree and its `unit_tests` target are present. Approval of this plan authorizes the listed incremental unit test build. It does not authorize reconfiguration or a full server build.
- **Installed server:** `/Users/pierre/azeroth-server/bin/worldserver` with `/Users/pierre/azeroth-server/etc/worldserver.conf`.
- **Playerbot configuration:** `/Users/pierre/azeroth-server/etc/modules/playerbots.conf`.
- **Start command:** `cd /Users/pierre/azeroth-server/bin && ./worldserver -c ../etc/worldserver.conf`.
- **Restart:** Stop worldserver cleanly, install an explicitly authorized build, then rerun the start command. Do not replace a running binary during verification.
- **Live verification gate:** Source implementation can reach `COMPLETE` with unit, codestyle, and static evidence. It cannot reach `VERIFIED`, and TS-001 through TS-005 cannot be claimed, until Pierre separately authorizes a worldserver build and installation or deployment of the committed playerbot revision. Record the executable revision and effective `EconomyLifecycleEnabled` value with every live result.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Bots drain their gold on an overpriced reagent | Medium | High | A purchase must fit `free money for tradeskill` and must not exceed the item template buy price for the auctioned quantity. Zero price references fail closed. |
| Reserved materials or useful crafted items are listed | Medium | High | Sales accept only `ITEM_USAGE_AH`. Tests cover that `ITEM_USAGE_SKILL` and `ITEM_USAGE_KEEP` never become sale candidates. |
| Auction inventory or gold diverges after a failure | Low | High | The action submits core Auction House packets and performs no direct Auction House database writes or manual inventory mutation. |
| A mailbox interaction deletes attachments when bags are full | Medium | High | Internal auction mail collection reuses the existing bag space guard and leaves the mail untouched when item storage is unavailable. |
| Economy work starves normal gathering, questing, or combat | Medium | Medium | The cached per bot action uses `randomBotUpdateInterval` as a minimum cooldown, applies a stable GUID offset, yields after every attempt, and releases completed economy travel targets. |
| Optional liquidity becomes an accidental hard dependency | Low | High | No source include, config lookup, hook, or test references `mod-ah-bot-plus`. E2E includes a run with that module absent. |

## E2E Test Scenarios

These scenarios use the WoW 3.3.5a client and worldserver logs because the user interface is the game client.

### TS-001: Gathered profession materials remain reserved
**Priority:** Critical
**Preconditions:** A random bot has a gathering or crafting profession, knows at least one recipe with item reagents, and the economy lifecycle is enabled.
**Mapped Tasks:** Task 1, Task 2, Task 3

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Observe an eligible autonomous gathering bot receive and complete a normal gathering target through the default `gather` strategy | The bot gathers without a master command, and no economy action replaces the gathering strategy. |
| 2 | Let the economy cycle run while the bot has other saleable surplus | Only `ITEM_USAGE_AH` surplus is selected. The reserved reagent remains in inventory. |
| 3 | Observe the bot after the economy action completes or declines an operation | The action enters cooldown, releases a completed economy travel target, and the bot resumes gathering or exploration opportunities. |
| 4 | Repeat from the same inventory and personality snapshot in the policy test | The same next phase and candidate are selected. |

### TS-002: Missing reagent is bought and crafted
**Priority:** Critical
**Preconditions:** A human lists a missing learned recipe reagent at or below its item template buy price. The bot has enough `free money for tradeskill`, an auctioneer is reachable, and a mailbox is near the Auction House.
**Mapped Tasks:** Task 1, Task 2, Task 3

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Allow the bot's noncombat economy cycle to choose a destination | The existing travel target moves the bot to a compatible Auction House instead of teleporting it. |
| 2 | Observe the bot at the auctioneer | The bot buys the cheapest eligible buyout through `HandleAuctionPlaceBid`, and normal Auction House mail is created. |
| 3 | Allow the bot to reach the nearby mailbox and continue cycling | The bought item is taken through the mail handler, then one eligible learned recipe is crafted through the normal spell path. |

### TS-003: Crafted surplus is sold and proceeds are collected
**Priority:** Critical
**Preconditions:** The bot owns a tradeable `ITEM_USAGE_AH` stack, has enough gold for the core deposit, and a human buyer can inspect the same Auction House.
**Mapped Tasks:** Task 1, Task 2, Task 3

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Let the economy cycle run at an auctioneer | One real inventory stack is listed through `HandleAuctionSellItem` with the core minimum auction duration. |
| 2 | Inspect and buy the listing as the human | The listing shows the bot as owner, the item leaves the Auction House, and normal sale mail is created. |
| 3 | Let the bot revisit the nearby mailbox | The proceeds are credited through the mail handler and the sale mail is removed only after successful collection. |

### TS-004: Optional liquidity changes volume, not correctness
**Priority:** High
**Preconditions:** The lifecycle passes TS-002 and TS-003 without `mod-ah-bot-plus`.
**Mapped Tasks:** Task 4, Task 5

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run the server without `mod-ah-bot-plus` | Human listings can still be bought, playerbot listings remain valid, and no missing module errors appear. |
| 2 | Separately install and configure `mod-ah-bot-plus` with non playerbot Auction House characters | Its seller adds optional supply and its buyer adds optional demand without changing playerbot configuration. |
| 3 | Disable the optional module again | Existing playerbot auctions, mail, crafting, and deterministic decisions continue through AzerothCore alone. |

### TS-005: Invalid or unsafe market operations fail closed
**Priority:** Critical
**Preconditions:** Candidate fixtures include an own auction, a zero buyout, an unaffordable listing, an overpriced listing, a bound item, and auction mail with insufficient bag space.
**Mapped Tasks:** Task 1, Task 2

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Evaluate the purchase candidates | Own, zero buyout, unaffordable, and above template price auctions are rejected. |
| 2 | Evaluate the sale candidates | Bound, reserved, and zero reference price items are rejected. |
| 3 | Attempt to collect an auction item with insufficient bag space | The item and mail remain intact, and the bot performs no later phase in that cycle. |

## Progress Tracking

- [x] Task 1: Define and test the deterministic economy policy.
- [x] Task 2: Implement real Auction House, crafting, travel, and auction mail operations.
- [x] Task 3: Schedule the lifecycle for eligible random bots.
- [x] Task 4: Add operator configuration and economy documentation.
- [x] Task 5: Verify, synchronize plans, and pin the compatible playerbot revision.

## File Structure

### Private playerbot repository

- `modules/mod-playerbots/src/Bot/Economy/PlayerbotEconomyPolicy.h` and `.cpp`: Pure phase, purchase, craft, sale, and price decisions.
- `modules/mod-playerbots/src/Ai/Base/Actions/EconomyAction.h` and `.cpp`: Runtime orchestration using existing values, travel, core Auction House handlers, spell casting, and mail handling.
- `modules/mod-playerbots/tests/PlayerbotEconomyPolicyTest.cpp`: Literal deterministic policy fixtures and safety boundaries.
- `modules/mod-playerbots/docs/economy.md`: Lifecycle, configuration, invariants, optional liquidity, and operations.

### Standalone Claude module repository

- `modules/mod-playerbot-claude/PLAYERBOTS_REVISION`: Exact compatible playerbot commit after the lifecycle lands.
- `modules/mod-playerbot-claude/docs/plans/2026-07-29-personality-playerbot-economy-lifecycle.md`: Approved coordinated plan copy.

## Implementation Tasks

### Task 1: Define and test the deterministic economy policy

**Objective:** Introduce a pure policy that receives already observed inventory, recipe, auction, mail, budget, and personality inputs, then returns one deterministic phase and at most one operation candidate. Establish failing literal tests before runtime integration.

**Files:**

- Create: `modules/mod-playerbots/src/Bot/Economy/PlayerbotEconomyPolicy.h`
- Create: `modules/mod-playerbots/src/Bot/Economy/PlayerbotEconomyPolicy.cpp`
- Create: `modules/mod-playerbots/tests/PlayerbotEconomyPolicyTest.cpp`
- Modify: `modules/mod-playerbots/mod-playerbots.cmake`

**Key Decisions / Notes:**

- Define phases in this exact precedence: collect delivered auction mail, craft one currently craftable useful or skill raising recipe, advance one selected incomplete learned recipe by buying a missing reagent, sell one surplus stack, then return to existing gathering and exploration behavior.
- Accept plain value records rather than `Player`, `Item`, `AuctionEntry`, or `Mail` pointers. The policy remains independent of world state, databases, sessions, and clocks.
- Select one concrete learned recipe before considering purchases. Calculate every reagent deficit from that recipe and current inventory. Keep the same recipe stable across cycles by skill up priority, output usage priority, personality tie break, and spell ID.
- Select purchase candidates only when one auction stack closes a complete deficit for the selected recipe. Rank by lowest buyout per item, then stable personality tie break, then auction ID. Reject unrelated reagents, partial stacks that do not close the chosen deficit, own account auctions, zero buyout, quantities above the existing two stack reserve ceiling, prices above `free money for tradeskill`, and prices above `ItemTemplate::BuyPrice * quantity`.
- Select craft candidates by existing skill up and item usage priorities. Resolve equal candidates with `SplitMix64(botGuidCounter xor spellId xor economyNamespace)`, then spell ID. Do not call `urand`, `std::random`, wall clock time, or persisted random events.
- Include exact item instance facts in sale candidates. Require `ITEM_USAGE_AH`, a tradeable exact GUID and count, an empty non container payload, no binding, no conjured flag, no duration, and no existing Auction House registration. Match the lowest nonzero competing per unit buyout when one exists. Otherwise use the item template buy price per unit. Skip zero references. Set starting bid to the item template sell value for the stack, clamped from one copper through buyout.
- Define a stable first cycle offset from the bot GUID and `randomBotUpdateInterval`. Tests use literal timestamps to prove that every attempt, including no candidate and failed precondition outcomes, yields until the next eligible time.
- Use distinct fixed economy namespace constants documented beside the policy. Adding tie break helpers does not change personality profile version 1.

**Definition of Done:**

- [x] RED tests fail because the policy API and decisions do not exist before production code.
- [x] Literal fixtures prove exact phase precedence and repeatable selection for fixed GUID, inventory, recipe, and auction inputs.
- [x] Multi reagent recipe fixtures prove stable work order selection, sequential deficit closure, crafting only after all deficits close, and rejection of unrelated or undersized auction stacks.
- [x] Purchase fixtures reject own, zero buyout, over reserve, over budget, and over template price candidates.
- [x] Sale fixtures include bound and unbound instances of the same item entry, nonempty bags, duration items, conjured items, already auctioned GUIDs, and exact market matched or template fallback prices.
- [x] Cadence fixtures prove stable staggering, bounded retry time, and a cooldown after operation, no candidate, and failed precondition outcomes.
- [x] A one character change to candidate price, ID, usage, or GUID causes at least one assertion to fail.
- [x] Verify: `cmake --build build-playerbot-claude-tests --target unit_tests --parallel "$(sysctl -n hw.logicalcpu)" && build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotEconomyPolicy*'`

### Task 2: Implement real Auction House, crafting, travel, and auction mail operations

**Objective:** Add a single noncombat economy cycle that observes live bot state, delegates decisions to the pure policy, travels through the existing target system, and performs one real core operation per cycle, as verified by TS-001, TS-002, TS-003, and TS-005.

**Files:**

- Create: `modules/mod-playerbots/src/Ai/Base/Actions/EconomyAction.h`
- Create: `modules/mod-playerbots/src/Ai/Base/Actions/EconomyAction.cpp`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/MailAction.h`
- Modify: `modules/mod-playerbots/src/Ai/Base/Actions/MailAction.cpp`

**Key Decisions / Notes:**

- Derive `EconomyCycleAction` from `ChooseTravelTargetAction`. Reject active masters, combat, battlegrounds, dead or teleporting bots, disabled configuration, and non random bots before collecting candidates.
- Enumerate known create item profession spells through `ListSpellsAction::GetSpellList`, existing spell metadata, `ItemUsageValue::SpellGivesSkillUp`, current reagent counts, and `CanCastSpell`. Build the selected work order from one exact recipe, cast only that selected spell through the existing playerbot spell path, and recompute the work order from live state every cycle.
- Enumerate missing reagents only for the selected learned recipe. Use current `ITEM_USAGE_SKILL` and `ITEM_USAGE_KEEP` classification as the reserve contract. Never mutate `CraftData` or create a parallel persisted reservation.
- Resolve a nearby compatible auctioneer with `GetNPCIfCanInteractWith`. If none is in range, set a forced travel target with `SetNpcFlagTarget({UNIT_NPC_FLAG_AUCTIONEER})` and stop that cycle.
- Read candidate listings from the faction compatible `AuctionHouseObject`, but submit a selected buyout as the packet expected by `WorldSession::HandleAuctionPlaceBid`. Submit selected inventory stacks through `WorldSession::HandleAuctionSellItem` using `MIN_AUCTION_TIME / MINUTE`. Do not call `AuctionEntry::SaveToDB`, `AuctionHouseObject::AddAuction`, `ModifyMoney`, or inventory removal directly.
- Immediately before a sell packet, re-resolve the selected item by exact GUID and recheck count, bag location, `CanBeTraded`, binding, container contents, conjured flag, duration, and `sAuctionMgr->GetAItem`. Any mismatch ends the cycle without a packet.
- Before mail collection, refresh mail through the session handler. Restrict the internal path to delivered `MAIL_AUCTION` messages. Refactor the existing take processor so manual `mail take` keeps its master checks and messages while the economy path can silently collect auction items or money.
- After each take handler returns, recheck the live mail. Remove it only when its money and attachments are gone. A failed item take or full bag leaves the message and remaining attachment intact.
- Use `MailProcessor::FindMailbox` first. When a mailbox game object is visible but outside interaction range, move toward the nearest visible mailbox through `MovementAction::MoveTo`. When none is loaded, travel to the Auction House and retry on a later cycle.
- Perform at most one purchase, sale, or craft cast per cycle. Auction mail collection may process the delivered auction messages that fit current bag space, then ends the cycle.
- Rely on the per bot cached action instance supplied by `NamedObjectContext<Action>` to hold `nextEligibleTime`. Initialize it with the policy's stable GUID offset, then advance it by the existing `randomBotUpdateInterval` after every attempt. While cooling down, `isUseful` returns false so gathering, exploration, questing, and other noncombat actions retain control.
- Clear a forced economy travel target after a successful operation or when the live snapshot no longer needs Auction House or mail work. Do not clear unrelated group, quest, or player forced targets.

**Definition of Done:**

- [ ] TS-001 proves reserved profession materials are never passed to the sell packet.
- [ ] TS-002 proves one eligible reagent buyout, normal Auction House mail, mailbox collection, and one normal craft cast.
- [ ] TS-003 proves a real bot owned listing, normal purchase by another character, and normal proceeds collection.
- [ ] TS-005 proves invalid operations leave inventory, gold, auctions, and mail unchanged.
- [ ] Repeated timer firings between eligible times perform no economy operation and preserve normal gathering or exploration opportunities.
- [x] No active path references the commented `StoreLootAction::AuctionItem`.
- [ ] Verify: `python apps/codestyle/codestyle-cpp.py`

### Task 3: Schedule the lifecycle for eligible random bots

**Objective:** Wire the economy cycle into the existing noncombat timer with a single enable switch, without introducing a parallel AI engine or changing combat behavior.

**Files:**

- Modify: `modules/mod-playerbots/src/Ai/Base/ActionContext.h`
- Modify: `modules/mod-playerbots/src/Ai/Base/Strategy/NonCombatStrategy.cpp`
- Modify: `modules/mod-playerbots/src/PlayerbotAIConfig.h`
- Modify: `modules/mod-playerbots/src/PlayerbotAIConfig.cpp`

**Key Decisions / Notes:**

- Register only one new action name, `economy cycle`, in `ActionContext`.
- Add `economy cycle` to the existing `timer` trigger in `NonCombatStrategy`. Do not create a new strategy, scheduler, tick counter, or background thread.
- Load `AiPlayerbot.EconomyLifecycleEnabled` as a boolean with a default of enabled. Eligibility and one operation limits stay in `EconomyCycleAction`, so the strategy has no duplicate conditions.
- `AiFactory.cpp` lines 585 through 586 already install `NonCombatStrategy`, `loot`, and `gather` for non battleground bots. Preserve that activation path and do not modify default combat strategies or global action relevance.

**Definition of Done:**

- [ ] Eligible autonomous random bots receive periodic economy opportunities without a master command.
- [ ] Player controlled bots, bots with active masters, combat, death, teleport, and battleground states never perform economy operations.
- [ ] Disabling `AiPlayerbot.EconomyLifecycleEnabled` stops new travel, bids, listings, crafts, and internal mail collection.
- [ ] Existing gathering remains active and becomes the fallback when the policy returns no market or craft operation.
- [ ] TS-001 records that an autonomous personality selected gathering bot receives and completes a normal gathering target before and after an economy opportunity.
- [x] Verify: `build-playerbot-claude-tests/src/test/unit_tests --gtest_filter='PlayerbotPersonality*:*PlayerbotProfessionPreference*:*PlayerbotEconomyPolicy*'`

### Task 4: Add operator configuration and economy documentation

**Objective:** Document the lifecycle, safety limits, market behavior, and the exact optional role of `mod-ah-bot-plus`, as verified by TS-004.

**Files:**

- Modify: `modules/mod-playerbots/conf/playerbots.conf.dist`
- Modify: `modules/mod-playerbots/README.md`
- Modify: `modules/mod-playerbots/docs/personality.md`
- Create: `modules/mod-playerbots/docs/economy.md`

**Key Decisions / Notes:**

- Place `AiPlayerbot.EconomyLifecycleEnabled` with profession settings and document default enabled behavior.
- Explain the five phase order, the existing two stack profession reserve, the tradeskill budget, deterministic tie breaks, template price ceiling, one mutation per cycle, and normal core mail.
- State that playerbots work without `mod-ah-bot-plus`. When operators choose it, use regular non playerbot characters for `AuctionHouseBot.GUIDs`; `AuctionHouseBot.EnableSeller` provides optional reagent supply and `AuctionHouseBot.Buyer.Enabled` provides optional demand.
- Do not copy or reimplement `mod-ah-bot-plus` pricing logic, and do not promise that enabling it guarantees every playerbot auction sells.
- Keep the Claude boundary explicit. Dialogue output cannot select a phase, item, recipe, auction, price, destination, or mail operation.

**Definition of Done:**

- [x] Configuration documents the default and restart requirement without creating a second tuning surface.
- [x] Documentation distinguishes playerbot economic actors from optional market maker characters.
- [ ] TS-004 passes with the optional module absent before any optional liquidity run.
- [x] README and personality documentation describe exactly the behavior that exists after Tasks 1 through 3.
- [x] Verify: `git diff --check && git -C modules/mod-playerbots diff --check && git -C modules/mod-playerbot-claude diff --check`

### Task 5: Verify, synchronize plans, and pin the compatible playerbot revision

**Objective:** Run the complete available local evidence, commit the playerbot lifecycle with its approved plan, record that exact revision in the Claude module, and leave remote publication and deployment gated.

**Files:**

- Modify: `docs/plans/2026-07-29-personality-playerbot-economy-lifecycle.md`
- Create: `modules/mod-playerbots/docs/plans/2026-07-29-personality-playerbot-economy-lifecycle.md`
- Modify: `modules/mod-playerbot-claude/PLAYERBOTS_REVISION`
- Create: `modules/mod-playerbot-claude/docs/plans/2026-07-29-personality-playerbot-economy-lifecycle.md`

**Key Decisions / Notes:**

- Run the playerbot policy and existing personality suites, C++ codestyle, and `git diff --check` before marking source implementation `COMPLETE`.
- Keep the plan at `COMPLETE` when no authorized runtime contains the committed revision. Mark it `VERIFIED` only after a separately authorized build and installation or deployment runs TS-001 through TS-005 against that exact revision.
- Commit `mod-playerbots` first using the required commit workflow. Copy the final plan into that repository in the same commit batch.
- Write the resulting 40 character playerbot commit SHA to `mod-playerbot-claude/PLAYERBOTS_REVISION`, copy the final plan there, run the exact revision check, then commit the sidecar pin with the required commit workflow.
- Commit the root plan after its final status is `VERIFIED`.
- Do not push any repository, install binaries, alter deployed configuration, or install `mod-ah-bot-plus` without separate current permission.

**Definition of Done:**

- [x] The full configured `unit_tests` binary exits with zero failures after the incremental build.
- [x] C++ codestyle and `git diff --check` exit zero in every changed repository.
- [ ] TS-001 through TS-005 record the executable playerbot revision and effective economy setting. Without separate deployment permission, they remain blocked and the plan remains `COMPLETE`, not `VERIFIED`.
- [x] `PLAYERBOTS_REVISION` contains the exact committed `mod-playerbots` revision and the local equality check exits zero.
- [ ] No approved plan remains untracked in the repository whose code or pin it documents.
- [x] Verify: `test "$(tr -d '[:space:]' < modules/mod-playerbot-claude/PLAYERBOTS_REVISION)" = "$(git -C modules/mod-playerbots rev-parse HEAD)"`

## Verification Results

Source status remains `COMPLETE`. The installed Worldserver does not contain an authorized deployment of this revision, so the plan is not `VERIFIED`.

### Automated Evidence

| Check | Result | Evidence |
|---|---|---|
| Incremental `unit_tests` build | PASS | Exit 0 after compiling the policy, runtime action, guarded mail path, and action registration |
| Focused personality and economy suites | PASS | 20 tests passed from 5 suites |
| Full configured `unit_tests` binary | PASS | 11,450 tests ran, 5,964 passed, 5,486 skipped, zero failed, and 1 remained disabled |
| Changed C++ codestyle | PASS | The repository codestyle script exited 0 against every changed C++ source and header |
| Diff checks | PASS | Root, playerbot, and sidecar diff checks exited 0 |
| Changes review | PASS after fixes | The two stack reserve is enforced during selection and immediately before sale. Manual mail take retains its original upfront bag guard. |

The broad root codestyle invocation remains noisy outside this change. Existing core and generated Graphify files fail that scan. Running the same codestyle checks against the changed C++ files exits 0.

### E2E Results

| Scenario | Priority | Result | Notes |
|---|---|---|---|
| TS-001 | Critical | BLOCKED | Requires the committed revision installed in Worldserver and an observed gathering bot |
| TS-002 | Critical | BLOCKED | Requires a live Auction House, mailbox, and WoW client |
| TS-003 | Critical | BLOCKED | Requires a second character to buy a live bot listing |
| TS-004 | High | BLOCKED | Requires a live run without optional liquidity before any separate module run |
| TS-005 | Critical | BLOCKED | Static policy guards pass, but live state mutation proof requires the installed revision |

### Not Verified

| Not Verified | Reason |
|---|---|
| Executable identity and effective economy setting | No build installation or deployed configuration change was authorized |
| TS-001 through TS-005 | They require the committed playerbot revision in a running Worldserver and game client interaction |
| Optional `mod-ah-bot-plus` volume run | Installation and activation are explicitly outside this source pass |
| Root plan commit | The approved plan requires the root copy to remain uncommitted until its final status is `VERIFIED` |
