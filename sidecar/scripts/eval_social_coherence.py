#!/usr/bin/env python
"""Coherence evaluation for the social prompt.

Three modes, in increasing cost and decreasing determinism.

Offline (the default) replays the seeded scenarios through prompt composition and checks the
structural properties a coherent generation depends on: that the bot's premise names who it is,
that thread lines keep their identity annotations and addressee markers, and that exactly the
right addressee guidance reached the prompt. No network, no key, no model. This is what runs in
the suite, and it is what catches a prompt change that quietly drops a guarantee.

Judge mode (``--judge``) generates real replies through the configured provider and has a judge
model score each one against the coherence rubric. Structure alone cannot prove the addressee
bleed is gone: only a real generation that declines to answer in somebody else's place can.

Window mode (``--window FILE``) judges an exported window of real conversation from a live
server, which is the acceptance step this change is finally measured by. The export SQL and the
procedure live in ``docs/social-coherence-eval.md``.

Credentials follow the module's own convention: the sidecar reads
``MOD_PLAYERBOTS_LLM_ANTHROPIC_API_KEY`` and deliberately ignores the global
``ANTHROPIC_API_KEY``. Nothing here reimplements that lookup; the provider adapter is reused so
it cannot drift.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SIDECAR_ROOT = Path(__file__).resolve().parents[1]
if str(_SIDECAR_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_SIDECAR_ROOT / "src"))

from playerbots_llm import generation, protocol  # noqa: E402
from playerbots_llm.providers import anthropic as anthropic_provider  # noqa: E402

SCENARIO_PATH = _SIDECAR_ROOT / "tests" / "fixtures" / "social_coherence_scenarios.json"

# The scenarios that reproduce the 2026-08-12 bleed. A judged run that does not cover both is
# not an acceptance run, whatever else it covered.
REGRESSION_SCENARIOS = ("addressee_bleed", "human_to_other")

# The one phrase per addressee variant that identifies it in a composed prompt. Deliberately a
# fragment of the rule rather than the whole rule: this has to survive ordinary rewording and
# still fail when a variant goes missing or two of them reach one prompt.
ADDRESSEE_VARIANT_PHRASES = {
    "direct": "asks YOU a question directly",
    "bystander": "you are a bystander",
    "unasked": "asked no question",
}


@dataclass
class Violation:
    scenario: str
    check: str
    detail: str


@dataclass
class ScenarioReport:
    scenario: str
    checks: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)


def load_scenarios(path: Path = SCENARIO_PATH) -> dict[str, dict[str, Any]]:
    """Every seeded scenario, as a request payload plus what its prompt must come out saying.

    The expectation is declared beside the request rather than derived from it. Deriving it
    would make each check compare composition against itself, which passes by construction and
    proves nothing: the point is to notice when the two disagree.
    """

    return json.loads(path.read_text(encoding="utf-8"))


def build_request(payload: dict[str, Any]) -> protocol.SocialRequest:
    return protocol.parse_social_request(json.dumps(payload).encode("utf-8"), payload["token"])


def check_scenario(name: str, scenario: dict[str, Any]) -> ScenarioReport:
    """Every structural property a coherent generation rests on, for one scenario."""

    report = ScenarioReport(scenario=name)
    expect = scenario["expect"]
    request = build_request(scenario["request"])
    system = generation.build_social_system_prompt(request)
    user = generation.build_social_user_message(request)

    def fail(check: str, detail: str) -> None:
        report.violations.append(Violation(name, check, detail))

    # 1. The bot knows who it is. Without this the premise is a name and a level, and a bot
    #    asked what it plays will invent an answer.
    if expect["premise_contains"]:
        report.checks.append("premise_identity")
        for fragment in expect["premise_contains"]:
            if fragment not in system:
                fail("premise_identity", f"the premise does not say {fragment!r}")

    # 2. Thread identity and addressee markers survive composition. The worldserver renders
    #    these; the only way they reach the model is verbatim inside the fenced THREAD section.
    if expect["thread_contains"]:
        report.checks.append("thread_annotations")
        for line in expect["thread_contains"]:
            if line not in user:
                fail("thread_annotations", f"thread line missing from the prompt: {line!r}")

        addressed = [line for line in expect["thread_contains"] if "(to " in line]
        if addressed:
            report.checks.append("addressee_markers")
            for line in addressed:
                marker = line[line.index("(to ") : line.index(")", line.index("(to ")) + 1]
                if marker not in user:
                    fail("addressee_markers", f"addressee marker missing: {marker!r}")

    # 3. Exactly one addressee variant, and the declared one. Two would let the model choose
    #    which rule to follow, and none would put it back where the regression happened.
    report.checks.append("addressee_variant")
    expected = ADDRESSEE_VARIANT_PHRASES[expect["addressee_variant"]]
    present = [phrase for phrase in ADDRESSEE_VARIANT_PHRASES.values() if phrase in system]
    if present != [expected]:
        fail("addressee_variant", f"expected only {expected!r}, prompt carries {present!r}")

    # 4. The notation rule, wherever there is a thread to read with it. It is what confines
    #    trust to the renderer authored prefix, so a forged bracket stays text.
    if expect["notation_rule"]:
        report.checks.append("notation_rule")
        if "before the first colon" not in system:
            fail("notation_rule", "the prompt does not confine the notation to the line prefix")

    return report


def thread_of(scenario: dict[str, Any]) -> list[str]:
    context = protocol.parse_social_context(scenario["request"]["context"])
    return list(context.thread) if context is not None else []


def run_offline(scenarios: dict[str, dict[str, Any]], stream: Any = None) -> int:
    # Resolved per call, not bound at import: a default of sys.stdout captures whatever stdout
    # was when this module loaded, which is the wrong stream for anything that redirects it.
    stream = sys.stdout if stream is None else stream
    reports = [check_scenario(name, payload) for name, payload in sorted(scenarios.items())]

    for report in reports:
        if report.violations:
            print(f"FAIL {report.scenario}", file=stream)
            for violation in report.violations:
                print(f"       {violation.check}: {violation.detail}", file=stream)
        else:
            print(f"ok   {report.scenario} ({', '.join(report.checks)})", file=stream)

    failures = sum(len(report.violations) for report in reports)
    print(f"\n{len(reports)} scenarios, {failures} violations", file=stream)
    return 1 if failures else 0


JUDGE_RUBRIC = """You are grading one chat line a game bot produced in a live conversation.

