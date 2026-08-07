# Architecture

Three processes, two trust boundaries, one direction of trust: the game validates every model result, and the machine never trusts the network.

```
worldserver (C++)                     sidecar (Python)              Anthropic API
+---------------------+   loopback    +--------------------+   TLS  +-----------+
| world thread        |     TCP       | asyncio server     | -----> | provider  |
|  PlayerbotLLMScripts| <-----------> |  protocol.py       | <----- | adapter   |
| bridge worker       |  length-      |  generation.py     |        +-----------+
|  PlayerbotLLM/Bridge|  prefixed     |  budget.py         |
+---------------------+  JSON frames  |  ledger.py         |
                                      |  store.py          |
                                      |  schema.py         |
                                      |  state.py          |
                                      +---------+----------+
                                                |
                                                v
                                      +--------------------+
                                      | MySQL              |
                                      |  acore_playerbots  |
                                      +--------------------+
```

The sidecar shares the worldserver's Playerbots database rather than keeping a private
file. That is what makes the budget hold across a sidecar restart and across two
sidecars pointed at one realm, and it removes a second thing to back up. Connection
settings are read from the deployed `playerbots.conf` through `--playerbots-config`,
never duplicated into the sidecar's own configuration.

## Worldserver side (C++)

All game state lives on the world thread. The bridge worker thread never touches it.

- `PlayerbotLLMScripts.cpp` runs only on the world thread. It observes supported chat and milestone events, owns the legacy ambient cadence, and implements the optional playerbot career and social providers. Chat requests contain immutable names, personality values, and text. Career requests contain immutable personality values plus opaque legal candidates. No pointers cross the thread boundary.
- `PlayerbotLLM.cpp` runs the bridge worker (`std::jthread`). It owns the socket, serializes requests, and parses responses. Queues in both directions are bounded (`QueueSize`); a full queue rejects new work immediately.
- Delivery happens back on the world thread during world update. The bot GUID is re-resolved through `ObjectAccessor` immediately before speaking; if the bot is gone, offline, or the deadline (`ResponseDeadlineMs`) has passed, the response is dropped. World delivery also requires a human to remain connected, an alive bot outside combat, and a current World channel. `SayToWorld` failure is inspected and produces no fallback text.
- Which mode is in force is decided by `AiPlayerbot.SocialChat.Enable`. While it is on, this module registers with the coordinator and the legacy whisper, party, and ambient hooks stand down, so one message can never produce two answers chosen by two different rules. `PlayerbotLLM.AmbientWorldEnable` is then reported as ignored in the log rather than silently skipped.
- The gate is read at two different cadences on purpose. Provider registration reads it once at startup, because `SetSocialProvider` is the worldserver's registration seam and a provider that appeared and vanished as an operator toggled a config would leave outstanding requests pointing at nothing; turning the feature on or off therefore takes effect at the next startup. The legacy hooks and the ambient limiter re-read it every tick, so a gate that becomes live-controllable silences them immediately rather than at the next restart. Both directions fail toward silence.
- Legacy ambient mode, for a server with the social gate off, additionally requires `AiPlayerbot.EnableBroadcasts = 0`. When canned broadcasts remain enabled, only ambient scheduling is disabled. Whisper, party, and milestone generation remains available.
- The model cannot act. Chat responses contain one `message`. Career responses contain one candidate token and one spending style. Playerbot code validates the response against its pending request and legal candidate set before persisting it.

## Wire protocol (loopback TCP)

