"""
Management command: generate-amendment-summaries

For each Council Bill, detects supporting documents whose title contains
'amendment', sets Legislation.has_amendment_docs, and creates or updates
an AmendmentSummary record for each amendment document.

Amendment data extracted per document:
  - amendment_number   : parsed from document title
  - short_title        : LLM-extracted from document text
  - sponsors           : LLM-extracted from document text
  - effect_statement   : LLM-extracted, attributed to sponsor(s)
  - normative_summary  : LLM summary embracing normative/policy language
  - technical_changes  : LLM-generated bulleted list of instrumental changes
  - votes_json         : from legislation.vote_data, or inferred "pass as amended"
  - pass_as_amended    : True when no separate amendment vote was recorded
"""

import re
import sys

from django.core.management.base import BaseCommand

from server.legistar.models import AmendmentSummary, Legislation

_COUNCIL_BILL_KIND = "Council Bill"
_AMENDMENT_KEYWORDS = frozenset({"amend", "substitute"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_amendment_document(document) -> bool:
    """Return True if the document title contains 'amendment'."""
    return "amendment" in document.title.lower()


def _amendment_number_from_title(title: str) -> str:
    """Parse the amendment identifier from the document title.

    Title format: 'legislation-{id}-supporting-{name}'
    e.g. 'legislation-123-supporting-Amendment A' → 'A'
    """
    name = title.split("-supporting-", 1)[-1]
    m = re.search(r"amendment\s+(\S+)", name, re.IGNORECASE)
    return m.group(1) if m else name


def _olmo_extract(olmo, prompt: str, max_new_tokens: int) -> str:
    """Run an OLMo prompt and return the stripped result."""
    try:
        return olmo.generate(prompt, max_new_tokens=max_new_tokens).strip()
    except Exception as exc:
        print(f"    [warn] OLMo extraction failed: {exc}", file=sys.stderr)
        return ""


def _extract_sponsors(olmo, text: str) -> list[dict]:
    """Extract sponsor/author names from the first ~600 chars of the document."""
    excerpt = text[:600]
    prompt = (
        "From the following Seattle City Council amendment text, extract only the "
        "sponsor(s) or author(s) names. Return as a comma-separated list of names "
        "with no other text.\n\nAmendment text:\n" + excerpt
    )
    raw = _olmo_extract(olmo, prompt, max_new_tokens=64)
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return [{"name": n} for n in names]


def _extract_effect_statement(olmo, text: str, sponsors: list[dict]) -> str:
    """Extract the Effect Statement, attributed to sponsor(s)."""
    excerpt = text[:2500]
    sponsor_names = (
        ", ".join(s["name"] for s in sponsors) if sponsors else "the sponsor"
    )
    prompt = (
        "From the following Seattle City Council amendment text, extract the "
        "Effect Statement section verbatim. If no labeled 'Effect Statement' "
        "section exists, write a 1-2 sentence description of the amendment's "
        "stated purpose or effect. Do not add any preamble.\n\n"
        "Amendment text:\n" + excerpt
    )
    statement = _olmo_extract(olmo, prompt, max_new_tokens=256)
    if statement:
        attribution = f"Per {sponsor_names}: "
        if not statement.startswith(attribution):
            statement = attribution + statement
    return statement


def _extract_short_title(olmo, text: str) -> str:
    """Extract a short descriptive title from the amendment text."""
    excerpt = text[:1000]
    prompt = (
        "Write a short title (10 words or fewer) that describes what this "
        "Seattle City Council amendment does. Return only the title, no quotes.\n\n"
        "Amendment text:\n" + excerpt
    )
    return _olmo_extract(olmo, prompt, max_new_tokens=32)


def _generate_normative_summary(olmo, text: str) -> str:
    """2-3 sentence summary embracing the amendment's normative/policy language."""
    excerpt = text[:4000]
    prompt = (
        "Summarize the following Seattle City Council amendment using its normative "
        "and policy language. Describe what the amendment establishes, requires, "
        "or prohibits as if explaining its intent to a Seattle resident. "
        "Write 2-3 sentences. Do not add any preamble.\n\n"
        "Amendment text:\n" + excerpt
    )
    return _olmo_extract(olmo, prompt, max_new_tokens=256)


def _generate_technical_changes(olmo, text: str) -> str:
    """Bulleted list of concrete instrumental changes made by the amendment."""
    excerpt = text[:4000]
    prompt = (
        "List the specific technical changes made by the following Seattle City "
        "Council amendment as a bulleted list. For each change, specify what "
        "section or text is modified and what the change is. "
        "Use the format: '• [Section/item] changes from [old] to [new]' or "
        "'• [Section/item] is added/removed'. Return only the bulleted list.\n\n"
        "Amendment text:\n" + excerpt
    )
    return _olmo_extract(olmo, prompt, max_new_tokens=384)


# ---------------------------------------------------------------------------
# Vote matching
# ---------------------------------------------------------------------------


def _match_amendment_votes(legislation, amendment_number: str) -> tuple[dict, bool]:
    """
    Find votes for this amendment from the legislation's vote_data.

    Returns (votes_json, pass_as_amended).

    - votes_json: {rows: [{name, vote, in_favor, opposed, absent}, ...]}
    - pass_as_amended: True when no separate amendment vote was found but
      the bill has a final passage vote (the amendment was implicitly adopted).
    """
    vote_data = legislation.vote_data or {}
    action_details = vote_data.get("action_details", [])

    # Try to find a vote specifically for this amendment
    amend_lower = f"amendment {amendment_number}".lower()
    for entry in action_details:
        action_text = (entry.get("action") or "").lower()
        action_by = (entry.get("action_by") or "").lower()
        if amend_lower in action_text or amend_lower in action_by:
            rows = _rows_from_entry(entry)
            if rows:
                return {"rows": rows}, False

    # No amendment-specific vote found. Check for a final passage vote.
    # If the bill passed (result is non-empty), the amendment was passed as amended.
    for entry in action_details:
        result = (entry.get("result") or "").strip()
        action_by = (entry.get("action_by") or "").lower()
        is_full_council = any(
            body in action_by
            for body in ("full council", "seattle city council", "city council")
        )
        if result and is_full_council:
            rows = _rows_from_entry(entry)
            if rows:
                # Relabel votes as "Pass as Amended"
                amended_rows = [
                    {
                        **r,
                        "vote": "Pass as Amended",
                        "in_favor": True,
                        "opposed": False,
                        "absent": False,
                    }
                    for r in rows
                ]
                return {"rows": amended_rows}, True

    return {}, False


def _rows_from_entry(entry: dict) -> list[dict]:
    """Extract vote rows from an action_details entry."""
    action = entry.get("action") or {}
    if isinstance(action, str):
        return []
    return action.get("rows", [])


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _process_legislation(legislation, force: bool, olmo) -> None:
    """Detect amendment docs, update flag, and create AmendmentSummary rows."""
    supporting_docs = list(legislation.documents.filter(kind="supporting_document"))
    amendment_docs = [d for d in supporting_docs if _is_amendment_document(d)]

    # Update the flag
    new_flag = bool(amendment_docs)
    if legislation.has_amendment_docs != new_flag:
        legislation.has_amendment_docs = new_flag
        legislation.save(update_fields=["has_amendment_docs"])

    for doc in amendment_docs:
        if (
            not force
            and AmendmentSummary.objects.filter(
                legislation=legislation, document=doc
            ).exists()
        ):
            print(
                f"  [skip] Amendment already summarized: {doc.title}",
                file=sys.stderr,
            )
            continue

        print(f"  [process] {doc.title}", file=sys.stderr)

        # Ensure text is extracted (idempotent)
        try:
            text = doc.extract_text() if not doc.extracted_text else doc.extracted_text
        except Exception as exc:
            print(f"    [warn] Text extraction failed: {exc}", file=sys.stderr)
            text = ""

        amendment_number = _amendment_number_from_title(doc.title)

        if text:
            sponsors = _extract_sponsors(olmo, text)
            effect_statement = _extract_effect_statement(olmo, text, sponsors)
            short_title = _extract_short_title(olmo, text)
            normative_summary = _generate_normative_summary(olmo, text)
            technical_changes = _generate_technical_changes(olmo, text)
        else:
            sponsors = []
            effect_statement = ""
            short_title = ""
            normative_summary = ""
            technical_changes = ""

        votes_json, pass_as_amended = _match_amendment_votes(
            legislation, amendment_number
        )

        AmendmentSummary.objects.update_or_create(
            legislation=legislation,
            document=doc,
            defaults={
                "amendment_number": amendment_number,
                "short_title": short_title,
                "sponsors": sponsors,
                "effect_statement": effect_statement,
                "normative_summary": normative_summary,
                "technical_changes": technical_changes,
                "votes_json": votes_json,
                "pass_as_amended": pass_as_amended,
            },
        )
        print(
            f"    [saved] Amendment {amendment_number}, "
            f"pass_as_amended={pass_as_amended}",
            file=sys.stderr,
        )


class Command(BaseCommand):
    help = (
        "Detect amendment supporting documents for Council Bills and generate "
        "structured AmendmentSummary records (sponsors, effect statement, "
        "normative summary, technical changes, votes)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Re-process amendment documents even if an "
                "AmendmentSummary already exists."
            ),
        )
        parser.add_argument(
            "--pk",
            type=int,
            default=None,
            help="Process only the Legislation with this primary key.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Process only the N most recently crawled Council Bills "
                "(ordered by database id descending)."
            ),
        )

    def handle(self, *args, **options):
        force = options["force"]
        pk = options["pk"]
        limit = options["limit"]

        if pk is not None:
            legislations = Legislation.objects.filter(pk=pk)
        else:
            legislations = Legislation.objects.filter(
                type__icontains=_COUNCIL_BILL_KIND
            ).order_by("-id")
            if limit is not None:
                legislations = legislations[:limit]

        total = legislations.count()
        self.stderr.write(f"Processing {total} Council Bill(s)...")

        # Load OLMo once; it's a ~6 GB model so we want a single instance
        from server.lib.olmo_client import get_olmo_client

        olmo = get_olmo_client()

        for i, legislation in enumerate(legislations.iterator(), start=1):
            self.stderr.write(
                f"[{i}/{total}] {legislation.record_no}: {legislation.truncated_title}"
            )
            try:
                _process_legislation(legislation, force=force, olmo=olmo)
            except Exception as exc:
                self.stderr.write(f"  [error] {exc}")

        self.stderr.write("Done.")
