import asyncio
import json
import logging
import os
import pickle
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import AuthorizedSession, Request as GoogleAuthRequest

from fluentogram import TranslatorRunner
from services.init_bot import config

logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/drive.file'
]


class GoogleDocsClient:
    """Minimal, httplib2-free Google Docs/Drive client using requests + google-auth.

    This client avoids googleapiclient (httplib2) and talks to REST endpoints directly.
    All network I/O is synchronous under the hood and should be called via asyncio.to_thread
    from async code paths.
    """

    def __init__(
        self,
        service_account_file: Optional[str] = None,
        oauth_token_file: Optional[str] = 'oauth_token.pickle',
    ) -> None:
        self.service_account_file = service_account_file
        self.oauth_token_file = oauth_token_file
        self._auth_session: Optional[AuthorizedSession] = None

    def authenticate(self) -> bool:
        try:
            logger.debug("Starting authentication in GoogleDocsClient")
            creds: Optional[UserCredentials] = None

            if self.service_account_file and os.path.exists(self.service_account_file):
                logger.info(f"Authenticating via Service Account: {self.service_account_file}")
                creds = ServiceAccountCredentials.from_service_account_file(
                    self.service_account_file, scopes=SCOPES
                )
            elif self.oauth_token_file and os.path.exists(self.oauth_token_file):
                logger.info(f"Authenticating via stored OAuth tokens: {self.oauth_token_file}")
                with open(self.oauth_token_file, 'rb') as f:
                    creds = pickle.load(f)
                if not creds or not creds.valid:
                    if creds and creds.expired and getattr(creds, 'refresh_token', None):
                        logger.info("Refreshing expired OAuth tokens")
                        # Refresh using google-auth requests transport
                        creds.refresh(GoogleAuthRequest())
                    else:
                        logger.error("OAuth tokens are invalid and cannot be refreshed")
                        return False
            else:
                logger.error("No authentication method available (service account or OAuth tokens)")
                return False

            self._auth_session = AuthorizedSession(creds)  # type: ignore[arg-type]
            logger.debug("Authentication completed successfully")
            return True
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False

    # --- Low-level HTTP helpers ---
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        assert self._auth_session is not None, "Client is not authenticated"
        timeout = kwargs.pop('timeout', 60)
        try:
            logger.debug(f"HTTP {method} {url}")
            resp = self._auth_session.request(method, url, timeout=timeout, **kwargs)
            logger.debug(f"HTTP {method} {url} -> {resp.status_code}")
            if resp.status_code >= 400:
                body = ''
                try:
                    body = resp.text[:1000]
                except Exception:
                    pass
                logger.error(f"HTTP error {resp.status_code} for {url}: {body}")
            return resp
        except Exception as e:
            logger.error(f"HTTP request failed {method} {url}: {e}")
            raise

    # --- Docs API ---
    def create_document(self, title: str) -> str:
        url = 'https://docs.googleapis.com/v1/documents'
        logger.info(f"Creating document: title_length={len(title)}")
        resp = self._request('POST', url, json={'title': title})
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Document created: id={data.get('documentId')}")
        return data['documentId']

    def batch_update(self, document_id: str, requests_body: List[Dict[str, Any]]) -> None:
        url = f'https://docs.googleapis.com/v1/documents/{document_id}:batchUpdate'
        logger.debug(f"Batch update: doc={document_id}, requests={len(requests_body)}")
        resp = self._request('POST', url, json={'requests': requests_body})
        resp.raise_for_status()
        logger.debug(f"Batch update OK: doc={document_id}")

    def get_document(self, document_id: str) -> Dict[str, Any]:
        url = f'https://docs.googleapis.com/v1/documents/{document_id}'
        logger.debug(f"Get document: doc={document_id}")
        resp = self._request('GET', url)
        resp.raise_for_status()
        data = resp.json()
        logger.debug(f"Get document OK: doc={document_id}, title={data.get('title')}")
        return data

    # --- Drive API ---
    def make_shareable_anyone_writer(self, file_id: str) -> None:
        url = f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions'
        body = {'type': 'anyone', 'role': 'writer'}
        logger.info(f"Setting shareable permissions: file={file_id}")
        resp = self._request('POST', url, params={'fields': 'id'}, json=body)
        # Do not fail hard on permission issues, but log for visibility
        try:
            resp.raise_for_status()
            logger.info(f"Permissions set successfully: file={file_id}")
        except Exception as e:
            logger.warning(f"Failed to set shareable permissions: {e}")


