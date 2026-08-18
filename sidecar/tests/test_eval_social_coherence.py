"""Coverage for the coherence eval itself.

An eval that cannot fail is not an eval. Most of what is asserted here is that each structural
check actually catches the thing it was written for, by mutating a scenario until it breaks.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_social_coherence.py"
_SPEC = importlib.util.spec_from_file_location("eval_social_coherence", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
evaluation = importlib.util.module_from_spec(_SPEC)
# Registered before execution because @dataclass resolves annotations through sys.modules, and a
# module that is not there yet makes every dataclass in the script a collection error.
sys.modules[_SPEC.name] = evaluation
_SPEC.loader.exec_module(evaluation)


def _scenario(name: str) -> dict[str, Any]:
    return copy.deepcopy(evaluation.load_scenarios()[name])


def _checks_that_failed(report: Any) -> list[str]:
    return [violation.check for violation in report.violations]


def test_the_seeded_scenarios_all_hold() -> None:
    stream = io.StringIO()

    assert evaluation.run_offline(evaluation.load_scenarios(), stream) == 0
    assert "0 violations" in stream.getvalue()


def test_a_thread_that_stopped_saying_who_was_asked_fails() -> None:
    """The 2026-08-12 shape, as a check.

    The scenario declares that its prompt must carry Klara's line addressed to Sweatyguest. A
    composition that delivers the same conversation without the marker is exactly the state the
    regression happened in, so the eval has to refuse it.
    """
    scenario = _scenario("addressee_bleed")
    assert not _checks_that_failed(evaluation.check_scenario("addressee_bleed", scenario))

    context = json.loads(scenario["request"]["context"])
    context["thread"] = [line.replace(" (to Sweatyguest)", "") for line in context["thread"]]
    scenario["request"]["context"] = json.dumps(context)

    assert _checks_that_failed(evaluation.check_scenario("addressee_bleed", scenario)) == [
        "thread_annotations",
        "addressee_markers",
    ]


def test_a_thread_that_lost_its_identity_annotations_fails() -> None:
    scenario = _scenario("whisper_direct_question")
    context = json.loads(scenario["request"]["context"])
    context["thread"] = ["Deszy (to Dodokkuli): what are you specced for?"]
    scenario["request"]["context"] = json.dumps(context)

    assert "thread_annotations" in _checks_that_failed(
        evaluation.check_scenario("whisper_direct_question", scenario)
    )


def test_the_wrong_addressee_variant_fails() -> None:
    """Two variants in one prompt lets the model pick a rule, and none puts it back where the
    regression happened. Either is a violation."""
    scenario = _scenario("addressee_bleed")
    scenario["request"]["addressed_to_bot"] = 1

    assert _checks_that_failed(evaluation.check_scenario("addressee_bleed", scenario)) == [
        "addressee_variant"
    ]

    unasked = _scenario("addressee_bleed")
    unasked["request"]["expects_answer"] = 0
    assert _checks_that_failed(evaluation.check_scenario("addressee_bleed", unasked)) == ["addressee_variant"]


def test_a_premise_that_forgot_who_the_bot_is_fails() -> None:
    scenario = _scenario("addressee_bleed")
    scenario["request"]["bot_race_id"] = 0
    scenario["request"]["bot_class_id"] = 0
    scenario["request"]["bot_zone"] = ""

    assert _checks_that_failed(evaluation.check_scenario("addressee_bleed", scenario)) == [
        "premise_identity",
        "premise_identity",
    ]


def test_a_failing_scenario_makes_the_whole_run_nonzero() -> None:
    scenarios = evaluation.load_scenarios()
    scenarios["addressee_bleed"]["request"]["addressed_to_bot"] = 1
    stream = io.StringIO()

    assert evaluation.run_offline(scenarios, stream) == 1
    output = stream.getvalue()
    assert "FAIL addressee_bleed" in output
    assert "1 violations" in output


def test_a_judged_run_reports_every_bleed_it_finds() -> None:
    """No provider and no network: the generation and the judge are both supplied."""
    scenarios = evaluation.load_scenarios()
    stream = io.StringIO()

    def judge(prompt: str) -> dict[str, Any]:
        assert "Klara" in prompt, "the judge must see the transcript the line answered"
        return {
            "adopted_addressee_perspective": True,
            "answered_what_was_asked": True,
            "class_consistent": True,
            "stayed_on_subject": True,
            "note": "answered in Sweatyguest's place",
        }

    exit_code = evaluation.run_judge(
        scenarios,
        ["addressee_bleed"],
        2,
        lambda request: "mostly questing, the mobs here are decent xp",
        judge,
        stream,
    )

    assert exit_code == 1
    output = stream.getvalue()
    assert output.count("BLEED addressee_bleed") == 2
    assert "2 generations, 2 adopted" in output


def test_a_clean_judged_run_passes() -> None:
    stream = io.StringIO()
    clean = {
        "adopted_addressee_perspective": False,
        "answered_what_was_asked": True,
        "class_consistent": True,
        "stayed_on_subject": True,
    }

    exit_code = evaluation.run_judge(
        evaluation.load_scenarios(),
        list(evaluation.REGRESSION_SCENARIOS),
        1,
        lambda request: "ha, that one is for Sweatyguest",
        lambda prompt: clean,
        stream,
    )

    assert exit_code == 0
    assert "0 adopted" in stream.getvalue()


def test_deliberate_silence_is_counted_and_named_rather_than_hidden() -> None:
    """A bystander that says nothing has not adopted anyone's perspective, so it cannot be a
    bleed. It is still reported, because a change that simply muted every bot would otherwise
    score a flawless acceptance run."""
    stream = io.StringIO()

    def judge(prompt: str) -> dict[str, Any]:
        raise AssertionError("a silent line has nothing for a judge to read")

    exit_code = evaluation.run_judge(
        evaluation.load_scenarios(),
        ["human_to_other"],
        2,
        lambda request: "",
        judge,
        stream,
    )

    assert exit_code == 0
    output = stream.getvalue()
    assert output.count("(deliberate silence)") == 2
    assert (
        "2 generations, 0 adopted the addressee's perspective, 0 failed the rubric, "
        "2 were deliberate silence" in output
    )


def test_a_rubric_failure_other_than_the_addressee_one_still_fails_the_run() -> None:
    """The judge is asked for four properties and all four are the plan's rubric.

    Reading only `adopted_addressee_perspective` let a reply that ignored the question, gave
    class wrong advice, or changed the subject exit 0, which would have made this tool claim a
    coherence evaluation it was not performing.
    """
    stream = io.StringIO()

    ignored_the_question = {
        "adopted_addressee_perspective": False,
        "answered_what_was_asked": False,
        "class_consistent": True,
        "stayed_on_subject": True,
        "note": "talked about its own quest instead",
    }

    exit_code = evaluation.run_judge(
        evaluation.load_scenarios(),
        ["addressee_bleed"],
        1,
        lambda request: "anyway I just dinged 24",
        lambda prompt: ignored_the_question,
        stream,
    )

    assert exit_code == 1
    output = stream.getvalue()
    assert "FAIL  addressee_bleed" in output
    assert "rubric: answered_what_was_asked" in output
    assert "talked about its own quest instead" in output
    assert "0 adopted the addressee's perspective, 1 failed the rubric" in output


def test_a_verdict_missing_a_rubric_key_fails_closed() -> None:
    """A judge that did not answer has cleared nothing."""
    stream = io.StringIO()

    exit_code = evaluation.run_judge(
        evaluation.load_scenarios(),
        ["addressee_bleed"],
        1,
        lambda request: "sure thing",
        lambda prompt: {},
        stream,
    )

    assert exit_code == 1
    assert "BLEED addressee_bleed" in stream.getvalue()


def test_a_run_that_generates_nothing_cannot_report_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An acceptance gate that passes without generating anything is worse than no gate."""
    assert evaluation.main(["--judge", "--samples", "0"]) == 2
    assert "--samples must be at least 1" in capsys.readouterr().err

    assert evaluation.main(["--judge", "--scenario", "addressee_bleed", "--scenario", "addressee_bleed"]) == 2
    assert "repeated" in capsys.readouterr().err


