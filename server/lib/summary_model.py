from django.db import models


class SummaryBaseModel(models.Model):
    """
    An abstract database model that defines the common fields and methods
    expected to be found on *all* summaries in the database, whether they are
    summaries of individual `Document`s, `Legislation`s, or even full council
    `Meeting`s.

    For details on how Django handles abstract base models, see:
    https://docs.djangoproject.com/en/4.2/topics/db/models/#abstract-base-classes
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # We summarize in two ways: a long-form `body` and a short-form `headline`.
    body = models.TextField(help_text="A detailed summary of a text.")
    headline = models.TextField(help_text="A brief summary of a text.")

    # For debugging purposes, we store the original text that was summarized.
    #
    # This is likely to *always* duplicate other content in our database
    # (for instance, if it's a `Document` summary, this will be the same as
    # `Document.extracted_text`), but it's useful to have it here for debugging.
    original_text = models.TextField(help_text="The original summarized text.")

    # When summarizing a large block of text, we often need to split it into
    # chunks and summarize each chunk individually. This allows us to get around
    # the LLM's limited context window size (4k tokens for GPT-3.5-turbo;
    # 2k tokens for Vicuna13B; etc.). To help us debug and just make sense of
    # our final summaries, we store the chunks and per-chunk summaries here.
    chunks = models.JSONField(
        default=list,
        help_text="Text chunks sent to the LLM for summarization.",
    )
    chunk_summaries = models.JSONField(
        default=list,
        help_text="LLM outputs for each text chunk.",
    )

    style = models.CharField(
        max_length=255,
        db_index=True,
        help_text="The SummarizationStyle used to generate this summary.",
    )

    class Meta:
        abstract = True
        ordering = ["-created_at"]

class SummaryStyle:
    """Configuration for OLMo-based summarization."""

    def __init__(self, name: str):
        self.name = name
        self._client = None

    @property
    def client(self):
        """Lazy-load the OLMo client."""
        if self._client is None:
            from server.lib.olmo_client import get_olmo_client
            self._client = get_olmo_client()
        return self._client

    def generate_summary(self, text: str, **kwargs) -> dict:
        """Generate summary using OLMo 3."""
        return self.client.summarize(
            text=text,
            style=self.name,
            max_tokens=kwargs.get('max_tokens', 256),
        )

# Style definitions - instances created lazily
_STYLES = None

def get_styles():
    """Get or create the STYLES dictionary lazily."""
    global _STYLES
    if _STYLES is None:
        _STYLES = {
            'concise': SummaryStyle('concise'),
            'detailed': SummaryStyle('detailed'),
        }
    return _STYLES
