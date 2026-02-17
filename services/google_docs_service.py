import io
import logging
import asyncio
from typing import Union, Optional, List, Dict, Any
from pathlib import Path
import json
import tempfile
import os
import pickle
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import PyPDF2
import aiofiles
from services.init_bot import config
from fluentogram import TranslatorRunner

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Scopes for Google Docs and Drive API
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/drive.file'
]


class GoogleDocsService:
    """
    A service to interact with Google Docs and Google Drive APIs.
    Enhanced with automated OAuth authentication.
    """

    def __init__(self, service_account_file: Optional[str] = None, credentials_file: Optional[str] = None,
                 oauth_client_file: Optional[str] = None, oauth_token_file: Optional[str] = None):
        """
        Initializes the Google Docs service.

        Args:
            service_account_file: Path to the service account JSON file.
            credentials_file: Path to the OAuth credentials JSON file.
            oauth_client_file: Path to the OAuth client configuration file (новый метод).
            oauth_token_file: Path to the OAuth token pickle file (новый метод).
        """
        self.service_account_file = service_account_file
        self.credentials_file = credentials_file
        self.oauth_client_file = oauth_client_file or 'oauth_client.json'
        self.oauth_token_file = oauth_token_file or 'oauth_token.pickle'
        self.creds = None
        self.docs_service = None
        self.drive_service = None
        self.auth_method = None

    async def authenticate(self) -> bool:
        """
        Authenticates with the Google API using the best available method.
        Prioritizes automated OAuth over other methods.

        Returns:
            True if authentication is successful, False otherwise.
        """
        try:
            # Метод 1: Попробуем новую автоматизированную OAuth авторизацию
            if await self._try_automated_oauth():
                logger.info("✅ Успешная авторизация через автоматизированный OAuth")
                self.auth_method = "automated_oauth"
                return True

            # Метод 2: Fallback на сервисный аккаунт
            if self.service_account_file and os.path.exists(self.service_account_file):
                logger.info(f"Пробуем авторизацию через сервисный аккаунт: {self.service_account_file}")
                self.creds = ServiceAccountCredentials.from_service_account_file(
                    self.service_account_file, scopes=SCOPES
                )
                self.auth_method = "service_account"
            
            # Метод 3: Fallback на обычный OAuth
            elif self.credentials_file and os.path.exists(self.credentials_file):
                logger.info(f"Пробуем авторизацию через OAuth: {self.credentials_file}")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                self.creds = flow.run_local_server(port=0)
                self.auth_method = "oauth"
            else:
                logger.error("Не найден ни один валидный метод авторизации!")
                return False

            # Инициализируем сервисы для fallback методов
            self.docs_service = await asyncio.to_thread(build, 'docs', 'v1', credentials=self.creds)
            self.drive_service = await asyncio.to_thread(build, 'drive', 'v3', credentials=self.creds)

            logger.info(f"✅ Успешная авторизация через {self.auth_method}")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка авторизации: {e}", exc_info=True)
            return False

    async def _try_automated_oauth(self) -> bool:
        """
        Попытка авторизации через новую автоматизированную OAuth систему.
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            logger.debug("🔍 Проверяем возможность автоматизированной OAuth авторизации...")
            
            # Проверяем наличие конфигурации OAuth
            if not os.path.exists(self.oauth_client_file):
                logger.info(f"⚠️ OAuth конфигурация не найдена: {self.oauth_client_file}")
                return False

            # Проверяем наличие сохраненных токенов
            if not os.path.exists(self.oauth_token_file):
                logger.info(f"⚠️ OAuth токены не найдены: {self.oauth_token_file}")
                logger.info("💡 Требуется первичная авторизация через браузер")
                return await self._initial_oauth_setup()

            # Загружаем и проверяем существующие токены
            async with aiofiles.open(self.oauth_token_file, 'rb') as token:
                token_data = await token.read()
                self.creds = await asyncio.to_thread(pickle.loads, token_data)

            # Проверяем валидность и обновляем если нужно
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    logger.debug("🔄 Обновляем истекшие OAuth токены...")
                    await asyncio.to_thread(self.creds.refresh, Request())
                    logger.info("✅ OAuth токены обновлены автоматически")
                    
                    # Сохраняем обновленные токены
                    token_data = await asyncio.to_thread(pickle.dumps, self.creds)
                    async with aiofiles.open(self.oauth_token_file, 'wb') as token:
                        await token.write(token_data)
                else:
                    logger.info("❌ OAuth токены невалидны, требуется реавторизация")
                    return await self._initial_oauth_setup()
            else:
                logger.info("✅ OAuth токены валидны")

            # Создаем сервисы
            self.docs_service = await asyncio.to_thread(build, 'docs', 'v1', credentials=self.creds)
            self.drive_service = await asyncio.to_thread(build, 'drive', 'v3', credentials=self.creds)

            return True

        except Exception as e:
            logger.warning(f"⚠️ Ошибка автоматизированной OAuth: {e}")
            return False

    async def _initial_oauth_setup(self) -> bool:
        """
        Первичная настройка OAuth (для серверов - авторизация через консоль).
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            logger.info("🌐 Выполняем первичную OAuth авторизацию...")
            logger.info("⚠️ Это нужно сделать ТОЛЬКО ОДИН РАЗ!")

            async with aiofiles.open(self.oauth_client_file, 'r') as f:
                client_config_str = await f.read()
                client_config = await asyncio.to_thread(json.loads, client_config_str)

            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            
            # Пробуем сначала локальный сервер (для локальных машин)
            try:
                logger.info("🔄 Попытка авторизации через локальный сервер...")
                self.creds = flow.run_local_server(port=0)
                logger.info("✅ Авторизация через локальный сервер успешна!")
                
            except Exception as browser_error:
                logger.info(f"⚠️ Локальный сервер недоступен: {browser_error}")
                logger.info("🔄 Переходим на консольную авторизацию...")
                
                # Консольная авторизация для серверов
                auth_url, _ = flow.authorization_url(prompt='consent')
                
                print("\n" + "=" * 70)
                print("🔐 СЕРВЕРНАЯ АВТОРИЗАЦИЯ - КОНСОЛЬНЫЙ РЕЖИМ")
                print("=" * 70)
                print("1. Откройте эту ссылку в любом браузере:")
                print(f"\n🔗 {auth_url}\n")
                print("2. Войдите в ваш Google аккаунт")
                print("3. Разрешите доступ к Google Docs и Drive")
                print("4. Вас перенаправит на страницу с ошибкой (это нормально!)")
                print("5. В адресной строке найдите параметр 'code=ВАШИ_СИМВОЛЫ'")
                print("6. Скопируйте значение после 'code=' и вставьте сюда")
                print("\n💡 Пример: если URL содержит 'code=4/0AVMBs...', то код это '4/0AVMBs...'")
                print("-" * 70)
                
                # Запрашиваем код у пользователя
                auth_code = await asyncio.to_thread(input, "📝 Введите код авторизации: ")
                auth_code = auth_code.strip()
                
                if not auth_code:
                    logger.error("❌ Код авторизации не был введен")
                    return False
                
                # Получаем токены по коду
                flow.fetch_token(code=auth_code)
                self.creds = flow.credentials
                
                logger.info("✅ Консольная авторизация успешна!")

            # Сохраняем токены для будущего использования
            token_data = await asyncio.to_thread(pickle.dumps, self.creds)
            async with aiofiles.open(self.oauth_token_file, 'wb') as token:
                await token.write(token_data)

            logger.info("✅ Первичная OAuth авторизация завершена")
            logger.info("🎉 Теперь можно работать БЕЗ браузера!")

            # Создаем сервисы
            self.docs_service = await asyncio.to_thread(build, 'docs', 'v1', credentials=self.creds)
            self.drive_service = await asyncio.to_thread(build, 'drive', 'v3', credentials=self.creds)

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка первичной OAuth авторизации: {e}")
            return False

    async def get_auth_status(self) -> Dict[str, Any]:
        """
        Получение подробного статуса авторизации.
        
        Returns:
            Dict with authentication status information.
        """
        status = {
            'current_method': self.auth_method,
            'is_authenticated': bool(self.docs_service and self.drive_service),
            'oauth_client_exists': os.path.exists(self.oauth_client_file),
            'oauth_tokens_exist': os.path.exists(self.oauth_token_file),
            'service_account_exists': bool(self.service_account_file and os.path.exists(self.service_account_file)),
            'credentials_file_exists': bool(self.credentials_file and os.path.exists(self.credentials_file))
        }
        
        # Проверяем валидность OAuth токенов
        if status['oauth_tokens_exist']:
            try:
                async with aiofiles.open(self.oauth_token_file, 'rb') as token:
                    token_data = await token.read()
                    creds = await asyncio.to_thread(pickle.loads, token_data)
                status['oauth_tokens_valid'] = bool(creds and creds.valid)
                status['oauth_tokens_renewable'] = bool(creds and creds.expired and creds.refresh_token)
            except:
                status['oauth_tokens_valid'] = False
                status['oauth_tokens_renewable'] = False
        else:
            status['oauth_tokens_valid'] = False
            status['oauth_tokens_renewable'] = False

        return status

    async def extract_text_from_pdf(self, pdf_data: Union[bytes, str]) -> str:
        """
        Extracts text from a PDF file asynchronously.

        Args:
            pdf_data: PDF data as bytes or a path to a PDF file.

        Returns:
            The extracted text.
        """
        def sync_extract():
            try:
                text = ""
                if isinstance(pdf_data, str):
                    with open(pdf_data, 'rb') as file:
                        pdf_reader = PyPDF2.PdfReader(file)
                        for page in pdf_reader.pages:
                            text += page.extract_text() + "\n"
                else:
                    pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_data))
                    for page in pdf_reader.pages:
                        text += page.extract_text() + "\n"
                return text.strip()
            except Exception as e:
                logger.error(f"Failed to extract text from PDF: {e}", exc_info=True)
                raise

        try:
            # Выполняем PDF операции в отдельном потоке
            text = await asyncio.to_thread(sync_extract)
            logger.debug(f"Extracted {len(text)} characters from PDF.")
            return text
        except Exception as e:
            logger.error(f"Failed to extract text from PDF: {e}", exc_info=True)
            raise

    async def create_single_google_doc(self, title: str, transcript: str, i18n: TranslatorRunner, transcript_type: str = 'clean') -> Optional[str]:
        """
        Creates a simple Google Doc with a single transcript (either clean or full).

        Args:
            title: The title of the document.
            transcript: The transcript content.
            i18n: TranslatorRunner instance for internationalization.
            transcript_type: Type of transcript ('clean' or 'full').

        Returns:
            The shareable URL of the created document, or None on error.
        """
        if not self.docs_service or not self.drive_service:
            logger.error("Services not initialized. Call authenticate() first.")
            return None

        try:
            logger.debug(f"Creating single Google Doc with title: {title}")
            doc = await asyncio.to_thread(
                lambda: self.docs_service.documents().create(body={'title': title}).execute()
            )
            document_id = doc['documentId']

            # Create simple document content
            logger.debug("Creating simple document content")
            await self._create_single_document_content(document_id, title, transcript, i18n, transcript_type)

            # Make the document publicly readable
            logger.debug("Making document shareable")
            await self._make_document_shareable(document_id)

            shareable_url = f"https://docs.google.com/document/d/{document_id}/edit"
            logger.debug(f"Single Google Doc created successfully: {shareable_url}")
            return shareable_url

        except Exception as e:
            logger.error(f"Error creating single Google Doc: {str(e)}")
            return None

    async def create_enhanced_google_doc(self, title: str, clean_transcript: str, full_transcript: str, i18n: TranslatorRunner) -> Optional[
        str]:
        """
        Creates a beautifully formatted Google Doc with a working table of contents with hyperlinks to headings.

        Args:
            title: The title of the document.
            clean_transcript: The clean transcript without timestamps.
            full_transcript: The full transcript with timestamps.
            i18n: TranslatorRunner instance for internationalization.

        Returns:
            The shareable URL of the created document, or None on error.
        """
        if not self.docs_service or not self.drive_service:
            logger.error("Services not initialized. Call authenticate() first.")
            return None

        try:
            logger.info(f"Creating enhanced Google Doc with title: {title}")
            doc = await asyncio.to_thread(
                lambda: self.docs_service.documents().create(body={'title': title}).execute()
            )
            document_id = doc['documentId']

            # Phase 1: Create document content with headings
            logger.info("Phase 1: Creating document content with headings")
            await self._create_document_content(document_id, title, clean_transcript, full_transcript, i18n)

            # Phase 2: Read document to get heading IDs
            logger.info("Phase 2: Reading document to get heading IDs")
            heading_ids = await self._get_heading_ids(document_id, i18n)

            # Phase 3: Create table of contents with links
            logger.info("Phase 3: Creating table of contents with links")
            await self._create_table_of_contents(document_id, heading_ids, i18n)

            # Make the document publicly readable
            logger.info("Making document shareable")
            await self._make_document_shareable(document_id)

            shareable_url = f"https://docs.google.com/document/d/{document_id}/edit"
            logger.info(f"Enhanced Google Doc created successfully: {shareable_url}")
            return shareable_url

        except Exception as e:
            logger.error(f"Error creating enhanced Google Doc: {str(e)}")
            return None

    async def _create_single_document_content(self, document_id: str, title: str, transcript: str, i18n: TranslatorRunner, transcript_type: str):
        """Create simple document content with a single transcript."""
        
        requests = []
        current_pos = 1

        # --- CONTENT DEFINITIONS ---
        whisper_text = f"{i18n.google_docs_made_with_prefix()} {i18n.google_docs_whisper_ai_text()}\n\n"
        title_text = f"{title}\n\n"
        current_date = datetime.now().strftime("%d.%m.%Y")
        
        # Combine version type with date
        if transcript_type == 'clean':
            version_date_text = f"{i18n.google_docs_clean_version_title()}. {i18n.google_docs_creation_date(date=current_date)}\n\n"
        else:
            version_date_text = f"{i18n.google_docs_full_version_title()}. {i18n.google_docs_creation_date(date=current_date)}\n\n"

        # Build document text with just one transcript
        full_document_text = whisper_text + title_text + version_date_text + transcript + "\n"
        
        # Insert all text at once
        requests.append({'insertText': {'location': {'index': current_pos}, 'text': full_document_text}})

        # --- CALCULATE POSITIONS ---
        whisper_end = current_pos + len(whisper_text)
        title_end = whisper_end + len(title_text)
        version_date_end = title_end + len(version_date_text)

        # --- STYLING ---
        
        # 1. Format Whisper AI branding (small, gray, with link)
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': current_pos, 'endIndex': whisper_end - 2},
                'textStyle': {
                    'link': {'url': config.tg_bot.bot_url},
                    'italic': True,
                    'underline': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': {'color': {'rgbColor': {'red': 0.2, 'green': 0.2, 'blue': 0.2}}}
                },
                'fields': 'link,italic,underline,fontSize,foregroundColor'
            }
        })

        # 2. Format document title with TITLE style
        requests.append({
            'updateParagraphStyle': {
                'range': {'startIndex': whisper_end, 'endIndex': title_end - 2},
                'paragraphStyle': {'namedStyleType': 'TITLE'},
                'fields': 'namedStyleType'
            }
        })

        # 3. Format version and date line (small, gray)
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': title_end, 'endIndex': version_date_end - 2},
                'textStyle': {
                    'fontSize': {'magnitude': 11, 'unit': 'PT'},
                    'foregroundColor': {'color': {'rgbColor': {'red': 0.4, 'green': 0.4, 'blue': 0.4}}}
                },
                'fields': 'fontSize,foregroundColor'
            }
        })

        # Execute batch update
        await self._execute_batch_update(document_id, requests)

    async def _create_document_content(self, document_id: str, title: str, clean_transcript: str, full_transcript: str, i18n: TranslatorRunner):
        """Phase 1: Create document content with headings (but no table of contents yet)."""
        
        requests = []
        current_pos = 1

        # --- CONTENT DEFINITIONS ---
        whisper_text = f"{i18n.google_docs_made_with_prefix()} {i18n.google_docs_whisper_ai_text()}\n\n"
        title_text = f"{title}\n\n"
        current_date = datetime.now().strftime("%d.%m.%Y")
        date_text = f"{i18n.google_docs_creation_date(date=current_date)}\n\n"

        clean_title_text = f"{i18n.google_docs_clean_version_title()}\n\n"
        clean_description = f"{i18n.google_docs_clean_version_description()}\n\n"

        full_title_text = f"{i18n.google_docs_full_version_title()}\n\n"
        full_description = f"{i18n.google_docs_full_version_description()}\n\n"

        # --- BUILD COMPLETE DOCUMENT TEXT ---
        full_document_text = (
            whisper_text + title_text + date_text +
            clean_title_text + clean_description + clean_transcript + "\n\n" +
            full_title_text + full_description + full_transcript + "\n"
        )
        
        # Insert all text at once
        requests.append({'insertText': {'location': {'index': current_pos}, 'text': full_document_text}})

        # --- CALCULATE POSITIONS ---
        whisper_end = current_pos + len(whisper_text)
        title_end = whisper_end + len(title_text)
        date_end = title_end + len(date_text)
        clean_title_end = date_end + len(clean_title_text)
        clean_desc_end = clean_title_end + len(clean_description)
        clean_content_end = clean_desc_end + len(clean_transcript) + 2
        full_title_end = clean_content_end + len(full_title_text)
        full_desc_end = full_title_end + len(full_description)

        # --- STYLING ---
        
        # 1. Format Whisper AI branding (small, gray, with link)
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': current_pos, 'endIndex': whisper_end - 2},
                'textStyle': {
                    'link': {'url': config.tg_bot.bot_url},
                    'italic': True,
                    'underline': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': {'color': {'rgbColor': {'red': 0.2, 'green': 0.2, 'blue': 0.2}}}
                },
                'fields': 'link,italic,underline,fontSize,foregroundColor'
            }
        })

        # 2. Format document title with TITLE style
        requests.append({
            'updateParagraphStyle': {
                'range': {'startIndex': whisper_end, 'endIndex': title_end - 2},
                'paragraphStyle': {'namedStyleType': 'TITLE'},
                'fields': 'namedStyleType'
            }
        })

        # 3. Format creation date (small, gray)
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': title_end, 'endIndex': date_end - 2},
                'textStyle': {
                    'fontSize': {'magnitude': 11, 'unit': 'PT'},
                    'foregroundColor': {'color': {'rgbColor': {'red': 0.4, 'green': 0.4, 'blue': 0.4}}}
                },
                'fields': 'fontSize,foregroundColor'
            }
        })

        # 4. Format "Чистая версия" as HEADING_1
        requests.append({
            'updateParagraphStyle': {
                'range': {'startIndex': date_end, 'endIndex': clean_title_end - 2},
                'paragraphStyle': {'namedStyleType': 'HEADING_1'},
                'fields': 'namedStyleType'
            }
        })

        # 5. Format clean description (italic, small, gray)
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': clean_title_end, 'endIndex': clean_desc_end - 2},
                'textStyle': {
                    'italic': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': {'color': {'rgbColor': {'red': 0.5, 'green': 0.5, 'blue': 0.5}}}
                },
                'fields': 'italic,fontSize,foregroundColor'
            }
        })

        # 6. Format "Полная версия" as HEADING_1
        requests.append({
            'updateParagraphStyle': {
                'range': {'startIndex': clean_content_end, 'endIndex': full_title_end - 2},
                'paragraphStyle': {'namedStyleType': 'HEADING_1'},
                'fields': 'namedStyleType'
            }
        })

        # 7. Format full description (italic, small, gray)
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': full_title_end, 'endIndex': full_desc_end - 2},
                'textStyle': {
                    'italic': True,
                    'fontSize': {'magnitude': 10, 'unit': 'PT'},
                    'foregroundColor': {'color': {'rgbColor': {'red': 0.5, 'green': 0.5, 'blue': 0.5}}}
                },
                'fields': 'italic,fontSize,foregroundColor'
            }
        })

        # Execute batch update
        await self._execute_batch_update(document_id, requests)

    async def _get_heading_ids(self, document_id: str, i18n: TranslatorRunner) -> Dict[str, str]:
        """Phase 2: Read document and extract heading IDs."""
        
        try:
            # Get document with content
            doc = await asyncio.to_thread(
                lambda: self.docs_service.documents().get(documentId=document_id).execute()
            )
            
            heading_ids = {}
            
            # Look through document content for headings
            body_content = doc.get('body', {}).get('content', [])
            
            for element in body_content:
                if 'paragraph' in element:
                    paragraph = element['paragraph']
                    paragraph_style = paragraph.get('paragraphStyle', {})
                    
                    # Check if it's a heading
                    if paragraph_style.get('namedStyleType') == 'HEADING_1':
                        # Get heading ID
                        heading_id = paragraph_style.get('headingId')
                        
                        if heading_id:
                            # Get the text content of the heading
                            heading_text = ''
                            for para_element in paragraph.get('elements', []):
                                if 'textRun' in para_element:
                                    heading_text += para_element['textRun'].get('content', '')
                            
                            # Clean up the text (remove newlines, extra spaces)
                            heading_text = heading_text.strip()
                            
                            # Store the mapping using localized text
                            clean_version_title = i18n.google_docs_clean_version_title()
                            full_version_title = i18n.google_docs_full_version_title()
                            
                            if clean_version_title in heading_text:
                                heading_ids['clean'] = heading_id
                            elif full_version_title in heading_text:
                                heading_ids['full'] = heading_id
                            
                            logger.info(f"Found heading: '{heading_text}' with ID: {heading_id}")
            
            logger.info(f"Found {len(heading_ids)} headings: {heading_ids}")
            return heading_ids
            
        except Exception as e:
            logger.error(f"Error reading document for heading IDs: {str(e)}")
            return {}

    def _get_style_request(self, start: int, end: int, text_style: Dict[str, Any]) -> Dict[str, Any]:
        """Helper to create a text style update request."""
        fields = ",".join(text_style.keys())
        return {
            'updateTextStyle': {
                'range': {'startIndex': start, 'endIndex': end},
                'textStyle': text_style,
                'fields': fields
            }
        }

    def _get_color(self, red: float, green: float, blue: float) -> Dict[str, Any]:
        """Helper to create a color object."""
        return {'color': {'rgbColor': {'red': red, 'green': green, 'blue': blue}}}

    async def _execute_batch_update(self, document_id: str, requests: List[Dict[str, Any]]):
        """Executes batchUpdate requests in chunks."""
        batch_size = 50
        for i in range(0, len(requests), batch_size):
            batch = requests[i:i + batch_size]
            await asyncio.to_thread(
                lambda: self.docs_service.documents().batchUpdate(
                    documentId=document_id,
                    body={'requests': batch}
                ).execute()
            )
            logger.debug(f"Executed batch update: {i + 1}-{min(i + batch_size, len(requests))} of {len(requests)}")

    async def _make_document_shareable(self, document_id: str):
        """Make document publicly readable and return shareable URL."""
        try:
            # Set permissions to allow anyone with the link to read
            permission = {
                'type': 'anyone',
                'role': 'writer'
            }
            
            await asyncio.to_thread(
                lambda: self.drive_service.permissions().create(
                    fileId=document_id,
                    body=permission,
                    fields='id'
                ).execute()
            )
            
            logger.debug(f"Document {document_id} made publicly readable")
            
        except Exception as e:
            logger.error(f"Error making document shareable: {str(e)}")
            # Don't fail the entire process if sharing fails
            pass

    async def _create_table_of_contents(self, document_id: str, heading_ids: Dict[str, str], i18n: TranslatorRunner):
        """Phase 3: Create table of contents with links to headings."""
        
        if not heading_ids:
            logger.warning("No heading IDs found, skipping table of contents creation")
            return
        
        try:
            # Insert table of contents after the date (find the position)
            # We need to read the document again to find the right position
            doc = await asyncio.to_thread(
                lambda: self.docs_service.documents().get(documentId=document_id).execute()
            )
            
            # Find position after date line
            toc_position = None
            body_content = doc.get('body', {}).get('content', [])
            
            # Get localized search text to find the date line
            creation_date_text = i18n.google_docs_creation_date(date="").split(":")[0]  # Get just the prefix
            
            for element in body_content:
                if 'paragraph' in element:
                    paragraph = element['paragraph']
                    # Look for paragraph with creation date text
                    for para_element in paragraph.get('elements', []):
                        if 'textRun' in para_element:
                            text_content = para_element['textRun'].get('content', '')
                            if creation_date_text in text_content:
                                toc_position = element.get('endIndex', 0)
                                break
                    if toc_position:
                        break
            
            if not toc_position:
                logger.error("Could not find position for table of contents")
                return
            
            # Create table of contents content with better formatting
            separator_paragraph = "\n"  # Empty paragraph that will get a border
            toc_title = f"{i18n.google_docs_toc_title()}\n\n"
            toc_clean_item = f"{i18n.google_docs_toc_clean_item()}\n"
            toc_full_item = f"{i18n.google_docs_toc_full_item()}\n\n"
            
            # Insert table of contents text
            requests = [
                {'insertText': {'location': {'index': toc_position}, 'text': separator_paragraph}},
                {'insertText': {'location': {'index': toc_position + len(separator_paragraph)}, 'text': toc_title}},
                {'insertText': {'location': {'index': toc_position + len(separator_paragraph) + len(toc_title)}, 'text': toc_clean_item}},
                {'insertText': {'location': {'index': toc_position + len(separator_paragraph) + len(toc_title) + len(toc_clean_item)}, 'text': toc_full_item}}
            ]
            
            # Calculate positions for styling
            sep_end = toc_position + len(separator_paragraph)
            toc_title_start = sep_end
            toc_title_end = toc_title_start + len(toc_title)
            clean_item_start = toc_title_end
            clean_item_end = clean_item_start + len(toc_clean_item)
            full_item_start = clean_item_end
            full_item_end = full_item_start + len(toc_full_item)
            
            # Create a separator using paragraph border
            requests.append({
                'updateParagraphStyle': {
                    'range': {'startIndex': toc_position, 'endIndex': sep_end},
                    'paragraphStyle': {
                        'borderBottom': {
                            'color': {'color': {'rgbColor': {'red': 0.7, 'green': 0.7, 'blue': 0.7}}},
                            'width': {'magnitude': 1, 'unit': 'PT'},
                            'padding': {'magnitude': 6, 'unit': 'PT'},
                            'dashStyle': 'SOLID'
                        }
                    },
                    'fields': 'borderBottom'
                }
            })
            
            # Style the table of contents title (bold, larger font, left aligned)
            requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': toc_title_start, 'endIndex': toc_title_end - 2},
                    'textStyle': {
                        'bold': True,
                        'fontSize': {'magnitude': 16, 'unit': 'PT'},
                        'foregroundColor': {'color': {'rgbColor': {'red': 0.2, 'green': 0.2, 'blue': 0.2}}}
                    },
                    'fields': 'bold,fontSize,foregroundColor'
                }
            })
            
            # Left align the title (default alignment, but being explicit)
            requests.append({
                'updateParagraphStyle': {
                    'range': {'startIndex': toc_title_start, 'endIndex': toc_title_end - 2},
                    'paragraphStyle': {'alignment': 'START'},
                    'fields': 'alignment'
                }
            })
            
            # Style table of contents items
            requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': clean_item_start, 'endIndex': full_item_end - 2},
                    'textStyle': {
                        'fontSize': {'magnitude': 12, 'unit': 'PT'},
                        'foregroundColor': {'color': {'rgbColor': {'red': 0.3, 'green': 0.3, 'blue': 0.3}}}
                    },
                    'fields': 'fontSize,foregroundColor'
                }
            })
            
            # Add links to headings with proper styling
            if 'clean' in heading_ids:
                # Find position of clean version text in the item
                clean_version_title = i18n.google_docs_clean_version_title()
                clean_link_start = clean_item_start + 3  # Skip "1. "
                clean_link_end = clean_link_start + len(clean_version_title)
                
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': clean_link_start, 'endIndex': clean_link_end},
                        'textStyle': {
                            'link': {'headingId': heading_ids['clean']},
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.0, 'green': 0.0, 'blue': 0.8}}},
                            'underline': True
                        },
                        'fields': 'link,foregroundColor,underline'
                    }
                })
            
            if 'full' in heading_ids:
                # Find position of full version text in the item
                full_version_title = i18n.google_docs_full_version_title()
                full_link_start = full_item_start + 3  # Skip "2. "
                full_link_end = full_link_start + len(full_version_title)
                
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': full_link_start, 'endIndex': full_link_end},
                        'textStyle': {
                            'link': {'headingId': heading_ids['full']},
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.0, 'green': 0.0, 'blue': 0.8}}},
                            'underline': True
                        },
                        'fields': 'link,foregroundColor,underline'
                    }
                })
            
            # Execute batch update
            await self._execute_batch_update(document_id, requests)
            logger.info("Table of contents with professional formatting and links created successfully")
            
        except Exception as e:
            logger.error(f"Error creating table of contents: {str(e)}")
            # Don't fail the entire process if TOC creation fails
            pass