- Frame: 4-byte network order length prefix, then a UTF-8 JSON payload. Payloads above 64 KiB are rejected.
- Payloads use strict schema version 5 JSON; every request kind currently declares the same version, and a kind is versioned by what its own payload requires rather than by the others. Chat payloads remain flat. Career payloads use a bounded nested candidate list because one request must carry the complete opaque legal set, and roleplay assessment requests use a bounded thread line list for the same reason. Assessment responses stay flat (a `capability_count` plus numbered `capability_N` slots rather than an array) because the worldserver's response parser reads only flat objects. Both sides reject unknown fields, duplicate keys, invalid types, raw game identifiers, oversized content, and trailing bytes.
- Career uses channel `career` and event kind `5`. It never enters conversation history. A valid reply must preserve the request correlation and supported versions, select an offered opaque token, and choose a spending style no broader than that candidate permits.
- Social uses channel `social` and declares `kind: "social"`. A social request carries the coordinator's correlation token, a bot actor and an optional subject actor in one shared field shape differing only in a `human` flag, the channel the line should be spoken on, an opaque thread identity, and a bounded context. A social response echoes the correlation token and bot identity, which are checked before the message is read, so a well formed answer to a different request is refused rather than delivered. It may instead set `regenerate`, which carries no message and is honoured at most once per request.
- Roleplay assessment uses `kind: "roleplay_assessment"`. The request carries its own correlation token, the channel, the opaque thread identity, the current line, and a bounded list of recent thread lines. The response echoes the token and reports one of six assessment kinds (ordinary, roleplay_invitation, roleplay_continuation, practical, opt_out, uncertain) plus the content capabilities the premise depends on, with a per kind cardinality contract enforced identically on both sides. The worldserver treats the answer as evidence only: it unions the reported capabilities with its own scan of the same text and applies its progression policy, so a capability omitted here cannot authorize anything.
- The structured social context carries two required trusted fields the worldserver chose: `prompt_mode` (`ordinary`, `decline_roleplay`, `acknowledge_roleplay`, `authorized_roleplay`) and `active_expansion` (`0` classic, `1` burning crusade, `2` wrath). The bridge refuses to serialize a request whose mode or expansion is outside those sets, and the sidecar refuses a structured context without them and keeps the ordinary prompt, so no untrusted string can select a mode. Only `authorized_roleplay` lifts the ordinary player voice, an authorized context may not carry fictional player identity fields, and the worldserver rescans the finished authorized line before delivery, refusing it when it needs later expansion content or the bot has entered combat.
- Response kind is declared rather than inferred from shape. Career and social answers travel the same socket, and the chat and social payloads are additionally mutually exclusive by field count, so a career decision cannot arrive as a line to speak.
- Every string bound in this protocol is a UTF-8 BYTE budget, enforced on both sides. Pydantic's `max_length` counts characters, so each bounded string carries an explicit byte validator beside it.
- `SocialExchange` owns one social request from submission to verdict, and `PlayerbotLLMState` implements the worldserver's `PlayerbotSocialProvider` interface and registers itself through `SetSocialProvider` while the gate is on. It deregisters on shutdown, so the coordinator never holds a provider whose bridge has gone.
- Ambient uses channel `world` and event kind `4`. Its trusted combination requires speaker GUID `0`, an empty speaker name, subject ID `0`, and marker `ambient_world`. Direct requests reject the empty speaker identity.
- Every payload carries the bridge token from `PLAYERBOT_LLM_BRIDGE_TOKEN`. Both sides compare it in constant time. A mismatch closes the connection without revealing the expected value. Both processes fail closed at startup when the token is missing or shorter than 32 bytes.
- The socket binds to 127.0.0.1 only. The token exists to stop other local processes from injecting bot speech or draining budget.

## Sidecar (Python)

