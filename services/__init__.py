import logging

logger = logging.getLogger(__name__)

# Global Google Docs service instance
_google_docs_service = None

async def get_google_docs_service():
    """
    Get or create the global Google Docs service instance.
    Returns None if authentication fails.
    """
    global _google_docs_service

    if _google_docs_service is None:
        try:
            from .google_docs_service import GoogleDocsService
            from services.init_bot import config

            _google_docs_service = GoogleDocsService(
                service_account_file=config.google_api.service_account_file,
                credentials_file=config.google_api.credentials_file
            )

            # Authenticate once
            if not await _google_docs_service.authenticate():
                logger.error("Failed to authenticate Google Docs service")
                _google_docs_service = None
                return None

            logger.debug("Google Docs service authenticated successfully")

        except Exception as e:
            logger.error(f"Error initializing Google Docs service: {e}")
            _google_docs_service = None
            return None

    return _google_docs_service

async def create_google_doc(title: str, clean_transcript: str, full_transcript: str, i18n) -> str | None:
    """
    Create a Google Doc with the given title and transcripts.

    Args:
        title: Document title
        clean_transcript: Clean transcript without timestamps
        full_transcript: Full transcript with timestamps
        i18n: TranslatorRunner instance for internationalization

    Returns:
        Google Docs shareable URL or None if failed
    """
    try:
        service = await get_google_docs_service()
        if service is None:
            return None

        return await service.create_enhanced_google_doc(
            title=title,
            clean_transcript=clean_transcript,
            full_transcript=full_transcript,
            i18n=i18n
        )

    except Exception as e:
        logger.error(f"Error creating Google Doc: {e}")
        return None

async def create_two_google_docs(title: str, clean_transcript: str, full_transcript: str, i18n) -> tuple[str | None, str | None]:
    """
    Create two separate Google Docs - one for clean transcript and one for full transcript.

    Args:
        title: Base document title
        clean_transcript: Clean transcript without timestamps
        full_transcript: Full transcript with timestamps
        i18n: TranslatorRunner instance for internationalization

    Returns:
        Tuple of (clean_doc_url, full_doc_url) or (None, None) if failed
    """
    try:
        service = await get_google_docs_service()
        if service is None:
            return None, None

        # Create titles for both documents (without version in title, version info is in date line)
        clean_title = title
        full_title = title

        # Create both documents
        clean_url = await service.create_single_google_doc(
            title=clean_title,
            transcript=clean_transcript,
            i18n=i18n,
            transcript_type='clean'
        )

        full_url = await service.create_single_google_doc(
            title=full_title,
            transcript=full_transcript,
            i18n=i18n,
            transcript_type='full'
        )

        return clean_url, full_url

    except Exception as e:
        logger.error(f"Error creating two Google Docs: {e}")
        return None, None
