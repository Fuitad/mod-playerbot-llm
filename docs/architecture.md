# Architecture

Three processes, two trust boundaries, one direction of trust: the game never trusts the model, and the machine never trusts the network.

```
worldserver (C++)                     sidecar (Python)              Anthropic API
+---------------------+   loopback    +--------------------+   TLS  +-----------+
| world thread        |     TCP       | asyncio server     | -----> | Claude    |
|  ClaudeChatScripts  | <-----------> |  protocol.py       | <----- | Haiku 4.5 |
| bridge worker       |  length-      |  claude.py         |        +-----------+
|  ClaudeChat/Bridge  |  prefixed     |  storage.py (SQLite)|
+---------------------+  JSON frames  +--------------------+
```

## Worldserver side (C++)

All game state lives on the world thread. The bridge worker thread never touches it.

- `ClaudeChatScripts.cpp` runs only on the world thread. It observes whispers to bots (any whisper the playerbot command system does not recognize becomes conversation; the `llm ` prefix forces Claude even for command-shaped text), `llm` prefixed party chat, and quest, level, and rare loot milestones, then copies everything the sidecar needs into an immutable `ChatRequest` value snapshot (names, personality numbers, message text). It also owns the ambient cadence. A due slot first requires a connected human, then selects an online, alive, noncombat machine bot with a usable World channel through deterministic personality weighting. Command recognition uses `ExternalEventHelper::IsChatCommand`, a non-executing mirror of mod-playerbots' own command resolution. No pointers cross the thread boundary.
- `ClaudeChat.cpp` runs the bridge worker (`std::jthread`). It owns the socket, serializes requests, and parses responses. Queues in both directions are bounded (`QueueSize`); a full queue rejects new work immediately.
- Delivery happens back on the world thread during world update. The bot GUID is re-resolved through `ObjectAccessor` immediately before speaking; if the bot is gone, offline, or the deadline (`ResponseDeadlineMs`) has passed, the response is dropped. World delivery also requires a human to remain connected, an alive bot outside combat, and a current World channel. `SayToWorld` failure is inspected and produces no fallback text.
- Ambient mode requires `AiPlayerbot.EnableBroadcasts = 0`. When canned broadcasts remain enabled, only ambient scheduling is disabled. Whisper, party, and milestone Claude behavior remains available.
- The model cannot act. Responses carry exactly one field, `message`, and the only thing the module ever does with it is have the bot say it.

## Wire protocol (loopback TCP)

- Frame: 4-byte network order length prefix, then a UTF-8 JSON payload. Payloads above 64 KiB are rejected.
- Payloads are strict flat JSON objects: schema version 2, no nesting, no arrays, no booleans, no null, no floats, no duplicate keys, no trailing bytes. Both sides validate independently (`ClaudeChat.cpp` and `protocol.py`); the Python and C++ test suites share byte-for-byte fixtures so the implementations cannot drift.
- Ambient uses channel `world` and event kind `4`. Its trusted combination requires speaker GUID `0`, an empty speaker name, subject ID `0`, and marker `ambient_world`. Direct requests reject the empty speaker identity.
- Every payload carries the bridge token from `PLAYERBOT_CLAUDE_BRIDGE_TOKEN`. Both sides compare it in constant time. A mismatch closes the connection without revealing the expected value. Both processes fail closed at startup when the token is missing or shorter than 32 bytes.
- The socket binds to 127.0.0.1 only. The token exists to stop other local processes from injecting bot speech or draining budget.

## Sidecar (Python)

