#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Автоматизированное OAuth решение для серверов
Требует браузер только ОДИН раз, затем работает автономно
"""

import json
import os
import pickle
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive'
]

class AutomatedOAuthDocsCreator:
    def __init__(self, client_config_file='oauth_client.json', token_file='oauth_token.pickle'):
        """
        Автоматизированный OAuth создатель документов
        
        Args:
            client_config_file (str): Файл конфигурации OAuth клиента
            token_file (str): Файл для сохранения токенов
        """
        self.client_config_file = client_config_file
        self.token_file = token_file
        self.credentials = None
        self.docs_service = None
        self.drive_service = None
        
        # Проверяем готовность к автоматической работе
        self.is_ready = self._check_readiness()
        
        if self.is_ready:
            self._authenticate()
    
    def _check_readiness(self):
        """Проверка готовности к автоматической работе"""
        print("🔍 ПРОВЕРКА ГОТОВНОСТИ К АВТОМАТИЧЕСКОЙ РАБОТЕ")
        print("=" * 60)
        
        # Проверяем наличие конфигурации OAuth
        if not os.path.exists(self.client_config_file):
            print(f"❌ Файл {self.client_config_file} не найден")
            print(f"💡 Создайте OAuth клиента в Google Cloud Console")
            return False
        else:
            print(f"✅ OAuth конфигурация найдена")
        
        # Проверяем наличие сохраненных токенов
        if not os.path.exists(self.token_file):
            print(f"⚠️ Токены не найдены ({self.token_file})")
            print(f"💡 Потребуется РАЗОВАЯ авторизация через браузер")
            return False
        else:
            print(f"✅ Сохраненные токены найдены")
        
        # Проверяем валидность токенов
        try:
            with open(self.token_file, 'rb') as token:
                creds = pickle.load(token)
                
            if creds and creds.valid:
                print(f"✅ Токены валидны - ПОЛНОСТЬЮ АВТОМАТИЧЕСКАЯ РАБОТА")
                return True
            elif creds and creds.expired and creds.refresh_token:
                print(f"⚠️ Токены истекли, но можно обновить - АВТОМАТИЧЕСКАЯ РАБОТА")
                return True
            else:
                print(f"❌ Токены невалидны - потребуется реавторизация")
                return False
                
        except Exception as e:
            print(f"❌ Ошибка проверки токенов: {e}")
            return False
    
    def _authenticate(self):
        """Автоматическая аутентификация"""
        try:
            print(f"\n🔐 АВТОМАТИЧЕСКАЯ АУТЕНТИФИКАЦИЯ")
            print("-" * 40)
            
            # Загружаем существующие токены
            if os.path.exists(self.token_file):
                with open(self.token_file, 'rb') as token:
                    self.credentials = pickle.load(token)
            
            # Обновляем токены если нужно
            if not self.credentials or not self.credentials.valid:
                if self.credentials and self.credentials.expired and self.credentials.refresh_token:
                    print("🔄 Обновление истекших токенов...")
                    self.credentials.refresh(Request())
                    print("✅ Токены обновлены автоматически")
                else:
                    print("🔐 Требуется первичная авторизация...")
                    return self._initial_authorization()
            else:
                print("✅ Токены валидны")
            
            # Сохраняем обновленные токены
            with open(self.token_file, 'wb') as token:
                pickle.dump(self.credentials, token)
            
            # Создаем сервисы
            self.docs_service = build('docs', 'v1', credentials=self.credentials)
            self.drive_service = build('drive', 'v3', credentials=self.credentials)
            
            print("✅ Автоматическая аутентификация завершена")
            return True
            
        except Exception as e:
            print(f"❌ Ошибка автоматической аутентификации: {e}")
            return False
    
    def _initial_authorization(self):
        """Первичная авторизация (автоматически выбирает метод: браузер или консоль)"""
        try:
            print("🌐 ПЕРВИЧНАЯ АВТОРИЗАЦИЯ")
            print("⚠️ Это нужно сделать ТОЛЬКО ОДИН РАЗ!")
            print("-" * 50)
            
            with open(self.client_config_file, 'r') as f:
                client_config = json.load(f)
            
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            
            # Пробуем сначала локальный сервер (для локальных машин)
            try:
                print("🔄 Попытка авторизации через локальный сервер...")
                self.credentials = flow.run_local_server(port=0)
                print("✅ Авторизация через локальный сервер успешна!")
                
            except Exception as browser_error:
                print(f"⚠️ Локальный сервер недоступен: {browser_error}")
                print("🔄 Переходим на консольную авторизацию...")
                
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
                auth_code = input("📝 Введите код авторизации: ").strip()
                
                if not auth_code:
                    print("❌ Код авторизации не был введен")
                    return False
                
                # Получаем токены по коду
                flow.fetch_token(code=auth_code)
                self.credentials = flow.credentials
                
                print("✅ Консольная авторизация успешна!")
            
            # Сохраняем токены для будущего использования
            with open(self.token_file, 'wb') as token:
                pickle.dump(self.credentials, token)
            
            print("✅ Первичная авторизация завершена")
            print("🎉 Теперь можно работать БЕЗ браузера!")
            
            # Создаем сервисы
            self.docs_service = build('docs', 'v1', credentials=self.credentials)
            self.drive_service = build('drive', 'v3', credentials=self.credentials)
            
            return True
            
        except Exception as e:
            print(f"❌ Ошибка первичной авторизации: {e}")
            return False
    
    def create_google_doc(self, title="Новый документ", content=""):
        """
        Создание Google Docs документа (автоматически)
        
        Args:
            title (str): Название документа
            content (str): Содержимое документа
            
        Returns:
            dict: Информация о созданном документе
        """
        if not self.is_ready and not self._authenticate():
            return {
                'success': False,
                'error': 'Аутентификация не пройдена',
                'method': 'automated_oauth'
            }
        
        try:
            print(f"\n📝 АВТОМАТИЧЕСКОЕ СОЗДАНИЕ ДОКУМЕНТА")
            print(f"📋 Название: '{title}'")
            
            # Создаем документ
            document = {'title': title}
            doc = self.docs_service.documents().create(body=document).execute()
            document_id = doc.get('documentId')
            
            print(f"✅ Документ создан: {document_id}")
            
            # Добавляем контент
            if content:
                self._add_content(document_id, content)
            
            # Настраиваем доступ
            self._make_document_public(document_id)
            
            document_url = f"https://docs.google.com/document/d/{document_id}/edit"
            
            result = {
                'document_id': document_id,
                'title': title,
                'url': document_url,
                'success': True,
                'created_at': datetime.now().isoformat(),
                'method': 'automated_oauth'
            }
            
            print(f"🎉 Документ '{title}' создан автоматически!")
            print(f"🔗 Ссылка: {document_url}")
            
            return result
            
        except Exception as e:
            print(f"❌ Ошибка создания документа: {e}")
            return {
                'success': False,
                'error': str(e),
                'method': 'automated_oauth'
            }
    
    def _add_content(self, document_id, content):
        """Добавление содержимого в документ"""
        try:
            requests = [
                {
                    'insertText': {
                        'location': {'index': 1},
                        'text': content
                    }
                }
            ]
            
            self.docs_service.documents().batchUpdate(
                documentId=document_id, body={'requests': requests}
            ).execute()
            
            print(f"✅ Контент добавлен")
            
        except Exception as e:
            print(f"⚠️ Ошибка добавления контента: {e}")
    
    def _make_document_public(self, document_id):
        """Настройка публичного доступа"""
        try:
            permission = {
                'type': 'anyone',
                'role': 'reader'
            }
            
            self.drive_service.permissions().create(
                fileId=document_id,
                body=permission
            ).execute()
            
            print(f"✅ Публичный доступ настроен")
            
        except Exception as e:
            print(f"⚠️ Ошибка настройки доступа: {e}")
    
    def get_status(self):
        """Получение статуса готовности"""
        return {
            'ready': self.is_ready,
            'has_config': os.path.exists(self.client_config_file),
            'has_tokens': os.path.exists(self.token_file),
            'automated': self.is_ready
        }


def create_google_docs_automated(title="Новый документ", content=""):
    """
    Функция-обертка для автоматического создания документов
    
    Args:
        title (str): Название документа
        content (str): Содержимое документа
        
    Returns:
        dict: Информация о созданном документе
    """
    creator = AutomatedOAuthDocsCreator()
    return creator.create_google_doc(title, content)


def main():
    """Демонстрация автоматизированного OAuth"""
    
    print("🚀 АВТОМАТИЗИРОВАННОЕ OAUTH РЕШЕНИЕ")
    print("🎯 Браузер нужен только ПЕРВЫЙ раз!")
    print("=" * 70)
    
    try:
        creator = AutomatedOAuthDocsCreator()
        status = creator.get_status()
        
        print(f"\n📊 СТАТУС ГОТОВНОСТИ:")
        print(f"   OAuth конфигурация: {'✅' if status['has_config'] else '❌'}")
        print(f"   Сохраненные токены: {'✅' if status['has_tokens'] else '❌'}")
        print(f"   Автоматическая работа: {'✅' if status['automated'] else '❌'}")
        
        if status['ready']:
            print(f"\n🎉 ПОЛНОСТЬЮ АВТОМАТИЧЕСКАЯ РАБОТА!")
            
            # Создаем документ автоматически
            result = creator.create_google_doc(
                title="Автоматический тест",
                content="Этот документ создан ПОЛНОСТЬЮ автоматически!\n\nНикаких браузеров не требовалось."
            )
            
            if result['success']:
                print(f"\n🎉 АВТОМАТИЧЕСКОЕ СОЗДАНИЕ УСПЕШНО!")
                print(f"📝 Документ: {result['title']}")
                print(f"🔗 Ссылка: {result['url']}")
                print(f"⚙️ Метод: {result['method']}")
        else:
            print(f"\n⚠️ Требуется первичная настройка:")
            if not status['has_config']:
                print(f"   1. Создать OAuth клиента в Google Cloud Console")
                print(f"   2. Скачать oauth_client.json")
            if not status['has_tokens']:
                print(f"   3. Выполнить первичную авторизацию через браузер")
            print(f"\nПосле этого будет работать БЕЗ браузера!")
            
    except Exception as e:
        print(f"❌ Общая ошибка: {e}")


if __name__ == "__main__":
    main() 