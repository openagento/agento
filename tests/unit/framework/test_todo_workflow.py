from agento.framework.channels.base import PromptFragments
from agento.framework.workflows.base import Workflow
from agento.framework.workflows.todo import TodoWorkflow


def test_todo_workflow_is_a_workflow():
    assert issubclass(TodoWorkflow, Workflow)


def test_build_prompt_uses_channel_fragments():
    class FakeChannel:
        name = "fake"

        def get_prompt_fragments(self, reference_id, config=None):
            return PromptFragments(
                read_context=f"READ {reference_id}", respond="RESPOND"
            )

    wf = TodoWorkflow(runner=None, logger=None)
    prompt = wf.build_prompt(FakeChannel(), "REF-1")
    assert "READ REF-1" in prompt
    assert "RESPOND" in prompt
    assert "(fake)" in prompt


def test_build_prompt_opening_falls_back_to_channel_and_reference_id():
    # No task_intro -> keep the generic "(name) reference_id" opening (Jira/test behaviour unchanged).
    class FakeChannel:
        name = "fake"

        def get_prompt_fragments(self, reference_id, config=None):
            return PromptFragments(read_context="READ", respond="RESPOND")

    wf = TodoWorkflow(runner=None, logger=None)
    first_line = wf.build_prompt(FakeChannel(), "REF-1").splitlines()[0]
    assert first_line == "Wykonaj zadanie (fake) REF-1. Postępuj krok po kroku:"


def test_build_prompt_uses_channel_task_intro_instead_of_reference_id():
    # When a channel supplies task_intro, the opening line uses it verbatim and does NOT repeat the
    # (possibly long/compound) reference_id — saving prompt tokens.
    class FakeChannel:
        name = "fake"

        def get_prompt_fragments(self, reference_id, config=None):
            return PromptFragments(
                read_context=f"READ {reference_id}", respond="RESPOND",
                task_intro="Wykonaj zadanie z wiadomości email.",
            )

    wf = TodoWorkflow(runner=None, logger=None)
    prompt = wf.build_prompt(FakeChannel(), "slug::LONGBASE64ID")
    first_line = prompt.splitlines()[0]
    assert first_line == "Wykonaj zadanie z wiadomości email. Postępuj krok po kroku:"
    assert "slug::LONGBASE64ID" not in first_line