- `app.py` serializes all budget bookkeeping behind one lock, so socket concurrency can never race the ledger: record profile, count tokens, reserve, generate, settle, append memory. Ambient acceptance is persisted before token counting, so provider or counting failure still consumes the conservative rolling hourly slot.
- The entire pipeline (queueing included) runs under an end-to-end `ResponseDeadlineMs` deadline, and the SDK client's own timeout is capped at the same value. A request that cannot finish in time is dropped silently; its reservation stays charged at maximum, and the dead exchange never enters conversation memory.
- One residual overlap exists by design: a deadline cancellation releases the lock while the abandoned synchronous SDK call finishes in its worker thread (Python cannot interrupt it; the capped client timeout bounds it). This is safe because httpx clients are thread-safe and an abandoned call can never settle or write memory, but it does mean a new provider call may start while the abandoned one is still physically in flight.
- `providers/anthropic.py` is the current adapter and the only file that talks to Anthropic. The model gets no tools. Chat uses structured output with one bounded `message`. Career selection uses a separate structured output schema with one offered token and one allowed spending style.
- Career generation receives no conversation history or human text. The prompt describes only the personality and opaque candidate properties. `app.py` records a diagnostic decision after validation but never makes it authoritative.
- Ambient generation receives only bot identity and personality plus a fixed observation instruction. The adapter discards any supplied history, and `app.py` neither reads nor appends conversation turns for ambient requests.
- Roleplay assessment is classification only: the prompt forbids chatting or roleplaying, the conversation text is fenced as untrusted data, and the structured output is the six kind vocabulary plus capabilities. It is admitted under the same ledger as everything else, as classification work carrying the human priority of the reply it precedes. Every failure path (admission, provider, deadline, malformed output) answers with silence, and the worldserver's own timeout then resumes ordinary social behavior.
- The API key is read from `MOD_PLAYERBOT_LLM_ANTHROPIC_API_KEY` only by the Anthropic adapter and passed to the SDK explicitly. The SDK's implicit `ANTHROPIC_API_KEY` fallback is disabled by construction, so a machine-wide key can never be used by this module.

## Budget ledger

All arithmetic is in integer nano-USD (1 USD = 1,000,000,000 nano), so documented examples reproduce exactly and no float rounding can leak into the ledger. Storage is decimal dollars: the `mod-playerbots` schema records money in `DECIMAL(12, 6)` columns, so an amount is rounded UP to the nearest millionth of a dollar before it is admitted, not on the way to the database. Rounding at the write instead would leave the running totals disagreeing with the rows they are meant to be the sum of, by up to 999 nano a row, permanently.

Cost formula: `(input_tokens * 1.00 + output_tokens * 5.00) / 1,000,000` USD at the default Haiku 4.5 rates, rounded up. Tested examples:

- `2,500` input plus `80` output tokens: `2,900,000` nano, rendered `0.0029` USD
- `4,095` input plus `96` output tokens: `4,575,000` nano, rendered `0.004575` USD

The decisions and the storage are deliberately separate. `budget.py` holds every rule as a pure function of its arguments: no clock, no database, no network. `ledger.py` holds only what genuinely needs a database. A rule embedded in a transaction is a rule the tests can reach only through a transaction, and the arithmetic is the part that has to be right.

The storage half is three modules rather than one, along the seams that were already there:

| Module | Holds |
| --- | --- |
| `schema.py` | The sidecar's own DDL, both ownership lists, the startup guard, the named lock, and the UTC day |
| `ledger.py` | The budget: admission, settlement, release, the reservation identity, and the circuit breaker |
| `store.py` | Profiles, dialogue memory, career decisions, and the legacy ambient rate |

`ledger.py` and `store.py` both import `schema.py`; neither imports the other. They share a connection and the lock table and nothing else, which is why they are separate: money has a ceiling, a breaker, and a schema it does not own, and none of that should be in the way of reading how conversation memory is trimmed.

The reserve-then-settle cycle, one MySQL transaction per step:

1. **Count.** The exact prompt is counted via the API, with the same structured output schema the generation request sends (the schema is billed as input). Input above 4,095 tokens is rejected before any money moves.
2. **Reserve.** The maximum possible cost (counted input plus the full 96 token output allowance, rounded up) is charged. Reading the day's totals, applying the admission policy, and inserting the reservation all happen inside one transaction holding the `budget_day` named lock, so two concurrent requests cannot both see the same remaining budget and both fit.
3. **Settle.** After a successful reply, the reported usage replaces the maximum. If the reported cost exceeds what was reserved, the circuit breaker opens, the true figure is recorded, and every later request is denied.

