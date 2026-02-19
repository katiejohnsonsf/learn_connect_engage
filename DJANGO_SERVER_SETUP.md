# Django Development Server Setup

## Current Status

The Django development server is running successfully with all recent updates integrated.

### ✅ Server Started Successfully
```
System check identified no issues (0 silenced).
Django version 4.2.28
Starting development server at http://127.0.0.1:8000/
```

### 📊 Database Status
- **Meetings**: 4 (crawled successfully)
- **Legislation**: 7 (council bills and appointments)
- **Documents**: 35 (PDFs attached to legislation)
- **Meeting Summaries**: 0 (need to be generated)
- **Legislation Summaries**: 0 (need to be generated)
- **Document Summaries**: 0 (need to be generated)

## What's Happening Now

### Background Process Running
The `setup_summaries.py` script is currently running to prepare all summaries. This process:

1. **Extracts text from 35 PDF documents** (attachments, supporting docs)
2. **Loads OLMo 3 model** (13GB, will download if not cached)
3. **Generates document summaries** using OLMo 3
4. **Generates legislation summaries** with amendment tracking
5. **Generates meeting summaries** from legislation summaries

**Estimated time**: 15-30 minutes (depending on model download and PDF complexity)

### Monitor Progress

Check the pipeline progress:
```bash
# View live output
tail -f /private/tmp/claude-501/-Users-katiejohnson/tasks/bb72882.output

# Check database counts
poetry run python manage.py shell -c "
from server.documents.models import DocumentSummary
from server.legistar.models import LegislationSummary, MeetingSummary
print(f'Document Summaries: {DocumentSummary.objects.count()}')
print(f'Legislation Summaries: {LegislationSummary.objects.count()}')
print(f'Meeting Summaries: {MeetingSummary.objects.count()}')
"
```

### Re-run Pipeline if Needed

If the pipeline stops or you want to restart it:
```bash
# Stop the current process
# Press Ctrl+C in the terminal running the server

# Run the pipeline manually
poetry run python setup_summaries.py
```

## Web Interface

### Current Issue
The web interface requires summaries to exist before displaying content. Visiting:
- `http://127.0.0.1:8000/` - Redirects to calendar
- `http://127.0.0.1:8000/calendar/concise/` - Shows 404 until summaries are generated

### After Pipeline Completes

Once summaries are generated, you can access:

1. **Calendar View**: `http://127.0.0.1:8000/calendar/concise/`
   - Shows upcoming meetings with OLMo-generated summaries
   - Displays legislation items for each meeting

2. **Meeting Details**: `http://127.0.0.1:8000/meeting/{meeting_id}/concise/`
   - Detailed view of a specific meeting
   - Lists all legislation items discussed

3. **Legislation Details**: `http://127.0.0.1:8000/legislation/{meeting_id}/{legislation_id}/concise/`
   - Comprehensive analysis including:
     - Original proposal
     - Amendments and votes
     - Final text analysis
     - Key changes from original

4. **Document Details**: `http://127.0.0.1:8000/document/{meeting_id}/{legislation_id}/{document_pk}/concise/`
   - Individual document summaries
   - Attached PDFs and supporting materials

### Admin Interface

Access Django admin at: `http://127.0.0.1:8000/admin/`

**Login credentials** (from `.env`):
- Username: `dev@frontseat.org`
- Password: `password`

**Available models**:
- Documents
- Document Summaries
- Meetings
- Meeting Summaries
- Legislation
- Legislation Summaries

## What Was Changed for OLMo 3 Integration

### 1. Removed Agenda/Minutes Crawling
**File**: `server/legistar/models.py`

The Meeting model no longer downloads:
- ❌ Agenda PDFs
- ❌ Agenda Packet PDFs
- ❌ Minutes PDFs
- ✅ Only legislation attachments and supporting documents

### 2. OLMo 3-Based Summarization
**New File**: `server/legistar/summarize/olmo_legislation.py`

Features:
- Comprehensive legislative history analysis
- Amendment tracking with vote details
- Original vs. final text comparison
- Uses OLMo 2-1124-13B-Instruct model

### 3. Updated Summarizers
**Files Modified**:
- `server/legistar/summarize/legislation.py` - Uses OLMo instead of GPT-3.5
- `server/legistar/summarize/meetings.py` - Uses OLMo instead of GPT-3.5

## OLMo 3 Configuration

### Environment Variables
```bash
OLMO_MODEL_NAME=allenai/OLMo-2-1124-13B-Instruct
OLMO_DEVICE=cpu  # or 'cuda' for GPU
OLMO_MAX_LENGTH=2048
```

### Model Details
- **Model**: OLMo 2 (13B parameters)
- **Provider**: Allen Institute for AI
- **Temperature**: 0.3 (for factual, consistent summaries)
- **Max Tokens**: 512-1024 (depending on summary type)
- **Device**: CPU (will use CUDA if available)

## Troubleshooting

### Web Interface Shows 404
**Cause**: No summaries exist yet
**Solution**: Wait for `setup_summaries.py` to complete

### Pipeline Taking Too Long
**Cause**: Large PDF files or model download
**Solution**:
- Check internet connection for model download
- Monitor: `tail -f /private/tmp/claude-501/-Users-katiejohnson/tasks/bb72882.output`
- PDFs can take 30-60 seconds each to process

### OLMo Model Not Loading
**Cause**: Insufficient disk space or memory
**Solution**:
- Check disk space: Model is ~13GB
- Check RAM: Need 16GB+ for CPU inference
- Consider using smaller model: `allenai/OLMo-2-1124-7B-Instruct`

### Summarization Errors
**Check logs for**:
- Document extraction failures (corrupt PDFs)
- Missing dependencies (pdfplumber, docx2txt)
- OLMo API errors (model loading issues)

## Next Steps

### 1. Wait for Pipeline to Complete
Monitor progress with:
```bash
watch -n 5 'poetry run python manage.py shell -c "
from server.documents.models import DocumentSummary
from server.legistar.models import LegislationSummary, MeetingSummary
print(f\"Docs: {DocumentSummary.objects.count()}/35\")
print(f\"Legislation: {LegislationSummary.objects.count()}/7\")
print(f\"Meetings: {MeetingSummary.objects.count()}/4\")
"'
```

### 2. Access Web Interface
Once summaries are generated:
```bash
# Server should already be running
# Visit: http://127.0.0.1:8000/calendar/concise/
```

### 3. Review Summaries
Check the quality of OLMo-generated summaries:
- Do they capture key legislative details?
- Are amendments properly tracked?
- Are vote results accurately reported?

### 4. Adjust Prompts if Needed
Edit prompts in `server/legistar/summarize/olmo_legislation.py`:
- Modify temperature for more/less variation
- Adjust max_tokens for longer/shorter summaries
- Refine prompt text for better results

### 5. Run Full Crawl
Once satisfied with test data:
```bash
poetry run python manage.py legistar crawl-calendar --start 2024-01-01
poetry run python setup_summaries.py
```

## Production Deployment

For production use:
1. **Static Site Generation**:
   ```bash
   poetry run python manage.py distill-local --force
   ```

2. **Deploy Static Files**:
   - Upload to S3, GitHub Pages, or static host
   - No server needed - just HTML/CSS/JS

3. **Scheduled Updates**:
   - Set up cron job to crawl weekly
   - Regenerate summaries
   - Re-deploy static site

## Support

For issues:
- Check Django logs (terminal output)
- Review `LEGISTAR_UPDATES.md` for configuration details
- Check OLMo model status: `server/lib/olmo_client.py`
