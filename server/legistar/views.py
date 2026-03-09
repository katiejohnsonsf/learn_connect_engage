import datetime
import json

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.html import format_html_join
from django.views.decorators.http import require_GET

from server.documents.models import Document, DocumentSummary
from server.lib.style import SUMMARIZATION_STYLES, SummarizationStyle
from server.lib.truncate import truncate_str

from .models import (
    CrawlMetadata,
    Legislation,
    LegislationSummary,
    Meeting,
    MeetingSummary,
    SummaryEvaluation,
)

_SUMMARY_PENDING = "Summary pending\u2026"
_COUNCIL_BILL_KIND = "Council Bill"

_FULL_COUNCIL_BODIES = frozenset(
    {"full council", "seattle city council", "city council"}
)

# Seattle City Council member → seat mapping (update after each election).
# Districts 1-7 are geographic; 8 = Position 8 at-large, 9 = Position 9 at-large.
# Names must match exactly what Legistar stores (lowercased for lookup).
# Last updated: 2025-2026 council seated after November 2025 elections.
_COUNCIL_DISTRICTS: dict[str, int] = {
    "rob saka": 1,  # District 1
    "eddie lin": 2,  # District 2
    "joy hollingsworth": 3,  # District 3
    "maritza rivera": 4,  # District 4
    "debora juarez": 5,  # District 5 (appointed July 2025)
    "dan strauss": 6,  # District 6
    "robert kettle": 7,  # District 7
    "alexis mercedes rinck": 8,  # Position 8 — At-Large
    "dionne foster": 9,  # Position 9 — At-Large
}

_NAME_PREFIXES = ("councilmember ", "council member ", "cm ", "councilmember. ")

_AMENDMENT_KEYWORDS = frozenset(
    {"amend", "substitute", "revised", "modified", "changed"}
)


_STATUS_TOOLTIPS = {
    "signed": "Signed by Mayor, awaiting or completed codification",
    "vetoed": "Returned by Mayor without signature",
    "passed": "Approved by Full Council, in executive phase",
    "failed": "Did not pass Full Council",
    "in_committee": "Referred and awaiting or undergoing committee review",
    "referred": "Referred from Council to Committee",
}


def _normalize_member_name(raw: str) -> str:
    """Strip honorific prefixes and lowercase a council member's name."""
    name = raw.strip().lower()
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def _classify_vote(vote_str: str) -> dict:
    """Return in_favor/opposed/absent booleans for a raw vote string."""
    low = vote_str.lower()
    return {
        "in_favor": "favor" in low,
        "opposed": low in ("no", "against", "opposed") or "oppos" in low,
        "absent": "absent" in low or "excused" in low,
    }


def _vote_rows_from_entry(entry: dict) -> list[dict]:
    return entry.get("action", {}).get("rows", [])


def _is_district_seat(district) -> bool:
    """Return True for geographic district seats (1-7), False for at-large (8-9) or unknown."""  # noqa: E501
    return isinstance(district, int) and district <= 7


def _extract_amendments(legislation) -> list[dict]:
    """Return amendments from AmendmentSummary records, falling back to action rows."""
    # Use AmendmentSummary records as the authoritative source
    amendment_summaries = list(
        legislation.amendment_summaries.all().order_by("amendment_number")
    )
    if amendment_summaries:
        amendments = []
        for s in amendment_summaries:
            sponsors = ", ".join(p["name"] for p in (s.sponsors or []) if "name" in p)
            amendments.append(
                {
                    "date": s.created_at.date(),
                    "action": s.short_title or f"Amendment {s.amendment_number}",
                    "action_by": sponsors,
                    "result": "Pass as Amended" if s.pass_as_amended else "",
                    "votes": (s.votes_json or {}).get("rows", []),
                }
            )
        return amendments

    # Fall back to action history rows
    amendments = []
    try:
        for row in legislation.crawl_data.rows:
            action = (row.action or "").lower()
            if any(kw in action for kw in _AMENDMENT_KEYWORDS):
                amendments.append(
                    {
                        "date": row.date,
                        "action": row.action,
                        "action_by": row.action_by or "",
                        "result": row.result or "",
                    }
                )
    except Exception:
        pass
    return amendments