# ---------------- Higher-level helpers (replicate existing behavior) -----------------

def _get_color(red: float, green: float, blue: float) -> Dict[str, Any]:
    return {'color': {'rgbColor': {'red': red, 'green': green, 'blue': blue}}}


async def _execute_batches(client: GoogleDocsClient, document_id: str, requests_all: List[Dict[str, Any]], batch_size: int = 50) -> None:
    total = len(requests_all)
    if total == 0:
        logger.debug(f"No requests to execute for doc={document_id}")
        return
    logger.debug(f"Executing {total} update requests in batches of {batch_size} for doc={document_id}")
    for i in range(0, total, batch_size):
        batch = requests_all[i:i + batch_size]
        logger.debug(f"Executing batch {i + 1}-{min(i + batch_size, total)} of {total} for doc={document_id}")
        await asyncio.to_thread(client.batch_update, document_id, batch)


async def create_enhanced_google_doc_lite(
    title: str,
    clean_transcript: str,
    full_transcript: str,
    i18n: TranslatorRunner,
    service_account_file: Optional[str] = None,
    oauth_token_file: Optional[str] = 'oauth_token.pickle'
) -> Optional[str]:
    """Create a document with two sections (clean/full) using requests-based client.

    Returns shareable URL on success, None otherwise.
    """
    client = GoogleDocsClient(service_account_file=service_account_file, oauth_token_file=oauth_token_file)
    ok = await asyncio.to_thread(client.authenticate)
    if not ok:
        logger.warning("Authentication failed in create_enhanced_google_doc_lite, returning None")
        return None

    try:
        logger.info(f"Creating enhanced Google Doc (lite): title_len={len(title)}, clean_len={len(clean_transcript)}, full_len={len(full_transcript)}")
        document_id = await asyncio.to_thread(client.create_document, title)

        # Build full text and styling similar to existing implementation
        current_pos = 1
        whisper_text = f"{i18n.google_docs_made_with_prefix()} {i18n.google_docs_whisper_ai_text()}\n\n"
        title_text = f"{title}\n\n"
        current_date = datetime.now().strftime("%d.%m.%Y")
        date_text = f"{i18n.google_docs_creation_date(date=current_date)}\n\n"

        clean_title_text = f"{i18n.google_docs_clean_version_title()}\n\n"
        clean_description = f"{i18n.google_docs_clean_version_description()}\n\n"

        full_title_text = f"{i18n.google_docs_full_version_title()}\n\n"
        full_description = f"{i18n.google_docs_full_version_description()}\n\n"

        full_document_text = (
            whisper_text + title_text + date_text +
            clean_title_text + clean_description + clean_transcript + "\n\n" +
            full_title_text + full_description + full_transcript + "\n"
        )

        reqs: List[Dict[str, Any]] = []
        reqs.append({'insertText': {'location': {'index': current_pos}, 'text': full_document_text}})

        whisper_end = current_pos + len(whisper_text)
        title_end = whisper_end + len(title_text)
        date_end = title_end + len(date_text)
        clean_title_end = date_end + len(clean_title_text)
        clean_desc_end = clean_title_end + len(clean_description)
        clean_content_end = clean_desc_end + len(clean_transcript) + 2
        full_title_end = clean_content_end + len(full_title_text)
        full_desc_end = full_title_end + len(full_description)

        # Branding style
        reqs.append({
            'updateTextStyle': {
                'range': {'startIndex': current_pos, 'endIndex': whisper_end - 2},
                'textStyle': {
                    'link': {'url': config.tg_bot.bot_url},
                    'italic': True,
                    'underline': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': _get_color(0.2, 0.2, 0.2)
                },
                'fields': 'link,italic,underline,fontSize,foregroundColor'
            }
        })

        # Title style
        reqs.append({
            'updateParagraphStyle': {
                'range': {'startIndex': whisper_end, 'endIndex': title_end - 2},
                'paragraphStyle': {'namedStyleType': 'TITLE'},
                'fields': 'namedStyleType'
            }
        })

        # Date line style
        reqs.append({
            'updateTextStyle': {
                'range': {'startIndex': title_end, 'endIndex': date_end - 2},
                'textStyle': {
                    'fontSize': {'magnitude': 11, 'unit': 'PT'},
                    'foregroundColor': _get_color(0.4, 0.4, 0.4)
                },
                'fields': 'fontSize,foregroundColor'
            }
        })

        # Clean heading
        reqs.append({
            'updateParagraphStyle': {
                'range': {'startIndex': date_end, 'endIndex': clean_title_end - 2},
                'paragraphStyle': {'namedStyleType': 'HEADING_1'},
                'fields': 'namedStyleType'
            }
        })

        # Clean description style
        reqs.append({
            'updateTextStyle': {
                'range': {'startIndex': clean_title_end, 'endIndex': clean_desc_end - 2},
                'textStyle': {
                    'italic': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': _get_color(0.5, 0.5, 0.5)
                },
                'fields': 'italic,fontSize,foregroundColor'
            }
        })

        # Full heading
        reqs.append({
            'updateParagraphStyle': {
                'range': {'startIndex': clean_content_end, 'endIndex': full_title_end - 2},
                'paragraphStyle': {'namedStyleType': 'HEADING_1'},
                'fields': 'namedStyleType'
            }
        })

        # Full description style
        reqs.append({
            'updateTextStyle': {
                'range': {'startIndex': full_title_end, 'endIndex': full_desc_end - 2},
                'textStyle': {
                    'italic': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': _get_color(0.5, 0.5, 0.5)
                },
                'fields': 'italic,fontSize,foregroundColor'
            }
        })

        await _execute_batches(client, document_id, reqs)

        # Build TOC
        # Re-fetch document to find positions is not strictly required here; we skip advanced TOC for lite flow
        # Make shareable
        await asyncio.to_thread(client.make_shareable_anyone_writer, document_id)

        url = f"https://docs.google.com/document/d/{document_id}/edit"
        logger.info(f"Enhanced Google Doc created: {url}")
        return url
    except Exception as e:
        logger.error(f"Failed to create document via lite client: {e}")
        return None