Admission is refused, in this order, for an open circuit, unknown pricing, the total ceiling, and then the human reserve. The reserve denies background work only: `HumanBudgetReserveRatio` protects a share of the ceiling for whispers, party lines, and direct or mixed social replies. Ambient World chatter, career selection, bot only social continuations, and starters are background. Human work is protected from background work; it is not exempt from the ceiling.

The ceiling is per UTC calendar day, so it rolls over at one instant regardless of server timezone or daylight saving. The configured `DailyBudgetUsd` is the only ceiling; no policy maximum sits above it. It is refused above what `DECIMAL(12, 6)` can record, 999999.999999 USD, because a ceiling the ledger cannot enforce would let honest traffic saturate the day's spent total. That refusal is what makes saturation a reliable integrity signal rather than an ordinary outcome.

The breaker itself does not live on the day. It is on the singleton `playerbot_social_runtime_control` row, which is what stops it reopening by itself at UTC rollover: a provider reporting an impossible cost is not a fact about one calendar day. The row is created by the breaker if it is not already there, so a realm that has never tripped it has no row rather than an unknown state.

Failure handling depends on what can be proven:

- An authentication or rate limit failure was rejected before generation, so the reservation is released immediately.
- Invalid output arrives with the provider's own usage attached, because the adapter reads usage before it validates content. The completion was generated and billed, so the reservation settles at that exact cost. Releasing would spend money the ledger never records; charging the maximum would overcharge the realm permanently for a reply that was merely unusable.
- A timeout or a provider error carries no usage and nothing can be concluded, so the reservation is left alone for expiry.
- A crash or a cancelled deadline tells the sidecar nothing at all, and is treated the same way.

Anything left outstanding stays charged at its maximum until another transaction reclaims it, ten minutes after creation. That holds the money against the ceiling while the request might still matter and stops guessing once it cannot. A completion arriving after the reclaim is refused rather than charged twice, because `settle` only accepts a reservation still in the reserved state.

The legacy ambient rate gate uses the same strategy. It stores each accepted attempt before token counting, so a failing provider cannot be retried without limit, retains active attempts across restart, rejects above the configured limit from `1` through `6`, and removes attempts at the exact one hour boundary. Nothing reaches it while the social gate is on, because the limiter is never configured alongside the coordinator.

The two sides disagree on that ceiling, and the sidecar is the one that decides. `MAX_AMBIENT_MESSAGES_PER_HOUR` is `60` in `PlayerbotLLM.h` and `6` in `store.py`, so worldserver will schedule attempts at a configured rate the sidecar then declines one by one. The effect is silence rather than overspend, which is why it is documented as an effective ceiling of `6` rather than repaired here: changing either constant is a behaviour change to the legacy path and does not belong in a documentation synchronization.

## Storage schema

All in `acore_playerbots`. Nothing migrates from the removed SQLite file.

Two owners, and the split matters. The sidecar creates the five tables it alone uses. The budget tables and the runtime control row belong to the `mod-playerbots` SQL revisions under `data/sql/playerbots/updates`, and the sidecar refuses to start when they are absent rather than creating its own version of them.

| Table | Contents | Bound | Created by |
| --- | --- | --- | --- |
| `playerbot_llm_profile` | Latest observed trusted profile per bot | One row per bot | sidecar |
| `playerbot_llm_conversation_turn` | Rolling dialogue memory | 12 turns per bot, trimmed on write | sidecar |
| `playerbot_llm_ambient_attempt` | Accepted ambient attempt timestamps | Rolling one hour | sidecar |
| `playerbot_llm_career_decision` | Validated opaque career response diagnostics | Latest row per bot | sidecar |
| `playerbot_llm_lock` | Named serialization points | Bounded key set, never deleted | sidecar |
| `playerbot_llm_daily_budget` | Reserved and spent decimal totals | One row per UTC day | mod-playerbots |
| `playerbot_llm_budget_reservation` | One row per attempt, with request kind, model, maximum, and settled cost | Unique on the minted `req_` public id | mod-playerbots |
| `playerbot_social_runtime_control` | Operator controls, including the budget circuit breaker | Singleton row | mod-playerbots |