def test_a_partial_scenario_run_says_it_is_not_the_acceptance_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(evaluation.anthropic_provider.API_KEY_ENV_VAR, raising=False)

    # Stops at the credential check, which is after the coverage note it must have printed.
    assert evaluation.main(["--judge", "--scenario", "addressee_bleed"]) == 2
    err = capsys.readouterr().err
    assert "not an acceptance run" in err
    assert "human_to_other" in err


def test_a_live_window_export_is_judged_end_to_end() -> None:
    export = [
        {
            "thread_public_id": "thr_1",
            "bot_name": "Dodokkuli",
            "bot_line": "mostly questing",
            "lines": ["Klara [Troll Rogue 23, Durotar] (to Sweatyguest): you leveling through here?"],
        },
        {
            "thread_public_id": "thr_2",
            "bot_name": "Dodokkuli",
            "bot_line": "razormane camp is south of the crossroads",
            "lines": ["Klara [Troll Rogue 23, Durotar]: anyone know where the razormane camp is?"],
        },
    ]
    stream = io.StringIO()

    def judge(prompt: str) -> dict[str, Any]:
        return {
            "adopted_addressee_perspective": "(to Sweatyguest)" in prompt,
            "answered_what_was_asked": True,
            "class_consistent": True,
            "stayed_on_subject": True,
        }

    assert evaluation.run_window(export, judge, stream) == 1
    output = stream.getvalue()
    assert "BLEED thr_1" in output
    assert "ok    thr_2" in output
    assert "2 live lines, 1 adopted" in output