def _build_single_doc_requests(
    title: str,
    transcript: str,
    i18n: TranslatorRunner,
    transcript_type: str
) -> List[Dict[str, Any]]:
    # Content blocks
    current_pos = 1
    whisper_text = f"{i18n.google_docs_made_with_prefix()} {i18n.google_docs_whisper_ai_text()}\n\n"
    title_text = f"{title}\n\n"
    current_date = datetime.now().strftime("%d.%m.%Y")
    if transcript_type == 'clean':
        version_date_text = f"{i18n.google_docs_clean_version_title()}. {i18n.google_docs_creation_date(date=current_date)}\n\n"
    else:
        version_date_text = f"{i18n.google_docs_full_version_title()}. {i18n.google_docs_creation_date(date=current_date)}\n\n"

    full_text = whisper_text + title_text + version_date_text + transcript + "\n"

    requests: List[Dict[str, Any]] = []
    requests.append({'insertText': {'location': {'index': current_pos}, 'text': full_text}})

    whisper_end = current_pos + len(whisper_text)
    title_end = whisper_end + len(title_text)
    version_date_end = title_end + len(version_date_text)

    # Branding style with link
    requests.append({
        'updateTextStyle': {
            'range': {'startIndex': current_pos, 'endIndex': whisper_end - 2},
            'textStyle': {
                'link': {'url': config.tg_bot.bot_url},
                'italic': True,
                'underline': True,
                'fontSize': {'magnitude': 10, 'unit': 'PT'},
                'foregroundColor': _get_color(0.2, 0.2, 0.2)
            },
            'fields': 'link,italic,underline,fontSize,foregroundColor'
        }
    })

    # Title style
    requests.append({
        'updateParagraphStyle': {
            'range': {'startIndex': whisper_end, 'endIndex': title_end - 2},
            'paragraphStyle': {'namedStyleType': 'TITLE'},
            'fields': 'namedStyleType'
        }
    })

    # Version/date style
    requests.append({
        'updateTextStyle': {
            'range': {'startIndex': title_end, 'endIndex': version_date_end - 2},
            'textStyle': {
                'fontSize': {'magnitude': 11, 'unit': 'PT'},
                'foregroundColor': _get_color(0.4, 0.4, 0.4)
            },
            'fields': 'fontSize,foregroundColor'
        }
    })

    return requests