async def upload_transcript_to_google_docs(
        full_transcript: str,
        clean_transcript: str,
        i18n: TranslatorRunner,
        title: Optional[str] = None,
        service_account_file: Optional[str] = None,
        credentials_file: Optional[str] = None,
        oauth_client_file: Optional[str] = None,
        oauth_token_file: Optional[str] = None
) -> Optional[str]:
    """
    Main function to upload a transcript to Google Docs with enhanced formatting and navigation.
    Uses automated OAuth by default for better reliability.

    Args:
        full_transcript: The full transcript with timestamps.
        clean_transcript: The clean transcript without timestamps.
        i18n: TranslatorRunner instance for internationalization.
        title: The title of the document. If None, uses localized default.
        service_account_file: Path to the service account JSON file (fallback).
        credentials_file: Path to the OAuth credentials JSON file (fallback).
        oauth_client_file: Path to the OAuth client configuration file (recommended).
        oauth_token_file: Path to the OAuth token pickle file (recommended).

    Returns:
        The shareable URL of the document, or None on error.
    """
    logger.info("🚀 Запуск процесса загрузки транскрипции в Google Docs с автоматизированной авторизацией")
    try:
        # Use default title if not provided
        if title is None:
            title = i18n.google_docs_default_title()
        
        google_service = GoogleDocsService(
            service_account_file=service_account_file,
            credentials_file=credentials_file,
            oauth_client_file=oauth_client_file,
            oauth_token_file=oauth_token_file
        )

        if not await google_service.authenticate():
            logger.error("❌ Не удалось выполнить авторизацию")
            return None

        # Выводим информацию о методе авторизации
        status = await google_service.get_auth_status()
        logger.info(f"✅ Авторизация выполнена через: {status['current_method']}")

        share_url = await google_service.create_enhanced_google_doc(title, clean_transcript, full_transcript, i18n)

        if share_url:
            logger.info(f"✅ Успешно загружена транскрипция в Google Docs: {share_url}")
        else:
            logger.error("❌ Не удалось создать документ Google Docs")

        return share_url

    except Exception as e:
        logger.error(f"❌ Ошибка в upload_transcript_to_google_docs: {e}", exc_info=True)
        return None


