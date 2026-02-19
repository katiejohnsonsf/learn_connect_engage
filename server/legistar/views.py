import datetime

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.html import format_html_join
from django.views.decorators.http import require_GET

from server.documents.models import Document, DocumentSummary
from server.lib.style import SUMMARIZATION_STYLES, SummarizationStyle
from server.lib.truncate import truncate_str

from .models import CrawlMetadata, Legislation, LegislationSummary, Meeting, MeetingSummary

_SUMMARY_PENDING = "Summary pending\u2026"

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


_STRUCTURED_SECTION_HEADERS = {
    "WHAT WAS ORIGINALLY PROPOSED": "Clear 2-3 sentence summary",
    "AMENDMENTS AND VOTES": "Council member vote breakdown",
    "WHAT THE FINAL TEXT DOES": "3-4 sentence summary",
    "WHAT CHANGED FROM THE ORIGINAL": "Differences from the original proposal",
}


def _structured_summary_to_html(text: str):
    """Convert a structured summary with section headers into HTML."""
    from django.utils.html import format_html

    lines = [s.strip() for s in text.split("\n")]
    html_parts = []
    for line in lines:
        if not line:
            continue
        if line in _STRUCTURED_SECTION_HEADERS:
            desc = _STRUCTURED_SECTION_HEADERS[line]
            html_parts.append(format_html(
                '<h2 style="font-weight:700">{} <small style="font-weight:normal;color:#666">({})</small></h2>',
                line.title(), desc,
            ))
        else:
            html_parts.append(format_html("<p>{}</p>", line))
    return "\n".join(html_parts)


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
        _remove_surrounding_quotes(summary.headline)
        if summary
        else _SUMMARY_PENDING
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
        _remove_surrounding_quotes(summary.headline)
        if summary
        else _SUMMARY_PENDING
    )
    body = summary.body if summary else "This summary is being generated."

    # Use structured rendering for Council Bill summaries with section headers
    if summary and "WHAT WAS ORIGINALLY PROPOSED" in body:
        rendered_summary = _structured_summary_to_html(body)
    else:
        rendered_summary = _text_to_html_paragraphs(body)

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


@require_GET
def calendar(request, style: str):
    """Render the calendar page as a bill-centric view."""
    if style not in SUMMARIZATION_STYLES:
        raise Http404(f"Unknown style: {style}")

    # Get all active meetings (no past cutoff — include all crawled data)
    meetings = Meeting.manager.active().order_by("-date")

    # Build a flat list of bill entries: one per (legislation, meeting) pair
    bill_entries = []
    seen = set()  # avoid duplicates if a bill appears in multiple meetings
    for meeting in meetings:
        for legislation in meeting.legislations:
            key = (legislation.pk, meeting.pk)
            if key in seen:
                continue
            seen.add(key)
            if not LegislationSummary.objects.filter(
                legislation=legislation, style=style
            ).exists():
                continue
            leg_context = _legislation_context(legislation, style)
            kind = leg_context["kind"]
            leg_context["summary"] = leg_context["summary"].replace("*", "")
            bill_entries.append({
                "legislation": leg_context,
                "meeting_date": meeting.date,
                "day_of_week": meeting.date.strftime("%A"),
                "committee": meeting.crawl_data.department.name,
                "meeting_id": meeting.legistar_id,
                "is_council_bill": "Council Bill" in kind,
                "is_informational": kind == "Informational",
            })

    # Sort by meeting date descending (newest first)
    bill_entries.sort(key=lambda e: e["meeting_date"], reverse=True)

    # Compute date range
    if bill_entries:
        date_range_start = min(e["meeting_date"] for e in bill_entries)
        date_range_end = max(e["meeting_date"] for e in bill_entries)
    else:
        date_range_start = None
        date_range_end = None

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

    return render(
        request,
        "calendar.dhtml",
        {
            "style": style,
            "bill_entries": bill_entries,
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
def index(request):
    """Render the index page, which currently meta-redirects to /calendar/concise/"""
    return render(request, "index.dhtml")
