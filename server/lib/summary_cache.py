"""
Caching layer for document and legislative summaries.

This module provides caching functionality to avoid regenerating
identical summaries when content hasn't changed.
"""
import hashlib
from typing import Optional, Dict, Any
from django.core.cache import cache
from django.db.models import Model


def compute_content_hash(text: str) -> str:
    """
    Compute SHA256 hash of text content.
    
    Args:
        text: Text content to hash
        
    Returns:
        Hexadecimal hash string
    """
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def get_cache_key(content_hash: str, style: str, model_name: str) -> str:
    """
    Generate cache key for a summary.
    
    Args:
        content_hash: Hash of the content being summarized
        style: Summary style (e.g., 'concise', 'detailed')
        model_name: Name of the model used for summarization
        
    Returns:
        Cache key string
    """
    return f"summary:{content_hash}:{style}:{model_name}"


class SummaryCache:
    """
    Cache for document and legislative summaries.
    
    Provides both in-memory (Django cache) and database-backed caching
    to avoid regenerating summaries for identical content.
    """
    
    def __init__(self, summary_model_class):
        """
        Initialize cache with a summary model class.
        
        Args:
            summary_model_class: Django model class for summaries
                                (e.g., DocumentSummary, BillSummary)
        """
        self.summary_model = summary_model_class
    
    def get_from_db(
        self,
        content_hash: str,
        style: str,
        model_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve summary from database.
        
        Args:
            content_hash: Hash of content
            style: Summary style
            model_name: Optional model name filter
            
        Returns:
            Dictionary with 'headline' and 'body' keys, or None
        """
        filters = {
            'content_hash': content_hash,
            'style': style,
        }
        
        if model_name:
            filters['model'] = model_name
        
        summary = self.summary_model.objects.filter(**filters).first()
        
        if summary:
            return {
                'headline': summary.headline,
                'body': summary.body,
                'model': summary.model,
                'created_at': summary.created_at,
            }
        
        return None
    
    def get_from_memory(
        self,
        content_hash: str,
        style: str,
        model_name: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve summary from in-memory cache.
        
        Args:
            content_hash: Hash of content
            style: Summary style
            model_name: Model name
            
        Returns:
            Dictionary with 'headline' and 'body' keys, or None
        """
        cache_key = get_cache_key(content_hash, style, model_name)
        return cache.get(cache_key)
    
    def set_to_memory(
        self,
        content_hash: str,
        style: str,
        model_name: str,
        summary_data: Dict[str, Any],
        timeout: int = 3600 * 24,  # 24 hours default
    ) -> None:
        """
        Store summary in in-memory cache.
        
        Args:
            content_hash: Hash of content
            style: Summary style
            model_name: Model name
            summary_data: Dictionary with 'headline' and 'body'
            timeout: Cache timeout in seconds
        """
        cache_key = get_cache_key(content_hash, style, model_name)
        cache.set(cache_key, summary_data, timeout)
    
    def get_or_generate(
        self,
        text: str,
        style: str,
        model_name: str,
        generator_func,
        parent_object: Optional[Model] = None,
        force_regenerate: bool = False,
    ) -> Dict[str, Any]:
        """
        Get cached summary or generate new one.
        
        This is the main method you'll use. It checks caches and generates
        new summaries only when necessary.
        
        Args:
            text: Text content to summarize
            style: Summary style
            model_name: Model name (e.g., 'allenai/OLMo-2-1124-13B-Instruct')
            generator_func: Function to call to generate summary if not cached.
                           Should accept (text, style) and return dict with
                           'headline' and 'body' keys.
            parent_object: Optional parent object (Document, Bill, etc.)
                          to associate with the database cache entry
            force_regenerate: If True, bypass cache and regenerate
            
        Returns:
            Dictionary with 'headline', 'body', and metadata
        """
        content_hash = compute_content_hash(text)
        
        # Check in-memory cache first (fastest)
        if not force_regenerate:
            cached = self.get_from_memory(content_hash, style, model_name)
            if cached:
                return cached
            
            # Check database cache (slower but persistent)
            cached = self.get_from_db(content_hash, style, model_name)
            if cached:
                # Populate memory cache for next time
                self.set_to_memory(content_hash, style, model_name, cached)
                return cached
        
        # Generate new summary
        print(f"Generating new summary (style={style}, hash={content_hash[:8]}...)")
        summary_data = generator_func(text, style)
        
        # Add metadata
        summary_data['model'] = model_name
        summary_data['content_hash'] = content_hash
        
        # Save to database if parent object provided
        if parent_object:
            self._save_to_db(
                parent_object,
                content_hash,
                style,
                model_name,
                summary_data,
            )
        
        # Save to memory cache
        self.set_to_memory(content_hash, style, model_name, summary_data)
        
        return summary_data
    
    def _save_to_db(
        self,
        parent_object: Model,
        content_hash: str,
        style: str,
        model_name: str,
        summary_data: Dict[str, Any],
    ) -> None:
        """
        Save summary to database.
        
        Args:
            parent_object: Parent object (Document, Bill, etc.)
            content_hash: Hash of content
            style: Summary style
            model_name: Model name
            summary_data: Summary data with 'headline' and 'body'
        """
        # Get the foreign key field name dynamically
        # This assumes your summary models have a field pointing to the parent
        # e.g., DocumentSummary has a 'document' field, BillSummary has a 'bill' field
        parent_field_name = self._get_parent_field_name(parent_object)
        
        self.summary_model.objects.update_or_create(
            **{parent_field_name: parent_object},
            style=style,
            defaults={
                'headline': summary_data.get('headline', ''),
                'body': summary_data.get('body', ''),
                'model': model_name,
                'content_hash': content_hash,
            }
        )
    
    def _get_parent_field_name(self, parent_object: Model) -> str:
        """
        Determine the field name that links to the parent object.
        
        Args:
            parent_object: Parent model instance
            
        Returns:
            Field name as string
        """
        model_name = parent_object.__class__.__name__.lower()
        
        # Common mappings
        field_mappings = {
            'document': 'document',
            'bill': 'bill',
            'amendment': 'amendment',
            'meeting': 'meeting',
            'legislation': 'legislation',
        }
        
        return field_mappings.get(model_name, model_name)
    
    def invalidate(
        self,
        content_hash: str,
        style: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        """
        Invalidate cached summaries.
        
        Args:
            content_hash: Hash of content to invalidate
            style: Optional style filter (invalidates all styles if None)
            model_name: Optional model filter
        """
        if style and model_name:
            # Invalidate specific cache entry
            cache_key = get_cache_key(content_hash, style, model_name)
            cache.delete(cache_key)
        else:
            # Invalidate all related entries (less efficient)
            # This would require iterating through possible combinations
            # For now, we'll just delete from DB
            filters = {'content_hash': content_hash}
            if style:
                filters['style'] = style
            if model_name:
                filters['model'] = model_name
            
            self.summary_model.objects.filter(**filters).delete()


# Convenience functions for common use cases

def get_document_summary_cache():
    """Get cache instance for document summaries."""
    from server.documents.models import DocumentSummary
    return SummaryCache(DocumentSummary)


def get_bill_summary_cache():
    """Get cache instance for bill summaries."""
    from server.legistar.models import BillSummary  # You'll create this
    return SummaryCache(BillSummary)


def get_legislation_summary_cache():
    """Get cache instance for legislation summaries."""
    from server.legistar.models import LegislationSummary
    return SummaryCache(LegislationSummary)