async def create_enhanced_google_docs_automated(
        title: str,
        clean_transcript: str,
        full_transcript: str,
        i18n: TranslatorRunner,
        oauth_client_file: str = 'oauth_client.json',
        oauth_token_file: str = 'oauth_token.pickle'
) -> Dict[str, Any]:
    """
    Автоматизированная функция для создания документов Google Docs с расширенным форматированием.
    Работает полностью автоматически после первичной настройки.

    Args:
        title: Название документа.
        clean_transcript: Чистая транскрипция без временных меток.
        full_transcript: Полная транскрипция с временными метками.
        i18n: TranslatorRunner instance for internationalization.
        oauth_client_file: Путь к файлу конфигурации OAuth клиента.
        oauth_token_file: Путь к файлу с сохраненными токенами.

    Returns:
        Dict с информацией о результате создания документа.
    """
    try:
        logger.info(f"📝 Автоматическое создание документа: '{title}'")
        
        google_service = GoogleDocsService(
            oauth_client_file=oauth_client_file,
            oauth_token_file=oauth_token_file
        )

        # Проверяем статус готовности  
        status = await google_service.get_auth_status()
        
        if not await google_service.authenticate():
            return {
                'success': False,
                'error': 'Авторизация не пройдена',
                'method': 'automated_oauth',
                'status': status
            }

        # Создаем документ с расширенным форматированием
        share_url = await google_service.create_enhanced_google_doc(title, clean_transcript, full_transcript, i18n)

        if share_url:
            # Извлекаем ID документа из URL
            document_id = share_url.split('/d/')[1].split('/')[0] if '/d/' in share_url else None
            
            result = {
                'document_id': document_id,
                'title': title,
                'url': share_url,
                'success': True,
                'created_at': datetime.now().isoformat(),
                'method': google_service.auth_method or 'automated_oauth',
                'features': ['enhanced_formatting', 'table_of_contents', 'navigation_links']
            }
            
            logger.info(f"🎉 Документ '{title}' создан автоматически!")
            logger.info(f"🔗 Ссылка: {share_url}")
            return result
        else:
            return {
                'success': False,
                'error': 'Не удалось создать документ',
                'method': google_service.auth_method or 'automated_oauth'
            }

    except Exception as e:
        logger.error(f"❌ Ошибка автоматического создания документа: {e}")
        return {
            'success': False,
            'error': str(e),
            'method': 'automated_oauth'
        }


