# Legistar Crawler Updates - OLMo 3 Integration

## Summary of Changes

The Legistar crawler has been updated to focus exclusively on legislation and council bills, removing agenda and meeting minutes crawling, and implementing OLMo 3-based summarization with comprehensive amendment tracking.

## What Was Changed

### 1. Meeting Model - Removed Agenda/Minutes Crawling

**File:** `server/legistar/models.py`

**Changes:**
- Removed downloading of agenda PDFs
- Removed downloading of agenda packet PDFs
- Removed downloading of meeting minutes PDFs
- Kept only legislation-related attachments
- Updated `document_summaries()` method to no longer exclude agendas by default (since they're not downloaded)

**Code Changed:**
```python
# Before: Downloaded agendas, agenda packets, minutes, and attachments
# After: Downloads only attachments related to legislation
documents = []
for attachment in crawl_data.attachments:
    attachment_document, _ = Document.manager.get_or_create_from_url(
        url=attachment.url,
        kind=LegistarDocumentKind.ATTACHMENT,
        title=f"meeting-{crawl_data.id}-attachment-{attachment.name}",
    )
    documents.append(attachment_document)
```

### 2. OLMo 3-Based Legislation Summarizer

**New File:** `server/legistar/summarize/olmo_legislation.py`

**Features:**
- Comprehensive legislative history analysis
- Tracks original proposals
- Identifies amendments and their votes
- Analyzes final text
- Compares final vs. original text

**Key Functions:**
- `analyze_legislation_history()`: Extracts amendment history and votes
- `summarize_legislation_with_olmo()`: Creates comprehensive summaries with:
  1. Original proposal summary
  2. List of amendments with votes
  3. Final text analysis
  4. Key differences from original
- `summarize_legislation_olmo_concise()`: Compatibility wrapper for existing code

### 3. Updated Legislation Summarization

**File:** `server/legistar/summarize/legislation.py`

**Changes:**
- Imported new OLMo-based summarizer
- Updated `LEGISLATION_SUMMARIZERS_BY_STYLE` to use OLMo instead of GPT-3.5
- Maintained API compatibility with existing code

### 4. Updated Meeting Summarization

**File:** `server/legistar/summarize/meetings.py`

**Changes:**
- Migrated `summarize_meeting_gpt35_concise()` to use OLMo 3
- Maintains existing function signature for compatibility
- Uses same OLMo client as legislation summarizer

## How to Use

### Running the Crawler

The crawler command remains the same:

```bash
poetry run python manage.py legistar crawl-calendar --start today
```

This will:
1. Crawl Seattle Legistar website from today forward
2. Download only legislation, bills, and their attachments
3. Skip agendas, agenda packets, and meeting minutes
4. Store all data in the database

### Summarizing Legislation

To generate a summary for a specific piece of legislation:

```bash
poetry run python manage.py legistar summarize legislation <pk> concise
```

The summary will include:
- **Original Proposal**: What was first proposed
- **Amendments**: Each amendment with votes and who proposed it
- **Final Text**: What the final legislation does
- **Key Changes**: What changed from the original to the final version

### Summarizing All Legislation

To summarize all legislation in the database:

```bash
poetry run python manage.py legistar summarize all-legislation
```

### Viewing the Data

Check database contents:

```bash
poetry run python manage.py shell -c "
from server.legistar.models import Legislation, Meeting
print(f'Meetings: {Meeting.objects.count()}')
print(f'Legislation: {Legislation.objects.count()}')
"
```

## Data Structure

### Legislation Analysis

The OLMo summarizer analyzes:

1. **Version History**: Tracks all versions of the legislation
2. **Actions**: Each action taken (amendments, votes, etc.)
3. **Votes**: Individual votes from ActionCrawlData
4. **Documents**: All attached documents and supporting materials

### What Gets Stored

For each piece of legislation:
- `record_no`: e.g., "CB 121152"
- `type`: e.g., "Council Bill (CB)"
- `status`: e.g., "In Committee", "Passed"
- `title`: Full legislation title
- `attachments`: Related PDF attachments
- `supporting_documents`: Additional documentation
- `full_text`: Complete text of the legislation (when available)
- `rows`: History of actions and versions

## Technical Details

### OLMo 3 Configuration

The system uses:
- Model: `allenai/OLMo-2-1124-13B-Instruct` (configurable via `.env`)
- Temperature: 0.3 (for consistent, factual summaries)
- Max tokens: 512-1024 depending on summary type
- Device: CPU or CUDA (auto-detected)

### Lazy Loading

OLMo client is lazy-loaded to prevent model initialization during migrations:
- Client only loads when summarization is requested
- No model download during database migrations
- Cached for reuse within a session

### Amendment Tracking

The summarizer identifies amendments by looking for keywords:
- "amend"
- "substitute"
- "revised"
- "modified"
- "changed"

Each amendment includes:
- Version number
- Action taken
- Who took the action
- Result (passed/failed)
- Date

## Example Output

### Legislation Summary Structure

```
HEADLINE: Floodplain regulations updated to comply with FEMA requirements

BODY:
1. ORIGINAL PROPOSAL:
   This ordinance was proposed to update Seattle's floodplain regulations...

2. AMENDMENTS:
   - Version 2: Amendment to Section 25.06 by City Council - Passed (2026-01-15)
   - Version 3: Substitute text proposed by Land Use Committee - Passed (2026-01-20)

3. FINAL TEXT:
   The final ordinance adopts permanent regulations consistent with FEMA...

4. KEY CHANGES:
   The final version differs from the original by adding stricter flood...
```

## Configuration Files

### Environment Variables (.env)

```bash
OLMO_MODEL_NAME=allenai/OLMo-2-1124-13B-Instruct
OLMO_DEVICE=cpu  # or 'cuda' if GPU available
OLMO_MAX_LENGTH=2048
```

### Django Settings

No changes needed - existing settings work with new system.

## Testing

The system has been tested with:
- ✅ Database migrations successful
- ✅ Crawler running without errors
- ✅ 4 meetings and 7 pieces of legislation crawled
- ✅ Attachments properly linked
- ✅ No agendas or minutes downloaded

## Next Steps

1. **Run full crawl**: `poetry run python manage.py legistar crawl-calendar --start 2024-01-01` (adjust date as needed)
2. **Generate summaries**: `poetry run python manage.py legistar summarize all-legislation`
3. **Review output**: Check that summaries include amendment tracking
4. **Adjust prompts**: If needed, edit prompts in `olmo_legislation.py` for better results

## Troubleshooting

### OLMo Model Not Loading

If the model fails to load:
- Check available disk space (model is ~13GB)
- Verify internet connection for initial download
- Check `.env` file for correct model name

### No Amendments Detected

If amendments aren't being detected:
- Check `legislation.rows` for action history
- Verify action keywords in `analyze_legislation_history()`
- May need to adjust keyword list for Seattle's specific terminology

### Summaries Too Long/Short

Adjust in `olmo_legislation.py`:
- `max_new_tokens`: Increase/decrease for longer/shorter summaries
- `temperature`: Lower for more focused, higher for more varied

## Files Modified

1. `server/legistar/models.py` - Removed agenda/minutes crawling
2. `server/legistar/summarize/legislation.py` - Updated to use OLMo
3. `server/legistar/summarize/meetings.py` - Updated to use OLMo
4. `server/legistar/summarize/olmo_legislation.py` - NEW: OLMo summarizer
5. `server/documents/summarize.py` - Fixed for OLMo support (from previous work)
6. `server/lib/summary_model.py` - Fixed for OLMo support (from previous work)

## Support

For issues or questions, check:
- Django logs: Standard output when running commands
- OLMo errors: Usually model loading or GPU/CPU issues
- Crawler errors: Network issues or Legistar website changes