async def create_single_google_doc_lite(
    title: str,
    transcript: str,
    i18n: TranslatorRunner,
    transcript_type: str = 'clean',
    service_account_file: Optional[str] = None,
    oauth_token_file: Optional[str] = 'oauth_token.pickle'
) -> Optional[str]:
    """Create a single Google Doc (clean or full) with basic formatting."""
    client = GoogleDocsClient(service_account_file=service_account_file, oauth_token_file=oauth_token_file)
    ok = await asyncio.to_thread(client.authenticate)
    if not ok:
        logger.warning("Authentication failed in create_single_google_doc_lite, returning None")
        return None

    try:
        logger.info(f"Creating single Google Doc (lite): type={transcript_type}, title_len={len(title)}, transcript_len={len(transcript)}")
        document_id = await asyncio.to_thread(client.create_document, title)
        reqs = _build_single_doc_requests(title, transcript, i18n, transcript_type)
        await _execute_batches(client, document_id, reqs)
        await asyncio.to_thread(client.make_shareable_anyone_writer, document_id)
        url = f"https://docs.google.com/document/d/{document_id}/edit"
        logger.info(f"Single Google Doc created: {url}")
        return url
    except Exception as e:
        logger.error(f"Failed to create single document via lite client: {e}")
        return None


async def create_two_google_docs_lite(
    title: str,
    clean_transcript: str,
    full_transcript: str,
    i18n: TranslatorRunner,
    service_account_file: Optional[str] = None,
    oauth_token_file: Optional[str] = 'oauth_token.pickle'
) -> Optional[tuple[str, str]]:
    """Create two separate Google Docs: one for clean, one for full transcript.

    Returns (clean_url, full_url) on success, or None on error.
    """
    logger.info(f"Creating two Google Docs (lite): title_len={len(title)}, clean_len={len(clean_transcript)}, full_len={len(full_transcript)}")

    clean_url = await create_single_google_doc_lite(
        title=title,
        transcript=clean_transcript,
        i18n=i18n,
        transcript_type='clean',
        service_account_file=service_account_file,
        oauth_token_file=oauth_token_file
    )

    if not clean_url:
        logger.warning("Failed to create clean document, returning None")
        return None

    full_url = await create_single_google_doc_lite(
        title=title,
        transcript=full_transcript,
        i18n=i18n,
        transcript_type='full',
        service_account_file=service_account_file,
        oauth_token_file=oauth_token_file
    )

    if not full_url:
        logger.warning("Failed to create full document, returning None")
        return None

    logger.info(f"Two Google Docs created: clean={clean_url}, full={full_url}")
    return clean_url, full_url


