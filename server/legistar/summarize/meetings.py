import typing as t

from server.documents.summarize import (
    CONCISE_SUMMARY_TEMPLATE,
    SummarizationResult,
    summarize_openai,
)
from server.lib.style import SummarizationStyle

# ---------------------------------------------------------------------
# Django templates for our LLM prompts
# ---------------------------------------------------------------------

# Django template syntax uses {{ variable_name }}; this does not conflict
# with the LangChain variable substitution syntax, which uses {variable_name}.
# But it *does* read a little confusingly. Sorry about that.

MEETING_CONCISE_TEMPLATE = """The following is a set of descriptions of items on the agenda for an upcoming {{ department }} meeting. Write a concise summary of the following text. Include the most important details:

"{text}"

CONCISE_AGENDA_SUMMARY:"""  # noqa: E501


MEETING_CONCISE_HEADLINE_TEMPLATE = """The following is a set of descriptions of items on the agenda for an upcoming {{ department }} meeting. Write a concise and extremely compact headline (one sentence or less) for the following text. Capture the most salient detail or two:

"{text}"

CONCISE_COMPACT_HEADLINE_FOR_AGENDA:"""  # noqa: E501


# ---------------------------------------------------------------------
# Meeting summarizers
# ---------------------------------------------------------------------


def _meeting_template_context(department_name: str) -> dict[str, t.Any]:
    """Return a context dictionary for our Django meeting templates."""
    return {"department": department_name}


def summarize_meeting_gpt35_concise(
    department_name: str,
    document_summary_texts: list[str],
    legislation_summary_texts: list[str],
) -> SummarizationResult:
    """Summarize a meeting using OLMo (migration from GPT-3.5)."""
    from server.lib.olmo_client import get_olmo_client
    from server.documents.summarize import SummarizationSuccess, SummarizationError

    try:
        # Combine all summaries
        all_summaries = document_summary_texts + legislation_summary_texts
        context = f"Department: {department_name}\n\n"
        context += "Legislation Items:\n"
        context += "\n\n".join(f"{i}. {s}" for i, s in enumerate(all_summaries, 1))

        prompt = f"""Summarize this {department_name} meeting agenda:

{context}

Provide a concise summary of the meeting's key items and legislative actions."""

        olmo = get_olmo_client()
        body = olmo.generate(prompt, max_new_tokens=512, temperature=0.3)

        headline_prompt = (
            f"Create a brief headline for this {department_name} meeting (under 15 words)"
        )
        headline = olmo.generate(headline_prompt, max_new_tokens=30, temperature=0.3)

        return SummarizationSuccess(
            original_text=context,
            body=body,
            headline=headline.strip(),
            chunks=(context,),
            chunk_summaries=(body,),
        )
    except Exception as e:
        return SummarizationError(
            original_text=f"{department_name} meeting",
            message=f"Meeting summarization failed: {str(e)}",
        )


# ---------------------------------------------------------------------
# Meeting external utilties
# ---------------------------------------------------------------------


@t.runtime_checkable
class MeetingSummarizerCallable(t.Protocol):
    __name__: str

    def __call__(
        self,
        department_name: str,
        document_summary_texts: list[str],
        legislation_summary_texts: list[str],
    ) -> SummarizationResult:
        ...


MEETING_SUMMARIZERS: list[MeetingSummarizerCallable] = [
    summarize_meeting_gpt35_concise,
]

MEETING_SUMMARIZERS_BY_STYLE: dict[SummarizationStyle, MeetingSummarizerCallable] = {
    "concise": summarize_meeting_gpt35_concise,
}