# Example usage
if __name__ == "__main__":
    async def test():
        # Configure logging for the test
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        print("🚀 ТЕСТИРОВАНИЕ АВТОМАТИЗИРОВАННОГО GOOGLE DOCS API")
        print("🎯 Полностью автоматическая работа после первичной настройки")
        print("=" * 80)

        # --- Test Data ---
        full_transcript = """[00:01 - 00:05] SPEAKER_1
Привет! Меня зовут Алексей. Это тестовая расшифровка для демонстрации новой системы авторизации.

[00:05 - 00:12] SPEAKER_2
Привет, Алексей! Как дела? Новая система авторизации работает намного лучше.

[00:12 - 00:20] SPEAKER_1
Спасибо, все отлично! А у тебя как? Теперь не нужно каждый раз авторизоваться через браузер.

[00:20 - 00:25] SPEAKER_2
Да, это очень удобно для серверных приложений.""" * 5  # Making it longer to see navigation benefits

        clean_transcript = """Привет! Меня зовут Алексей. Это тестовая расшифровка для демонстрации новой системы авторизации.
Привет, Алексей! Как дела? Новая система авторизации работает намного лучше.
Спасибо, все отлично! А у тебя как? Теперь не нужно каждый раз авторизоваться через браузер.
Да, это очень удобно для серверных приложений.""" * 5

        # --- НОВАЯ СИСТЕМА АВТОРИЗАЦИИ (РЕКОМЕНДУЕТСЯ) ---
        # Автоматизированная OAuth - требует браузер только ОДИН раз
        oauth_client_file = "oauth_client.json"  # Скачать из Google Cloud Console
        oauth_token_file = "oauth_token.pickle"  # Создается автоматически после первой авторизации

        # --- FALLBACK МЕТОДЫ ---
        # Service Account (может работать нестабильно)
        service_account_file = "phonic-agility-406009-67560e050658.json"
        
        # Обычный OAuth (требует браузер каждый раз)
        # credentials_file = "path/to/your/credentials.json"

        print(f"\n🔍 ПРОВЕРКА ДОСТУПНЫХ МЕТОДОВ АВТОРИЗАЦИИ:")
        print(f"   OAuth клиент: {'✅' if os.path.exists(oauth_client_file) else '❌'} {oauth_client_file}")
        print(f"   OAuth токены: {'✅' if os.path.exists(oauth_token_file) else '❌'} {oauth_token_file}")
        print(f"   Сервисный аккаунт: {'✅' if os.path.exists(service_account_file) else '❌'} {service_account_file}")

        # Тестируем автоматизированную функцию
        print(f"\n📝 ТЕСТ 1: Автоматизированная функция создания документа")
        result = await create_enhanced_google_docs_automated(
            title=f"Автоматическая транскрипция {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            clean_transcript=clean_transcript,
            full_transcript=full_transcript,
            oauth_client_file=oauth_client_file,
            oauth_token_file=oauth_token_file
        )

        if result['success']:
            print(f"✅ ТЕСТ 1 ПРОЙДЕН!")
            print(f"📝 Документ: {result['title']}")
            print(f"🔗 URL: {result['url']}")
            print(f"⚙️ Метод авторизации: {result['method']}")
            print(f"🎨 Возможности: {', '.join(result['features'])}")
        else:
            print(f"❌ ТЕСТ 1 НЕ ПРОЙДЕН: {result['error']}")

        # Тестируем основную функцию с fallback
        print(f"\n📝 ТЕСТ 2: Основная функция с автоматическим выбором метода авторизации")
        url = await upload_transcript_to_google_docs(
            full_transcript=full_transcript,
            clean_transcript=clean_transcript,
            title=f"Fallback транскрипция {datetime.now().strftime('%H:%M')}",
            oauth_client_file=oauth_client_file,
            oauth_token_file=oauth_token_file,
            service_account_file=service_account_file if os.path.exists(service_account_file) else None
        )

        if url:
            print(f"✅ ТЕСТ 2 ПРОЙДЕН!")
            print(f"🔗 Документ создан: {url}")
        else:
            print(f"❌ ТЕСТ 2 НЕ ПРОЙДЕН")

        # Финальные рекомендации
        print(f"\n" + "=" * 80)
        print(f"💡 РЕКОМЕНДАЦИИ ПО НАСТРОЙКЕ:")
        print(f"1. 🌐 Создайте OAuth клиента в Google Cloud Console")
        print(f"2. 📁 Скачайте файл конфигурации как {oauth_client_file}")
        print(f"3. 🔐 Выполните первичную авторизацию (потребуется браузер ОДИН раз)")
        print(f"4. 🎉 После этого система будет работать ПОЛНОСТЬЮ автоматически!")
        print(f"\n🚀 ДЛЯ ПРОДАКШЕН СЕРВЕРОВ:")
        print(f"   from services.google_docs_service import create_enhanced_google_docs_automated")
        print(f"   result = await create_enhanced_google_docs_automated(title, clean, full)")


    # To run the test, you need an event loop
    try:
        asyncio.run(test())
    except Exception as e:
        print(f"❌ Ошибка во время тестирования: {e}")