def test_a_verdict_the_judge_did_not_answer_counts_as_a_bleed() -> None:
    """Fail closed. A judge that returned nothing useful has not cleared anything."""
    export = [{"thread_public_id": "thr_1", "bot_name": "Dodokkuli", "bot_line": "sure", "lines": []}]
    stream = io.StringIO()

    assert evaluation.run_window(export, lambda prompt: {}, stream) == 1


def test_the_command_line_names_the_regression_scenarios_by_default() -> None:
    parsed = evaluation.build_parser().parse_args(["--judge"])

    assert parsed.judge is True
    assert parsed.scenarios is None
    assert parsed.samples == 1
    assert evaluation.REGRESSION_SCENARIOS == ("addressee_bleed", "human_to_other")

    acceptance = evaluation.build_parser().parse_args(
        ["--judge", "--scenario", "addressee_bleed", "--scenario", "human_to_other", "--samples", "3"]
    )
    assert acceptance.scenarios == ["addressee_bleed", "human_to_other"]
    assert acceptance.samples == 3


def test_a_judged_run_refuses_an_unknown_scenario_before_spending_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert evaluation.main(["--judge", "--scenario", "no_such_scenario"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_a_judged_run_without_credentials_says_so_rather_than_failing_obscurely(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(evaluation.anthropic_provider.API_KEY_ENV_VAR, raising=False)

    assert evaluation.main(["--judge"]) == 2
    assert evaluation.anthropic_provider.API_KEY_ENV_VAR in capsys.readouterr().err


def test_the_default_run_is_offline_and_needs_no_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(evaluation.anthropic_provider.API_KEY_ENV_VAR, raising=False)

    assert evaluation.main([]) == 0
    assert "0 violations" in capsys.readouterr().out


def test_a_judge_that_talks_after_its_verdict_still_parses() -> None:
    """Live failure 2026-08-18: the judge closed its object, then kept going. The old
    last-brace slice swallowed the commentary and raised JSONDecodeError: Extra data."""
    reply = (
        '{"adopted_addressee_perspective": false, "answered_what_was_asked": true,\n'
        ' "class_consistent": true, "stayed_on_subject": true}\n\n'
        "The line answers the question put to this bot directly {and stays on topic}."
    )

    verdict = evaluation.parse_judge_verdict(reply)

    assert verdict == {
        "adopted_addressee_perspective": False,
        "answered_what_was_asked": True,
        "class_consistent": True,
        "stayed_on_subject": True,
    }


def test_a_clean_judge_reply_parses_unchanged() -> None:
    verdict = evaluation.parse_judge_verdict('{"adopted_addressee_perspective": true}')

    assert verdict == {"adopted_addressee_perspective": True}
