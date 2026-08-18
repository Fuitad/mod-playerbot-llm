# Social Coherence Evaluation

How to tell whether a bot knows who it is talking to.

This document covers three things: the offline eval that runs in the test suite, the judged run
over seeded scenarios, and the live judged window that is the real acceptance step after a
deployment.

## Deployment pairing (read this first)

This change moves the social wire protocol from schema 7 to schema 8. The worldserver and the
Python sidecar are two separate processes, and the sidecar refuses any request whose schema
version it does not recognise.

**Both processes must be restarted from the same `mod-playerbots-llm` revision.** A worldserver
only redeploy leaves a running v7 sidecar refusing every v8 request. The failure is silent from
a player's point of view: the entire social feature goes quiet, and the only trace is
`provider_failed` telemetry.

The post deploy check is one delivered social line. Watch the worldserver log, or the
`playerbot_social_event` table, until a generated line lands. If nothing arrives within a few
minutes of normal activity, the two sides are on different revisions.

## Offline eval

No network, no credentials, no model. It replays the seeded scenarios in
`sidecar/tests/fixtures/social_coherence_scenarios.json` through prompt composition and checks
the structural properties a coherent generation depends on:

- the premise names who the bot is (race, class, zone)
- thread lines keep their identity annotations
- thread lines keep their addressee markers
- exactly one addressee guidance variant reached the prompt, and it is the declared one
- the notation rule that confines trust to the renderer authored line prefix is present

```zsh
cd modules/mod-playerbots-llm/sidecar
uv run python scripts/eval_social_coherence.py
```

Exit 0 means every scenario holds. Exit 1 lists each violation by scenario and check.

Each scenario declares what its prompt must come out saying, beside the request that produces
it. That is deliberate: a check that derived its expectation from the same request would pass by
construction and prove nothing.

## Judged run over the seeded scenarios

Structure alone cannot prove the addressee bleed is gone. Only a real generation that declines to
answer in somebody else's place can. This mode sends each scenario through the configured
provider and has a judge model score the reply.

Credentials follow the module's own convention: the sidecar reads
`MOD_PLAYERBOTS_LLM_ANTHROPIC_API_KEY`, and the global `ANTHROPIC_API_KEY` is deliberately
ignored. The eval reuses the provider adapter, so it inherits that lookup rather than
reimplementing it.

```zsh
cd modules/mod-playerbots-llm/sidecar
export MOD_PLAYERBOTS_LLM_ANTHROPIC_API_KEY=...
uv run python scripts/eval_social_coherence.py \
  --judge --scenario addressee_bleed --scenario human_to_other --samples 3
```

This is the acceptance run. It generates three real replies for each of the two regression
scenarios and passes only when the judge finds zero addressee perspective adoptions across all
six. Any `BLEED` line is a failure, and the judge's note says why.

Deliberate silence is often the right bystander answer and cannot adopt anybody's perspective,
so it never counts as a bleed. It is counted and printed separately all the same, because a
change that simply muted every bot would otherwise score a flawless run.

The two scenarios are the 2026-08-12 regression from both directions:

- `addressee_bleed`: the thread carries a `(to Sweatyguest)` marker, so the bot can see the
  question already has an addressee.
- `human_to_other`: a human names their addressee inside the message text, with no marker, so
  only the trusted `addressed_to_bot` signal distinguishes the two cases.

## Live judged window

Run after a deployment, over real conversation.

### 1. Choose a window

Pick a period of ordinary activity. An hour of a busy evening is enough to produce a few dozen
generated lines. Note the start and end as Unix timestamps.

### 2. Export it

`playerbot_social_event` already stores the transcript and the reply linkage
(`reply_to_event_public_id`). No schema change is required, and none is made; what that costs the
export is spelled out below the query.

```sql
SELECT
    e.thread_public_id,
    e.public_id            AS event_public_id,
    e.reply_to_event_public_id,
    e.event_type,
    e.outcome,
    e.channel,
    a.display_name         AS speaker_name,
    a.actor_kind,
    e.message_text,
    UNIX_TIMESTAMP(e.occurred_at) AS occurred_at
FROM playerbot_social_event e
LEFT JOIN playerbot_social_actor a ON a.id = e.actor_id
WHERE e.occurred_at BETWEEN FROM_UNIXTIME(:window_start) AND FROM_UNIXTIME(:window_end)
ORDER BY e.thread_public_id, e.occurred_at;
```

The generated lines being judged are the rows with `event_type = 'social.delivery'` and
`outcome = 'delivered'`; everything else in the same `thread_public_id` is the transcript they
answered. `actor_kind` is `'bot'` or `'player'`. A row's `reply_to_event_public_id` points at the
`event_public_id` of the line it answered, which is how the transcript recovers who addressed
whom.

**What the export can and cannot reconstruct.** The identity annotations are composed at capture
time from the live character and are never persisted, so `[Troll Rogue 23, Durotar]` cannot be
recovered from storage. The addressee marker can: follow `reply_to_event_public_id` to the parent
row and take its `speaker_name`. So a live window transcript looks like this, with the addressee
marker but without the identity bracket:

```json
[
  {
    "thread_public_id": "thr_...",
    "bot_name": "Dodokkuli",
    "bot_line": "mostly questing, the mobs here are decent xp",
    "lines": [
      "Sweatyguest: just got here",
      "Klara (to Sweatyguest): you leveling through here or just grinding?"
    ]
  }
]
```

`lines` is the transcript the bot answered, in order. `bot_line` is the generated line under
judgement. The judge scores addressee behaviour, which is what the persisted reply linkage
supports; class consistency is judged only from what the lines themselves reveal, because the
class facts the prompt saw are not in the table. Recovering the full annotation would need the
identity columns persisted on `playerbot_social_event`, which is a schema change this procedure
deliberately does not make.

### 3. Judge it

```zsh
cd modules/mod-playerbots-llm/sidecar
export MOD_PLAYERBOTS_LLM_ANTHROPIC_API_KEY=...
uv run python scripts/eval_social_coherence.py --window /path/to/window.json
```

### 4. Read the report

Every line is marked `ok`, `FAIL`, or `BLEED`. `BLEED` is an addressee perspective adoption;
`FAIL` is any other rubric property the verdict did not satisfy, and the failing property names
are printed under the line. The run exits nonzero if any line failed any rubric property. A
verdict missing a property counts as a failure of it: an unanswered judgement has cleared
nothing.

## The rubric

The judge scores four properties of one line against the transcript it answered.

| Property | What it means |
|---|---|
| `adopted_addressee_perspective` | The line answers a question the transcript shows was addressed to somebody else, or speaks about that player's situation in the first person. This is the failure being measured. |
| `answered_what_was_asked` | If the transcript shows a question was asked of this bot, the line answers what was actually asked. If no question was asked of it, this is true. The property is only about answering; whether the bot should have spoken at all belongs to `adopted_addressee_perspective`. |
| `class_consistent` | Nothing in the line assumes the bot, or the person it answers, has abilities their class does not have. |
| `stayed_on_subject` | The line engages with what the transcript established rather than starting an unrelated topic. |

The judge is told to report `adopted_addressee_perspective` as true when uncertain, so the
acceptance bar fails closed.
