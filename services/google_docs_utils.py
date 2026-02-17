"""
Google Docs utility functions for easy integration throughout the application.
"""
import logging
from typing import Optional, Tuple
from . import create_google_doc, create_two_google_docs

logger = logging.getLogger(__name__)


async def create_transcript_google_doc(title: str, clean_transcript: str, full_transcript: str, i18n) -> Tuple[Optional[str], Optional[str]]:
    """
    Create two separate Google Docs with transcript content - one for clean and one for full transcript.
    
    Args:
        title: Document title
        clean_transcript: Clean transcript without timestamps
        full_transcript: Full transcript with timestamps
        i18n: TranslatorRunner instance for internationalization
        
    Returns:
        Tuple of (clean_doc_url, full_doc_url) or (None, None) if failed
    """
    try:
        logger.debug(f"Creating two Google Docs for: {title}")
        
        clean_url, full_url = await create_two_google_docs(
            title=title,
            clean_transcript=clean_transcript,
            full_transcript=full_transcript,
            i18n=i18n
        )
        
        if clean_url and full_url:
            logger.debug(f"Google Docs created successfully: clean={clean_url}, full={full_url}")
        else:
            logger.warning("Google Docs creation failed")
            
        return clean_url, full_url
        
    except Exception as e:
        logger.error(f"Error in create_transcript_google_doc: {e}")
        return None, None


async def get_google_docs_status() -> bool:
    """
    Check if Google Docs service is available and authenticated.
    
    Returns:
        True if service is available, False otherwise
    """
    try:
        from . import get_google_docs_service
        service = await get_google_docs_service()
        return service is not None
    except Exception as e:
        logger.error(f"Error checking Google Docs status: {e}")
        return False 