- `app.py` serializes all budget bookkeeping behind one lock, so socket concurrency can never race the ledger: record profile, count tokens, reserve, generate, settle, append memory. Ambient acceptance is persisted before token counting, so provider or counting failure still consumes the conservative rolling hourly slot.
- The entire pipeline (queueing included) runs under an end-to-end `ResponseDeadlineMs` deadline, and the SDK client's own timeout is capped at the same value. A request that cannot finish in time is dropped silently; its reservation stays charged at maximum, and the dead exchange never enters conversation memory.
- One residual overlap exists by design: a deadline cancellation releases the lock while the abandoned synchronous SDK call finishes in its worker thread (Python cannot interrupt it; the capped client timeout bounds it). This is safe because httpx clients are thread-safe and an abandoned call can never settle or write memory, but it does mean a new provider call may start while the abandoned one is still physically in flight.
- `claude.py` is the only file that talks to Anthropic. The trusted personality profile is rendered into the system prompt; the player's text is delivered as a separate, explicitly untrusted user message. The model gets no tools. Structured output (`output_format`) restricts the reply to a single `message` field, and the adapter additionally enforces non-empty, single-line, at most 240 UTF-8 bytes.
- Ambient generation receives only bot identity and personality plus a fixed observation instruction. The adapter discards any supplied history, and `app.py` neither reads nor appends conversation turns for ambient requests.
- The API key is read from `MOD_PLAYERBOT_CLAUDE_APIKEY` only and passed to the SDK explicitly. The SDK's implicit `ANTHROPIC_API_KEY` fallback is disabled by construction, so a machine-wide key can never be used by this module.

## Budget ledger

All money is integer nano-USD (1 USD = 1,000,000,000 nano), so documented examples reproduce exactly and no float rounding can leak into the ledger.

Cost formula: `(input_tokens * 1.00 + output_tokens * 5.00) / 1,000,000` USD at the default Haiku 4.5 rates. Tested examples (see `test_documented_cost_examples_are_exact`):

- `2,500` input plus `80` output tokens: `2,900,000` nano, rendered `0.0029` USD
- `4,095` input plus `96` output tokens: `4,575,000` nano, rendered `0.004575` USD

The reserve-then-settle cycle, one SQLite WAL transaction per step:

1. **Count.** The exact prompt is counted via the API, with the same structured output schema the generation request sends (the schema is billed as input). Input above 4,095 tokens is rejected before any money moves.
2. **Reserve.** The maximum possible cost (counted input plus 96 output tokens) is charged. If rolling settled actual cost plus rolling unsettled maximum cost plus this maximum would exceed `DailyBudgetUsd`, no reservation is created and no provider call happens.
3. **Submit.** The reservation is marked submitted before the provider call.
4. **Settle.** After a successful reply, actual usage atomically replaces the maximum charge and an append-only usage row records tokens, price snapshot, and cost.

A crash or provider failure at any point leaves the reservation charged at its maximum. Every request is attributed to reservation creation time. Settled actual cost and unsettled maximum cost leave the active window exactly 24 hours later, but no ledger row is deleted. `DailyBudgetUsd` accepts values through `5`; missing, zero, negative, or larger values disable all generation. SQLite `BEGIN IMMEDIATE` transactions prevent concurrent processes from admitting reservations beyond the rolling ceiling.

The ambient rate gate uses the same transaction strategy. It stores each accepted attempt before token counting, retains active attempts across restart, rejects above the configured limit from `1` through `6`, and removes attempts at the exact one hour boundary.

## Storage schema

| Table | Contents | Bound |
| --- | --- | --- |
| `profiles` | Latest observed trusted profile per bot | One row per bot |
| `conversation_turns` | Rolling dialogue memory | 20 turns per bot, older rows deleted |
| `reservations` | Budget reservations with state `reserved`, `submitted`, or `settled` | Append and update |
| `usage_log` | Actual usage with price snapshots | Append only |
| `ambient_attempts` | Accepted ambient attempt timestamps | Rolling one hour |

## Failure modes

Every failure ends in bot silence. There is no fallback text anywhere in the pipeline.

| Failure | Behavior |
| --- | --- |
| Sidecar not running | Bridge reconnects with backoff; pending requests expire |
| Token mismatch | Connection closed; nothing processed |
| Malformed frame or JSON | Connection closed |
| Prompt above 4,095 tokens | Dropped before reservation |
| Budget exhausted | Dropped before the provider call |
| Ambient hourly rate exhausted | Dropped before token counting |
| No connected human or eligible World bot | No ambient request is enqueued |
| Provider timeout, auth, rate limit, or error | Dropped; reservation stays charged at maximum |
| Sidecar pipeline exceeds `ResponseDeadlineMs` | Dropped; reservation stays charged at maximum |
| Model output invalid (empty, multiline, above 240 bytes, wrong schema) | Dropped |
| Bot despawned or deadline passed | Response discarded at delivery |
| Last human disconnects during generation | Accepted attempt remains charged; response is discarded |
