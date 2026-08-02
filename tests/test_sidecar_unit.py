

def test_a_rejection_names_an_objective_category() -> None:
    """Key Decision 2 asks for objective moderation categories, and Key Decision 6 for a
    deterministic gate. These are the same thing: the categories are what the gate reports.

    Objective means each one is a property of the text, decidable by reading it, rather than
    a judgement someone could disagree with. "Broke character" and "carried document
    structure" are checkable; "unhelpful" would not be.
    """
    request = protocol.parse_social_request(_social_request_payload(), TEST_TOKEN)

    cases = {
        "": claude.ModerationCategory.EMPTY,
        "First\nsecond": claude.ModerationCategory.NOT_ONE_LINE,
        "x" * 300: claude.ModerationCategory.TOO_LONG,
        "As an AI language model, no.": claude.ModerationCategory.BROKE_CHARACTER,
        "```code```": claude.ModerationCategory.DOCUMENT_STRUCTURE,
        "Grimbold: aye": claude.ModerationCategory.TRANSCRIPT,
    }

    for text, expected in cases.items():
        with pytest.raises(claude.ClaudeInvalidOutputError) as caught:
            claude.validate_social_message(text, request)

        assert caught.value.category is expected

    # And the categories are a closed set, so telemetry cannot grow a new one by accident.
    assert set(claude.ModerationCategory) == {
        claude.ModerationCategory.EMPTY,
        claude.ModerationCategory.NOT_ONE_LINE,
        claude.ModerationCategory.TOO_LONG,
        claude.ModerationCategory.BROKE_CHARACTER,
        claude.ModerationCategory.DOCUMENT_STRUCTURE,
        claude.ModerationCategory.TRANSCRIPT,
        claude.ModerationCategory.FORBIDDEN_CLAIM,
        claude.ModerationCategory.QUOTED_THREAD,
        claude.ModerationCategory.CARRIED_SECRET,
        claude.ModerationCategory.BOTH_ANSWERS,
        claude.ModerationCategory.UNKNOWN_EMOTE,
        claude.ModerationCategory.EMOTE_CHANNEL_ILLEGAL,
    }
