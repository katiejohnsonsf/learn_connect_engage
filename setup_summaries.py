#!/usr/bin/env python
"""
Setup script to extract text from documents and generate summaries.
This prepares the database for the web interface.
"""
import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')
django.setup()

from server.documents.models import Document, DocumentSummary
from server.legistar.models import Legislation, LegislationSummary, Meeting, MeetingSummary
from server.lib.style import SUMMARIZATION_STYLES

def extract_all_documents():
    """Extract text from all documents."""
    print("=" * 80)
    print("STEP 1: Extracting text from documents")
    print("=" * 80)

    documents = Document.objects.filter(extracted_text="")
    total = documents.count()

    if total == 0:
        print("✓ All documents already have extracted text")
        return

    print(f"Found {total} documents without extracted text")

    for i, document in enumerate(documents, 1):
        try:
            print(f"[{i}/{total}] Extracting: {document.title[:60]}...")
            document.extract_text()
            print(f"  ✓ Extracted {len(document.extracted_text)} characters")
        except Exception as e:
            print(f"  ✗ Error: {e}")

    print()

def summarize_all_documents():
    """Summarize all documents."""
    print("=" * 80)
    print("STEP 2: Summarizing documents")
    print("=" * 80)

    # Only summarize documents with extracted text
    documents = Document.objects.exclude(extracted_text="")
    total = documents.count()

    if total == 0:
        print("⚠ No documents with extracted text found")
        return

    print(f"Found {total} documents with extracted text")

    for style in SUMMARIZATION_STYLES:
        print(f"\nUsing style: {style}")
        for i, document in enumerate(documents, 1):
            # Skip if already summarized
            if DocumentSummary.objects.filter(document=document, style=style).exists():
                print(f"[{i}/{total}] {document.title[:50]}... (already summarized)")
                continue

            try:
                print(f"[{i}/{total}] Summarizing: {document.title[:50]}...")
                summary, created = DocumentSummary.manager.get_or_create_from_document(
                    document, style
                )
                if created:
                    print(f"  ✓ {summary.headline[:60]}...")
                else:
                    print(f"  ↻ Using existing summary")
            except Exception as e:
                print(f"  ✗ Error: {e}")

    print()

def clear_council_bill_summaries():
    """Delete existing Council Bill summaries so they get regenerated with structured format."""
    print("=" * 80)
    print("STEP 2.5: Clearing Council Bill summaries for regeneration")
    print("=" * 80)

    council_bills = Legislation.objects.filter(type__icontains="Council Bill")
    cb_count = council_bills.count()

    if cb_count == 0:
        print("No Council Bills found")
        return

    # Delete legislation summaries for Council Bills
    deleted_leg = LegislationSummary.objects.filter(
        legislation__in=council_bills
    ).delete()
    print(f"Deleted {deleted_leg[0]} Council Bill legislation summaries")

    # Delete meeting summaries that depend on these Council Bills
    for meeting in Meeting.objects.filter(time__isnull=False):
        has_cb = any(
            "Council Bill" in leg.type for leg in meeting.legislations
        )
        if has_cb:
            deleted = MeetingSummary.objects.filter(meeting=meeting).delete()
            if deleted[0] > 0:
                print(f"  Deleted meeting summary for meeting {meeting.legistar_id}")

    print()


def summarize_all_legislation():
    """Summarize all legislation."""
    print("=" * 80)
    print("STEP 3: Summarizing legislation")
    print("=" * 80)

    legislations = Legislation.objects.all()
    total = legislations.count()

    if total == 0:
        print("⚠ No legislation found")
        return

    print(f"Found {total} pieces of legislation")

    for style in SUMMARIZATION_STYLES:
        print(f"\nUsing style: {style}")
        for i, legislation in enumerate(legislations, 1):
            # Skip if already summarized
            if LegislationSummary.objects.filter(legislation=legislation, style=style).exists():
                print(f"[{i}/{total}] {legislation.record_no}: (already summarized)")
                continue

            # Check if all documents are summarized
            doc_count = legislation.documents.count()
            summarized_doc_count = DocumentSummary.objects.filter(
                document__in=legislation.documents.all(),
                style=style
            ).count()

            if doc_count > 0 and summarized_doc_count < doc_count:
                print(f"[{i}/{total}] {legislation.record_no}: ⚠ Missing document summaries ({summarized_doc_count}/{doc_count})")
                continue

            try:
                print(f"[{i}/{total}] Summarizing: {legislation.record_no}...")
                summary, created = LegislationSummary.manager.get_or_create_from_legislation(
                    legislation, style
                )
                if created:
                    print(f"  ✓ {summary.headline[:60]}...")
                else:
                    print(f"  ↻ Using existing summary")
            except Exception as e:
                print(f"  ✗ Error: {e}")

    print()

def summarize_all_meetings():
    """Summarize all meetings."""
    print("=" * 80)
    print("STEP 4: Summarizing meetings")
    print("=" * 80)

    meetings = Meeting.objects.filter(time__isnull=False)  # Only active meetings
    total = meetings.count()

    if total == 0:
        print("⚠ No active meetings found")
        return

    print(f"Found {total} active meetings")

    for style in SUMMARIZATION_STYLES:
        print(f"\nUsing style: {style}")
        for i, meeting in enumerate(meetings, 1):
            # Skip if already summarized
            if MeetingSummary.objects.filter(meeting=meeting, style=style).exists():
                print(f"[{i}/{total}] Meeting {meeting.legistar_id}: (already summarized)")
                continue

            # Check if all legislation is summarized
            leg_count = meeting.legislations.count()
            summarized_leg_count = LegislationSummary.objects.filter(
                legislation__in=meeting.legislations,
                style=style
            ).count()

            if leg_count > 0 and summarized_leg_count < leg_count:
                print(f"[{i}/{total}] Meeting {meeting.legistar_id}: ⚠ Missing legislation summaries ({summarized_leg_count}/{leg_count})")
                continue

            try:
                print(f"[{i}/{total}] Summarizing meeting {meeting.legistar_id}...")
                summary, created = MeetingSummary.manager.get_or_create_from_meeting(
                    meeting, style
                )
                if created:
                    print(f"  ✓ {summary.headline[:60]}...")
                else:
                    print(f"  ↻ Using existing summary")
            except Exception as e:
                print(f"  ✗ Error: {e}")

    print()

def main():
    """Run the complete summarization pipeline."""
    print("\n" + "=" * 80)
    print("Seattle City Council - Summarization Pipeline")
    print("Using OLMo 3 for AI-powered summaries")
    print("=" * 80 + "\n")

    try:
        extract_all_documents()
        summarize_all_documents()
        clear_council_bill_summaries()
        summarize_all_legislation()
        summarize_all_meetings()

        print("=" * 80)
        print("✓ PIPELINE COMPLETE")
        print("=" * 80)
        print("\nSummary:")
        print(f"  Documents: {Document.objects.count()}")
        print(f"  Document Summaries: {DocumentSummary.objects.count()}")
        print(f"  Legislation: {Legislation.objects.count()}")
        print(f"  Legislation Summaries: {LegislationSummary.objects.count()}")
        print(f"  Meetings: {Meeting.objects.filter(time__isnull=False).count()}")
        print(f"  Meeting Summaries: {MeetingSummary.objects.count()}")
        print("\nYou can now run: poetry run python manage.py runserver")
        print("And visit: http://127.0.0.1:8000/calendar/concise/")
        print()

    except KeyboardInterrupt:
        print("\n\n⚠ Pipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n✗ Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