That split is not tidiness. Both definitions used `CREATE TABLE IF NOT EXISTS`, so whichever ran first won and the other silently did nothing, and on a deployed realm that was the module. Every write the sidecar made to a column only its own definition had would have failed at runtime and nowhere else. The sidecar's ledger tests apply the module's revision files rather than a copy of them, so a column renamed there fails in the run that renames it.

Conversation memory is trimmed on write rather than on read, so the table is bounded on disk and not merely in what a query returns.

A reservation's identity is minted by the sidecar as `req_` followed by 32 lowercase hex, and that is the unique key. Nothing about the caller has to be unique for a repeat to get its own row, which is the point: the worldserver's request ids come from a per-process counter that restarts at 1, so a key derived from one collides with whatever the previous run left behind.

`priority_lane` on that row reads `unspecified`. The lane is decided on the worldserver side, collapsed to a queue priority, and discarded before the request is encoded, so it never crosses the bridge and the sidecar cannot know it. That is an accurate statement that the row's producer does not know, not a guess, and it matches how the plan already treats the other producer facts in the same telemetry contract. The truthful lane arrives when a producer for it lands. `model` beside it is written truthfully, because the sidecar is the process that chooses the model.

`reserved_usd` on the daily row is derived rather than incremented: every admission, settlement, release, and expiry sweep rewrites it from the reservations themselves, inside the day lock. Three separate increments and decrements that must each be right is three chances to be wrong.

## Failure modes

Every failure ends in bot silence. There is no fallback text anywhere in the pipeline.

| Failure | Behavior |
| --- | --- |
| Sidecar not running | Bridge reconnects with backoff; pending requests expire |
| Token mismatch | Connection closed; nothing processed |
| Malformed frame or JSON | Connection closed |
| Prompt above 4,095 tokens | Dropped before reservation |
| Budget exhausted | Dropped before the provider call |
| Ambient hourly rate exhausted (legacy mode) | Dropped before token counting |
| No connected human or eligible World bot (legacy mode) | No ambient request is enqueued |
| Social gate on, so a legacy hook fires | Yielded to the coordinator; nothing is enqueued here |
| Social gate on but this module is absent or its bridge is down | The coordinator has no provider and the bot stays silent; functional chat is unaffected |
| Provider auth or rate limit refusal | Dropped; reservation released, since nothing was generated |
| Provider timeout or error | Dropped; reservation left outstanding for expiry, since billing cannot be determined |
| Sidecar pipeline exceeds `ResponseDeadlineMs` | Dropped; reservation left outstanding for expiry reclaim |
| Model output invalid (empty, multiline, above 240 bytes) | Dropped; reservation settled at the cost the provider reported |
| Model output unparseable | Dropped; no usage came back, so left outstanding for expiry |
| Completion cannot be priced | Dropped; reservation stays outstanding rather than settling as free |
| Provider reports more than it reserved | Circuit breaker opens, the reported figure recorded verbatim |
| Day total would exceed what the column holds | Total saturates, circuit breaker opens naming the discarded amount |
| Circuit breaker open | Every request denied until it is cleared |
| Playerbots database unreachable | Sidecar refuses to start; `doctor` reports it and exits nonzero |
| Bot despawned or deadline passed | Response discarded at delivery |
| Last human disconnects during generation | Accepted attempt remains charged; response is discarded |
| Career provider disabled, busy, invalid, or late | Playerbot persists its deterministic fallback |
| Roleplay assessment refused, late, or malformed | The worldserver proceeds with ordinary social activation |
| Corrupt prompt authority on a social request | Refused on the worldserver side before enqueue; a structured context arriving without authority keeps the ordinary prompt |
| Authorized line needs later expansion content, or the bot entered combat | Refused at delivery by the worldserver; the bot stays silent for that line |