Judge ONLY the bot's line, against the transcript it answered. Report four booleans:

- adopted_addressee_perspective: the line answers a question that the transcript shows was
  addressed to somebody else, or speaks about that other player's situation in the first person.
  This is the failure being measured. When in doubt, report true.
- answered_what_was_asked: IF the transcript shows a question was asked of THIS BOT, the line
  answers what was actually asked. If no question was asked of this bot, report true. This
  property is only about answering; whether the bot should have spoken at all is covered by
  adopted_addressee_perspective, so do not report false here to express that concern.
- class_consistent: nothing in the line assumes the bot or the person it answers has abilities
  their class does not have.
- stayed_on_subject: the line engages with what the transcript established rather than starting
  an unrelated topic.

Answer with a single JSON object and nothing else, with those four boolean keys plus a short
"note" string explaining any false."""


def judge_prompt(transcript: Sequence[str], bot_name: str, bot_line: str) -> str:
    lines = "\n".join(transcript)
    return f"Transcript:\n{lines}\n\nThe bot is {bot_name}. Its line was:\n{bot_line}\n"


JudgeCall = Callable[[str], dict[str, Any]]


def _anthropic_judge() -> JudgeCall:
    """A judge built from the module's own credential and model constants.

    Imported here rather than at module scope so offline mode never needs the SDK, and reading
    the constants from the provider adapter rather than restating them is what keeps the key
    lookup from drifting away from the one the sidecar actually uses.
    """

    import anthropic

    client = anthropic.Anthropic(
        api_key=os.environ.get(anthropic_provider.API_KEY_ENV_VAR, ""),
        timeout=anthropic_provider.REQUEST_TIMEOUT_SECONDS,
    )

    def call(prompt: str) -> dict[str, Any]:
        response = client.messages.create(
            model=anthropic_provider.MODEL_ID,
            max_tokens=512,
            system=JUDGE_RUBRIC,
            messages=[
                {"role": "user", "content": prompt},
                # Prefilled so the reply continues an object rather than opening with a preamble
                # or a fenced block. Cheaper and more reliable than parsing around whatever the
                # judge felt like saying first.
                {"role": "assistant", "content": "{"},
            ],
        )
        text = "{" + "".join(block.text for block in response.content if block.type == "text")
        return json.loads(text[: text.rindex("}") + 1])

    return call


# The four properties the judge scores, in report order. `adopted_addressee_perspective` is the
# one stated as a failure; the other three are stated as properties that must HOLD, so their
# polarity is inverted when deciding whether a verdict passed.
RUBRIC_FAILURES = ("adopted_addressee_perspective",)
RUBRIC_REQUIREMENTS = ("answered_what_was_asked", "class_consistent", "stayed_on_subject")


@dataclass
class JudgedSample:
    scenario: str
    line: str
    verdict: dict[str, Any]

    @property
    def silent(self) -> bool:
        # Deliberate silence is a complete answer, and for a bystander it is often the right one.
        # It cannot adopt anybody's perspective, so it is never a bleed, but it is counted and
        # reported separately: a change that simply muted every bot would otherwise score a
        # flawless acceptance run.
        return not self.line.strip()

    @property
    def adopted(self) -> bool:
        if self.silent:
            return False
        return bool(self.verdict.get("adopted_addressee_perspective", True))

    @property
    def rubric_failures(self) -> list[str]:
        """Every rubric property this verdict did not satisfy, not just the addressee one.

        The judge is asked for four properties and all four are part of the plan's rubric, so a
        reply that ignored the question or gave class wrong advice has to be visible and has to
        fail the run. Reporting only the addressee property would have let this tool claim a
        coherence evaluation it was not performing.

        Missing keys fail closed: a judge that did not answer has cleared nothing.
        """

        if self.silent:
            return []

        failures = [name for name in RUBRIC_FAILURES if bool(self.verdict.get(name, True))]
        failures += [name for name in RUBRIC_REQUIREMENTS if not bool(self.verdict.get(name, False))]
        return failures

    @property
    def passed(self) -> bool:
        return not self.rubric_failures


def _report(judged: Sequence[JudgedSample], noun: str, stream: Any) -> int:
    """One report shape for the judged scenarios and the live window alike.

    Every rubric failure is named, not just the addressee one, and silence is counted on its own
    line so a run that simply muted every bot cannot read as a clean sweep.
    """

    adopted = [sample for sample in judged if sample.adopted]
    silent = [sample for sample in judged if sample.silent]
    failed = [sample for sample in judged if not sample.passed]

    for sample in judged:
        marker = "BLEED" if sample.adopted else ("FAIL " if not sample.passed else "ok   ")
        line = "(deliberate silence)" if sample.silent else sample.line
        print(f"{marker} {sample.scenario}: {line}", file=stream)
        if not sample.passed:
            print(f"       rubric: {', '.join(sample.rubric_failures)}", file=stream)
            note = sample.verdict.get("note")
            if note:
                print(f"       {note}", file=stream)

    print(
        f"\n{len(judged)} {noun}, {len(adopted)} adopted the addressee's perspective, "
        f"{len(failed)} failed the rubric, {len(silent)} were deliberate silence",
        file=stream,
    )
    return 1 if failed else 0


def run_judge(
    scenarios: dict[str, dict[str, Any]],
    names: Sequence[str],
    samples: int,
    generate: Callable[[protocol.SocialRequest], str],
    judge: JudgeCall,
    stream: Any = None,
) -> int:
    stream = sys.stdout if stream is None else stream
    judged: list[JudgedSample] = []

    for name in names:
        request = build_request(scenarios[name]["request"])
        transcript = thread_of(scenarios[name])
        for _ in range(samples):
            line = generate(request)
            # A silent line has nothing for a judge to read, so it is not sent to one.
            verdict = {} if not line.strip() else judge(judge_prompt(transcript, request.bot_name, line))
            judged.append(JudgedSample(scenario=name, line=line, verdict=verdict))

    return _report(judged, "generations", stream)


def run_window(export: Sequence[dict[str, Any]], judge: JudgeCall, stream: Any = None) -> int:
    """Judge real conversation exported from a live server.

    One entry per thread. ``lines`` is the transcript in order, already rendered the way the
    prompt renders it; ``bot_name`` and ``bot_line`` name the generated line being judged.
    """

    stream = sys.stdout if stream is None else stream
    judged: list[JudgedSample] = []

    for thread in export:
        verdict = judge(judge_prompt(thread["lines"], thread["bot_name"], thread["bot_line"]))
        judged.append(
            JudgedSample(
                scenario=thread.get("thread_public_id", "?"),
                line=thread["bot_line"],
                verdict=verdict,
            )
        )

    return _report(judged, "live lines", stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--judge", action="store_true", help="generate real replies and score them")
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        default=None,
        help="restrict a judged run to one scenario; repeatable",
    )
    parser.add_argument("--samples", type=int, default=1, help="generations per scenario in judge mode")
    parser.add_argument("--window", type=Path, default=None, help="judge a live window export instead")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.window is not None:
        export = json.loads(args.window.read_text(encoding="utf-8"))
        return run_window(export, _anthropic_judge())

    scenarios = load_scenarios()

    if not args.judge:
        return run_offline(scenarios)

    names = args.scenarios or list(REGRESSION_SCENARIOS)
    unknown = [name for name in names if name not in scenarios]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    """A run that generates nothing must not be able to report success.

    `--samples 0` produced an empty result set, which the reporter read as a clean sweep and
    exited 0 on: an acceptance gate that passes without generating anything is worse than no gate.
    Duplicates are rejected for the same reason, because three of one scenario is not coverage of
    two.
    """
    if args.samples < 1:
        print("--samples must be at least 1; a run that generates nothing proves nothing", file=sys.stderr)
        return 2
    if len(set(names)) != len(names):
        print("--scenario was repeated; each scenario is judged --samples times already", file=sys.stderr)
        return 2

    # A subset run is legitimate for exploring, but it is not THE acceptance run, and saying so is
    # what stops a partial pass being quoted as one.
    missing = [name for name in REGRESSION_SCENARIOS if name not in names]
    if missing:
        print(
            f"note: not an acceptance run - the regression scenarios {', '.join(missing)} are not covered",
            file=sys.stderr,
        )

    adapter = anthropic_provider.AnthropicProvider()
    if not adapter.configured:
        print(
            f"{anthropic_provider.API_KEY_ENV_VAR} is not set; a judged run needs credentials",
            file=sys.stderr,
        )
        return 2

    def generate(request: protocol.SocialRequest) -> str:
        return adapter.generate_social_reply(request).message

    return run_judge(scenarios, names, args.samples, generate, _anthropic_judge())


if __name__ == "__main__":
    raise SystemExit(main())
