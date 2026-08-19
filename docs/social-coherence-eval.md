# Social Coherence Evaluation

How to tell whether a bot knows who it is talking to.

This document covers three things: the offline eval that runs in the test suite, the judged run
over seeded scenarios, and the live judged window that is the real acceptance step after a
deployment.

## Deployment pairing (read this first)

The social wire protocol is at schema 9 (it moved 7 to 8 for the addressee signals, then 8 to 9
so every offered memory carries the id a reply cites it by). The worldserver and the Python
sidecar are two separate processes, and the sidecar refuses any request whose schema version it
does not recognise.

**Both processes must be restarted from the same `mod-playerbots-llm` revision.** A worldserver
only redeploy leaves a running v8 sidecar refusing every v9 request. The failure is silent from
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

Two properties of the storage decide what a window can contain, and both were confirmed against
the live table rather than inferred from the schema:

- **Only `social.delivery` rows carry text.** `social.source` rows record that a line was heard
  and never store what it said (0 of 290549 rows on the live server). That is the privacy posture,
  not an oversight: a human's words are held in memory under consent and are never written here.
- **The speaker is `bot_actor_id`, not `actor_id`.** `actor_id` is null on every row that carries
  text. `target_actor_id` is the addressee where one exists.

So a live window judges **bot to bot** exchanges, where both halves are delivery rows linked by
`reply_to_event_public_id`. A thread a human started can only ever show the bot's side, and the
human's line is deliberately absent. Pick windows with bot conversation in them.

```sql
SELECT
    c.thread_public_id,
    cb.display_name                 AS bot_name,
    c.message_text                  AS bot_line,
    pb.display_name                 AS parent_speaker,
    p.message_text                  AS parent_line,
    tb.display_name                 AS addressee,
    ch.class                        AS bot_class,
    ch.level                        AS bot_level,
    UNIX_TIMESTAMP(c.occurred_at)   AS occurred_at
FROM playerbot_social_event c
LEFT JOIN playerbot_social_event p  ON p.public_id = c.reply_to_event_public_id
LEFT JOIN playerbot_social_actor cb ON cb.id = c.bot_actor_id
LEFT JOIN playerbot_social_actor pb ON pb.id = p.bot_actor_id
LEFT JOIN playerbot_social_actor tb ON tb.id = c.target_actor_id
LEFT JOIN acore_characters.characters ch ON ch.name = cb.display_name
WHERE c.event_type = 'social.delivery'
  AND c.outcome = 'delivered'
  AND c.message_text <> ''
  AND c.occurred_at BETWEEN FROM_UNIXTIME(:window_start) AND FROM_UNIXTIME(:window_end)
ORDER BY c.thread_public_id, c.occurred_at;
```

**What the export can and cannot reconstruct.** The full identity annotation is composed at
capture time from the live character and is never persisted, so the zone the speaker stood in
cannot be recovered. Class and current level can, from the characters table (the join above;
qualify the schema name to match the install). The addressee marker can too, from
`parent_speaker` (or `addressee` where the row carries a target). A window transcript therefore
looks like this, with a class and level bracket and the addressee marker, but without the zone:

```json
[
  {
    "thread_public_id": "thr_...",
    "bot_name": "Ghosademuhzo [Hunter 22]",
    "bot_line": "running beast mastery, my pet does most of the work",
    "lines": [
      "Ghosademuhzo [Hunter 22]: just got here, first time in ashenvale. anybody got tips for a level 22 hunter?",
      "Esdeline [Priest 24] (to Ghosademuhzo): stick to the edges, lots of elves here. what spec are you running?"
    ]
  }
]
```

`lines` is the transcript the bot answered, in order. `bot_line` is the generated line under
judgement. Annotate `bot_name` as well, so the judge knows the judged bot's class even when the
bot has no earlier line in the transcript.

**The class bracket is not optional.** The first live window (2026-08-18) was exported without
it, and the judge produced four false positives on 17 lines, including a spurious BLEED: it
inferred from conversational tone that a rogue was not a rogue and scored lock picking as
somebody else's ability. With the bracket present, the same window judged 20 of 20 lines clean.
Recovering the zone half of the annotation, or the human half of a thread, would need schema
changes this procedure deliberately does not make.

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