def _extract_district_votes(legislation) -> tuple[list[dict], list[dict]]:
    """
    Return (district_votes, at_large_votes) from stored vote_data.
    Each item: {name, vote, in_favor, opposed, absent, district}.
    Returns empty lists if no vote data is stored.
    """
    entries = (legislation.vote_data or {}).get("action_details", [])
    district_votes: list[dict] = []
    at_large_votes: list[dict] = []
    seen: set[str] = set()

    for entry in entries:
        for row in _vote_rows_from_entry(entry):
            name = (row.get("person") or {}).get("name", "").strip()
            vote_str = (row.get("vote") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            district = _COUNCIL_DISTRICTS.get(_normalize_member_name(name))
            item = {
                "name": name,
                "vote": vote_str,
                "district": district,
                **_classify_vote(vote_str),
            }
            (district_votes if _is_district_seat(district) else at_large_votes).append(
                item
            )

    def _seat_order(v):
        return v["district"] if isinstance(v["district"], int) else 99  # noqa: E731

    district_votes.sort(key=_seat_order)
    at_large_votes.sort(key=_seat_order)
    return district_votes, at_large_votes


def _extract_full_council_vote_date(legislation):
    """Return the date of the Full Council vote for this legislation, or None."""
    for row in legislation.crawl_data.rows:
        if row.result and any(
            body in (row.action_by or "").lower() for body in _FULL_COUNCIL_BODIES
        ):
            return row.date
    return None


def _council_bill_status(legislation) -> tuple[str, str]:
    """Return (display_label, tooltip_text) for a council bill's legislative status."""
    raw = (legislation.status or "").strip()
    low = raw.lower()

    if "sign" in low:
        return "Signed", _STATUS_TOOLTIPS["signed"]
    if "veto" in low or "returned" in low:
        return "Vetoed", _STATUS_TOOLTIPS["vetoed"]
    if "pass" in low or "adopt" in low:
        return "Passed", _STATUS_TOOLTIPS["passed"]
    if "fail" in low or "defeat" in low:
        return "Failed", _STATUS_TOOLTIPS["failed"]

    # "In Committee" = actively being heard; "Referred" = assigned but not yet heard
    try:
        body = (legislation.crawl_data.controlling_body or "").strip()
    except Exception:
        body = ""

    if "hear" in low or "in committee" in low:
        if body and body.lower() not in _FULL_COUNCIL_BODIES:
            return f"In Committee ({body})", _STATUS_TOOLTIPS["in_committee"]

    return "Referred", _STATUS_TOOLTIPS["referred"]


# ------------------------------------------------------------------------
# Utilities for cleaning up text summaries and generating HTML
# ------------------------------------------------------------------------


# CONSIDER: if we make these (and our `truncate_str`) into Django template
# filters, we could remove most -- maybe all? -- of the `_*_context()`
# functions below and move their functionality directly to the templates.


def _text_to_html_paragraphs(text: str):
    """Convert text, with newlines, to simple runs of HTML paragraphs."""
    splits = [s.strip() for s in text.split("\n")]
    return format_html_join("\n", "<p>{}</p>", ((s,) for s in splits if s))


_STRUCTURED_SECTION_HEADERS = frozenset(
    {
        "WHAT WAS ORIGINALLY PROPOSED",
        "AMENDMENTS AND VOTES",
        "WHAT THE FINAL TEXT DOES",
        "WHAT CHANGED FROM THE ORIGINAL",
    }
)


_SKIP_SECTIONS = frozenset({"AMENDMENTS AND VOTES"})


def _structured_summary_to_html(text: str):
    """Convert a structured summary with section headers into HTML.

    The AMENDMENTS AND VOTES section is omitted — member votes are
    rendered separately via the interactive map and member grid.
    """
    from django.utils.html import format_html

    lines = [s.strip() for s in text.split("\n")]
    html_parts = []
    skip = False
    for line in lines:
        if not line:
            continue
        if line in _STRUCTURED_SECTION_HEADERS:
            skip = line in _SKIP_SECTIONS
            if not skip:
                html_parts.append(
                    format_html('<h2 style="font-weight:700">{}</h2>', line.title())
                )
        elif not skip:
            html_parts.append(format_html("<p>{}</p>", line))
    return "\n".join(html_parts)


def _split_structured_summary(text: str) -> tuple[str, str]:
    """Split a structured summary into (what_changed_html, main_html).

    what_changed_html: just the WHAT CHANGED FROM THE ORIGINAL section.
    main_html: all other sections except AMENDMENTS AND VOTES and WHAT CHANGED.
    """
    from django.utils.html import format_html

    _WHAT_CHANGED = "WHAT CHANGED FROM THE ORIGINAL"
    lines = [s.strip() for s in text.split("\n")]
    what_changed_parts: list[str] = []
    main_parts: list[str] = []
    current_section: str | None = None

    for line in lines:
        if not line:
            continue
        if line in _STRUCTURED_SECTION_HEADERS:
            current_section = line
            if line == _WHAT_CHANGED:
                what_changed_parts.append(
                    format_html('<h2 style="font-weight:700">{}</h2>', line.title())
                )
            elif line not in _SKIP_SECTIONS:
                main_parts.append(
                    format_html('<h2 style="font-weight:700">{}</h2>', line.title())
                )
        elif current_section == _WHAT_CHANGED:
            what_changed_parts.append(format_html("<p>{}</p>", line))
        elif current_section not in _SKIP_SECTIONS:
            main_parts.append(format_html("<p>{}</p>", line))

    return "\n".join(what_changed_parts), "\n".join(main_parts)


def _remove_surrounding_quotes(text: str):
    """Remove quotes and other annoying characters in a given text."""
    # Usually, we use this with the headline for a summary; for whatever reason,
    # GPT-3.5 and Vicuna 13B both like putting quotes around the headlines
    # they generate. CONSIDER making this part of the summarization pipeline
    # rather than a view/template concern.
    text = text.strip()
    if text.startswith("“") or text.startswith('"'):
        text = text[1:]
    if text.endswith("”") or text.endswith('"'):
        text = text[:-1]
    return text


# ------------------------------------------------------------------------
# Utilities to generate context data for our Django templates
# ------------------------------------------------------------------------


def _legislation_table_context(
    legislation: Legislation, style: SummarizationStyle
) -> dict:
    """
    Build context data for the given `legislation`; this is used in our
    HTML templates that display a table of legislation instances.
    """
    summary = LegislationSummary.objects.filter(
        legislation=legislation, style=style
    ).first()
    clean_headline = (
        _remove_surrounding_quotes(summary.headline) if summary else _SUMMARY_PENDING
    )
    return {
        "legistar_id": legislation.legistar_id,
        "url": legislation.url,
        "title": legislation.title,
        "truncated_title": legislation.truncated_title,
        "type": legislation.type,
        "kind": legislation.kind,
        "headline": clean_headline,
        "truncated_headline": truncate_str(clean_headline, 24),
        "summary_pending": summary is None,
    }


def _document_table_context(document: Document, style: SummarizationStyle) -> dict:
    """
    Build context data for a `document`; this is used in our HTML templates
    that display a table of `Document` instances.
    """
    summary = get_object_or_404(DocumentSummary, document=document, style=style)
    clean_headline = _remove_surrounding_quotes(summary.headline)
    return {
        "pk": document.pk,
        "url": document.url,
        "kind": document.kind.replace("_", " ").title(),
        "title": document.short_title,
        "truncated_title": document.truncated_title,
        "headline": clean_headline,
        "truncated_headline": truncate_str(clean_headline, 24),
    }


def _meeting_context(meeting: Meeting, style: SummarizationStyle) -> dict:
    """
    Build context data for a `meeting`; this is used in our HTML templates
    that display detailed information about a single `Meeting` instance.
    """
    base = {
        "legistar_id": meeting.legistar_id,
        "url": meeting.url,
        "date": meeting.date,
        "time": meeting.time,
        "location": meeting.location,
        "department": meeting.crawl_data.department,
    }
    if not meeting.is_active:
        return {**base, "is_active": False}

    summary = MeetingSummary.objects.filter(meeting=meeting, style=style).first()
    if summary is None:
        # Summary pending — show meeting without AI summary yet
        legislation_table_contexts = []
        for legislation in meeting.legislations:
            leg_summary = LegislationSummary.objects.filter(
                legislation=legislation, style=style
            ).first()
            if leg_summary:
                legislation_table_contexts.append(
                    _legislation_table_context(legislation, style)
                )
        return {
            **base,
            "is_active": True,
            "skip": False,
            "summary_pending": True,
            "headline": _SUMMARY_PENDING,
            "truncated_headline": _SUMMARY_PENDING,
            "summary": _text_to_html_paragraphs("Summaries are being generated."),
            "legislation_table_contexts": legislation_table_contexts,
        }

    clean_headline = _remove_surrounding_quotes(summary.headline)
    skip = "unable to summarize" in clean_headline.lower()
    legislation_contexts = [
        _legislation_context(legislation, style)
        for legislation in meeting.legislations
        if LegislationSummary.objects.filter(
            legislation=legislation, style=style
        ).exists()
    ]
    return {
        **base,
        "is_active": True,
        "skip": skip,
        "summary_pending": False,
        "headline": clean_headline,
        "truncated_headline": truncate_str(clean_headline, 24),
        "summary": _text_to_html_paragraphs(summary.body),
        "legislation_table_contexts": [
            _legislation_table_context(legislation, style)
            for legislation in meeting.legislations
            if LegislationSummary.objects.filter(
                legislation=legislation, style=style
            ).exists()
        ],
        "legislation_contexts": legislation_contexts,
    }


def _legislation_context(legislation: Legislation, style: SummarizationStyle) -> dict:
    """
    Build context data for a `legislation`; this is used in our HTML
    templates that display detailed information about a single `Legislation`
    instance.
    """
    summary = LegislationSummary.objects.filter(
        legislation=legislation, style=style
    ).first()
    headline = (
        _remove_surrounding_quotes(summary.headline) if summary else _SUMMARY_PENDING
    )
    body = summary.body if summary else "This summary is being generated."

    # Use structured rendering for Council Bill summaries with section headers
    is_structured = summary and "WHAT WAS ORIGINALLY PROPOSED" in body
    if is_structured:
        rendered_summary = _structured_summary_to_html(body)
        what_changed_html, summary_without_what_changed = _split_structured_summary(
            body
        )
    else:
        rendered_summary = _text_to_html_paragraphs(body)
        what_changed_html = ""
        summary_without_what_changed = rendered_summary

    is_council_bill = _COUNCIL_BILL_KIND in legislation.type
    bill_status_label, bill_status_tooltip = (
        _council_bill_status(legislation) if is_council_bill else (None, None)
    )
    district_votes, at_large_votes = (
        _extract_district_votes(legislation) if is_council_bill else ([], [])
    )
    vote_date = (
        _extract_full_council_vote_date(legislation) if is_council_bill else None
    )
    amendments = _extract_amendments(legislation) if is_council_bill else []
    amendments_json = json.dumps(
        [
            {**a, "date": a["date"].isoformat() if a.get("date") else None}
            for a in amendments
        ]
    )

    return {
        "legistar_id": legislation.legistar_id,
        "url": legislation.url,
        "title": legislation.title,
        "truncated_title": legislation.truncated_title,
        "type": legislation.type,
        "kind": legislation.kind,
        "headline": headline,
        "summary_pending": summary is None,
        "summary": rendered_summary,
        "summary_what_changed": what_changed_html,
        "summary_without_what_changed": summary_without_what_changed,
        "bill_status_label": bill_status_label,
        "bill_status_tooltip": bill_status_tooltip,
        "district_votes": district_votes,
        "district_votes_json": json.dumps(district_votes),
        "at_large_votes": at_large_votes,
        "vote_date": vote_date,
        "amendments": amendments,
        "amendments_json": amendments_json,
        "document_table_contexts": [
            _document_table_context(document, style)
            for document in legislation.documents.all()
            if DocumentSummary.objects.filter(document=document, style=style).exists()
        ],
    }


def _document_context(document: Document, style: SummarizationStyle) -> dict:
    """
    Build context data for a `document`; this is used in our HTML templates
    that display detailed information about a single `Document` instance.
    """
    summary = get_object_or_404(DocumentSummary, document=document, style=style)
    clean_headline = _remove_surrounding_quotes(summary.headline)
    return {
        "pk": document.pk,
        "url": document.url,
        "kind": document.kind.replace("_", " ").title(),
        "title": document.short_title,
        "truncated_title": document.truncated_title,
        "headline": clean_headline,
        "truncated_headline": truncate_str(clean_headline, 24),
        "summary": _text_to_html_paragraphs(summary.body),
    }


# ------------------------------------------------------------------------
# Utilities for grabbing the right data from our database
# ------------------------------------------------------------------------

PAST_CUTOFF_DELTA = datetime.timedelta(days=8)
"""How far back in time should we still show meeting summaries?"""


def _get_relative_to(when: datetime.date | None = None) -> datetime.date:
    """Return the date to use as the "relative to" date for meeting queries."""
    final_when = when or datetime.date.today()
    return final_when - PAST_CUTOFF_DELTA


def _meetings_qs():
    """Return a Django QuerySet of all meetings that should show summaries."""
    qs = Meeting.manager.future(relative_to=_get_relative_to())
    qs = qs.exclude(time=None)
    meeting_pks_with_summaries = set(
        MeetingSummary.objects.values_list("meeting_id", flat=True)
    )
    qs = qs.filter(pk__in=meeting_pks_with_summaries)
    return qs


# ------------------------------------------------------------------------
# Django Distill functions; these define the set of static pages to generate
# ------------------------------------------------------------------------


def distill_calendars():
    """
    Provide all possible parameterizations of /calendar/:style/ so that
    Django Distill can generate all the static pages we'd like.
    """
    for style in SUMMARIZATION_STYLES:
        yield {"style": style}


def distill_meetings():
    """
    Provide all possible parameterizations of /meeting/:meeting_id/:style/
    so that Django Distill can generate all the static pages we'd like.
    """
    qs = _meetings_qs()
    for meeting in qs:
        for style in SUMMARIZATION_STYLES:
            yield {"meeting_id": meeting.legistar_id, "style": style}


def distill_legislations():
    """
    Provide all possible parameterizations of
    /legislation/:meeting_id/:legislation_id/:style/
    so that Django Distill can generate all the static pages we'd like.
    """
    qs = _meetings_qs()
    for meeting in qs:
        for legislation in meeting.legislations:
            if not legislation.summaries.exists():
                continue
            for style in SUMMARIZATION_STYLES:
                yield {
                    "meeting_id": meeting.legistar_id,
                    "legislation_id": legislation.legistar_id,
                    "style": style,
                }


def distill_documents():
    """
    Provide all possible parameterizations of
    /document/:meeting_id/:legislation_id/:document_pk/:style/
    so that Django Distill can generate all the static pages we'd like.
    """
    qs = _meetings_qs()
    for meeting in qs:
        for legislation in meeting.legislations:
            if not legislation.summaries.exists():
                continue
            for document in legislation.documents.all():
                if not document.summaries.exists():
                    continue
                for style in SUMMARIZATION_STYLES:
                    yield {
                        "meeting_id": meeting.legistar_id,
                        "legislation_id": legislation.legistar_id,
                        "document_pk": document.pk,
                        "style": style,
                    }


# ------------------------------------------------------------------------
# Django views (our actual HTTP endpoints -- invoked by Django Distill)
# ------------------------------------------------------------------------


def _build_previous_bill_entries(style: str, exclude_pks: set | None = None) -> list:
    """Return bill-entry dicts for all council bills older than the crawl window."""
    cutoff_date = datetime.date.today() - datetime.timedelta(
        days=settings.CRAWL_INTERVAL_DAYS
    )
    seen_pks = set(exclude_pks or [])
    entries = []
    older_meetings = (
        Meeting.manager.active().filter(date__lt=cutoff_date).order_by("-date")
    )
    for meeting in older_meetings:
        for legislation in meeting.legislations:
            if legislation.pk in seen_pks:
                continue
            seen_pks.add(legislation.pk)
            if _COUNCIL_BILL_KIND not in legislation.kind:
                continue
            if not LegislationSummary.objects.filter(
                legislation=legislation, style=style
            ).exists():
                continue
            leg_context = _legislation_context(legislation, style)
            kind = leg_context["kind"]
            leg_context["summary"] = leg_context["summary"].replace("*", "")
            entries.append(
                {
                    "legislation": leg_context,
                    "meeting_date": meeting.date,
                    "day_of_week": meeting.date.strftime("%A"),
                    "committee": meeting.crawl_data.department.name,
                    "meeting_id": meeting.legistar_id,
                    "is_council_bill": _COUNCIL_BILL_KIND in kind,
                    "is_informational": kind == "Informational",
                }
            )
    entries.sort(key=lambda e: e["meeting_date"], reverse=True)
    return entries


@require_GET
def calendar(request, style: str):
    """Render the calendar page as a bill-centric view."""
    if style not in SUMMARIZATION_STYLES:
        raise Http404(f"Unknown style: {style}")

    # Only show meetings within the past crawl window (previous week)
    cutoff_date = datetime.date.today() - datetime.timedelta(
        days=settings.CRAWL_INTERVAL_DAYS
    )
    meetings = Meeting.manager.active().filter(date__gte=cutoff_date).order_by("-date")

    # Build a flat list of bill entries: one per (legislation, meeting) pair
    bill_entries = []
    seen = set()  # avoid duplicates if a bill appears in multiple meetings
    for meeting in meetings:
        for legislation in meeting.legislations:
            key = (legislation.pk, meeting.pk)
            if key in seen:
                continue
            seen.add(key)
            # Only show Council Bills; other types are still summarized but hidden
            if _COUNCIL_BILL_KIND not in legislation.kind:
                continue
            if not LegislationSummary.objects.filter(
                legislation=legislation, style=style
            ).exists():
                continue
            leg_context = _legislation_context(legislation, style)
            kind = leg_context["kind"]
            leg_context["summary"] = leg_context["summary"].replace("*", "")
            bill_entries.append(
                {
                    "legislation": leg_context,
                    "meeting_date": meeting.date,
                    "day_of_week": meeting.date.strftime("%A"),
                    "committee": meeting.crawl_data.department.name,
                    "meeting_id": meeting.legistar_id,
                    "is_council_bill": _COUNCIL_BILL_KIND in kind,
                    "is_informational": kind == "Informational",
                }
            )

    # Sort by meeting date descending (newest first)
    bill_entries.sort(key=lambda e: e["meeting_date"], reverse=True)

    previous_bill_entries = _build_previous_bill_entries(
        style, exclude_pks={pk for (pk, _) in seen}
    )

    # Crawl metadata
    crawl_meta = CrawlMetadata.get_instance()
    last_crawl_at = crawl_meta.last_crawl_at if crawl_meta else None
    if last_crawl_at:
        # Parse CRAWL_TIME (e.g. "01:30") and compute next crawl
        crawl_h, crawl_m = (int(x) for x in settings.CRAWL_TIME.split(":"))
        next_crawl_at = last_crawl_at + datetime.timedelta(
            days=settings.CRAWL_INTERVAL_DAYS
        )
        next_crawl_at = next_crawl_at.replace(hour=crawl_h, minute=crawl_m, second=0)
        from django.utils import timezone

        now = timezone.now()
        next_crawl_delta_days = (next_crawl_at - now).days
    else:
        next_crawl_at = None
        next_crawl_delta_days = None

    # Compute date range: earliest meeting date through 7 days later (1-week window)
    if bill_entries:
        date_range_start = min(e["meeting_date"] for e in bill_entries)
        date_range_end = date_range_start + datetime.timedelta(
            days=settings.CRAWL_INTERVAL_DAYS
        )
    elif last_crawl_at:
        date_range_start = last_crawl_at.date()
        date_range_end = (
            last_crawl_at + datetime.timedelta(days=settings.CRAWL_INTERVAL_DAYS - 1)
        ).date()
    else:
        date_range_start = None
        date_range_end = None

    return render(
        request,
        "calendar.dhtml",
        {
            "style": style,
            "bill_entries": bill_entries,
            "previous_bill_entries": previous_bill_entries,
            "date_range_start": date_range_start,
            "date_range_end": date_range_end,
            "data_source_date": last_crawl_at or datetime.date.today(),
            "last_crawl_at": last_crawl_at,
            "next_crawl_at": next_crawl_at,
            "next_crawl_delta_days": next_crawl_delta_days,
        },
    )


@require_GET
def meeting(request, meeting_id: int, style: str):
    """Render the meeting detail page for a given `meeting_id` and `style`."""
    if style not in SUMMARIZATION_STYLES:
        raise Http404(f"Unknown style: {style}")
    meeting_ = get_object_or_404(Meeting, legistar_id=meeting_id)
    meeting_context = _meeting_context(meeting_, style)
    return render(
        request,
        "meeting.dhtml",
        {
            "style": style,
            "meeting_id": meeting_id,
            "meeting_context": meeting_context,
        },
    )


@require_GET
def legislation(request, meeting_id: int, legislation_id: int, style: str):
    """Render the legislation detail page for a given `legislation_id` and `style`."""
    if style not in SUMMARIZATION_STYLES:
        raise Http404(f"Unknown style: {style}")
    legislation_ = get_object_or_404(Legislation, legistar_id=legislation_id)
    legislation_context = _legislation_context(legislation_, style)
    return render(
        request,
        "legislation.dhtml",
        {
            "style": style,
            "meeting_id": meeting_id,
            "legislation_id": legislation_id,
            "legislation_context": legislation_context,
        },
    )


@require_GET
def document(
    request, meeting_id: int, legislation_id: int, document_pk: int, style: str
):
    """Render the document detail page for a given `document_pk` and `style`."""
    if style not in SUMMARIZATION_STYLES:
        raise Http404(f"Unknown style: {style}")
    document_ = get_object_or_404(Document, pk=document_pk)
    document_context = _document_context(document_, style)
    return render(
        request,
        "document.dhtml",
        {
            "style": style,
            "meeting_id": meeting_id,
            "legislation_id": legislation_id,
            "document_pk": document_pk,
            "document_context": document_context,
        },
    )


@require_GET
def previous_legislation(request, style: str):
    """Render the previous-legislation page."""
    if style not in SUMMARIZATION_STYLES:
        raise Http404(f"Unknown style: {style}")
    entries = _build_previous_bill_entries(style)
    return render(
        request,
        "previous_legislation.dhtml",
        {"style": style, "bill_entries": entries},
    )


@require_GET
def index(request):
    """Render the index page, which currently meta-redirects to /calendar/concise/"""
    return render(request, "index.dhtml")


# Human-readable labels for each rubric dimension, in display order.
_RUBRIC_DIMENSIONS = [
    ("headline_accuracy", "Headline Accuracy"),
    ("proposed_intent_fidelity", "Proposed Intent"),
    ("final_text_fidelity", "Final Text Fidelity"),
    ("amendment_accuracy", "Amendment Accuracy"),
    ("accessibility", "Accessibility"),
    ("neutrality", "Neutrality"),
]


def distill_previous_legislation():
    """Yield all style parameterizations for the previous-legislation static page."""
    for style in SUMMARIZATION_STYLES:
        yield {"style": style}


def distill_evaluations():
    """Yield a single empty dict so Django Distill generates evaluations/index.html."""
    yield {}


@require_GET
def evaluations(request):
    """Render the summary evaluations page."""
    evals_qs = SummaryEvaluation.objects.select_related(
        "legislation_summary__legislation"
    ).order_by("legislation_summary__legislation__record_no")

    evaluation_contexts = []
    for ev in evals_qs:
        leg = ev.legislation_summary.legislation
        completeness_pct = (
            round(ev.overall_completeness / 5 * 100)
            if ev.overall_completeness is not None
            else None
        )
        faithfulness_pct = (
            round(ev.overall_faithfulness / 5 * 100)
            if ev.overall_faithfulness is not None
            else None
        )
        dimensions = []
        for key, label in _RUBRIC_DIMENSIONS:
            dim_data = ev.scores.get(key, {})
            c_val = dim_data.get("completeness")
            f_val = dim_data.get("faithfulness")
            c_bar = round(c_val / 5 * 100) if c_val is not None else 0
            f_bar = round(f_val / 5 * 100) if f_val is not None else 0
            dimensions.append(
                {
                    "label": label,
                    "completeness": c_val if c_val is not None else "\u2014",
                    "faithfulness": f_val if f_val is not None else "\u2014",
                    "reasoning": dim_data.get("reasoning", ""),
                    "completeness_bar": c_bar,
                    "faithfulness_bar": f_bar,
                    "completeness_color": "high"
                    if c_bar >= 80
                    else "mid"
                    if c_bar >= 60
                    else "low",
                    "faithfulness_color": "high"
                    if f_bar >= 80
                    else "mid"
                    if f_bar >= 60
                    else "low",
                }
            )
        evaluation_contexts.append(
            {
                "cb_number": leg.record_no,
                "headline": ev.legislation_summary.headline,
                "completeness_pct": completeness_pct,
                "faithfulness_pct": faithfulness_pct,
                "dimensions": dimensions,
            }
        )

    return render(
        request,
        "evaluations.dhtml",
        {"evaluations": evaluation_contexts},
    )
