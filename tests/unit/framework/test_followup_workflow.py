from agento.framework.channels.base import PromptFragments
from agento.framework.workflows.base import Workflow
from agento.framework.workflows.followup import FollowupWorkflow


def test_followup_workflow_is_a_workflow():
    assert issubclass(FollowupWorkflow, Workflow)


def test_build_prompt_includes_instructions_context():
    class FakeChannel:
        name = "fake"

        def get_followup_fragments(self, reference_id, instructions, config=None):
            return PromptFragments(
                read_context=f"READ {reference_id}",
                respond="RESPOND",
                extra=f"KONTEKST\n{instructions}",
            )

    wf = FollowupWorkflow(runner=None, logger=None)
    prompt = wf.build_prompt(FakeChannel(), "REF-1", instructions="do the thing")
    assert "do the thing" in prompt
    assert "READ REF-1" in prompt


def test_build_prompt_opening_falls_back_to_channel_and_reference_id():
    class FakeChannel:
        name = "fake"

        def get_followup_fragments(self, reference_id, instructions, config=None):
            return PromptFragments(read_context="READ", respond="RESPOND", extra="X")

    wf = FollowupWorkflow(runner=None, logger=None)
    first_line = wf.build_prompt(FakeChannel(), "REF-1", instructions="x").splitlines()[0]
    assert first_line == "Kontynuacja zadania (fake) REF-1. To jest zaplanowany follow-up."


def test_build_prompt_uses_channel_followup_intro_instead_of_reference_id():
    class FakeChannel:
        name = "fake"

        def get_followup_fragments(self, reference_id, instructions, config=None):
            return PromptFragments(
                read_context=f"READ {reference_id}", respond="RESPOND", extra="X",
                followup_intro="Kontynuuj zadanie z wiadomości email.",
            )

    wf = FollowupWorkflow(runner=None, logger=None)
    prompt = wf.build_prompt(FakeChannel(), "slug::LONGBASE64ID", instructions="x")
    first_line = prompt.splitlines()[0]
    assert first_line == "Kontynuuj zadanie z wiadomości email. To jest zaplanowany follow-up."
    assert "slug::LONGBASE64ID" not in first_line
