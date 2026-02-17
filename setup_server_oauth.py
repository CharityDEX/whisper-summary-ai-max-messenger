#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Утилита для настройки OAuth на серверах без GUI
Whisper AI - Google Docs Integration
"""

import os
import sys
import asyncio
from datetime import datetime

# Добавляем путь к проекту
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from services.google_docs_service import GoogleDocsService


async def setup_server_oauth():
    """Настройка OAuth для серверов"""
    
    print("🖥️ НАСТРОЙКА OAUTH ДЛЯ СЕРВЕРОВ")
    print("🎯 Whisper AI - Google Docs Integration")
    print("=" * 60)
    
    # Проверяем наличие конфигурационного файла
    oauth_client_file = "oauth_client.json"
    oauth_token_file = "oauth_token.pickle"
    
    if not os.path.exists(oauth_client_file):
        print(f"❌ Файл {oauth_client_file} не найден!")
        print("\n📋 ДЛЯ НАСТРОЙКИ:")
        print("1. Перейдите в Google Cloud Console")
        print("2. Создайте OAuth клиента (Desktop Application)")
        print("3. Скачайте конфигурацию как oauth_client.json")
        print("4. Поместите файл в корневую папку проекта")
        print("\n📚 Подробные инструкции: README_AUTOMATED_OAUTH_SETUP.md")
        return False
    
    print(f"✅ Найден файл конфигурации: {oauth_client_file}")
    
    # Проверяем существующие токены
    if os.path.exists(oauth_token_file):
        print(f"⚠️ Найдены существующие токены: {oauth_token_file}")
        
        while True:
            choice = input("🔄 Перезаписать токены? (y/n): ").strip().lower()
            if choice in ['y', 'yes', 'д', 'да']:
                os.remove(oauth_token_file)
                print("✅ Старые токены удалены")
                break
            elif choice in ['n', 'no', 'н', 'нет']:
                print("ℹ️ Используем существующие токены")
                break
            else:
                print("❌ Введите 'y' для да или 'n' для нет")
    
    # Создаем сервис и выполняем авторизацию
    print(f"\n🔐 ЗАПУСК ПРОЦЕССА АВТОРИЗАЦИИ...")
    print("💡 Система автоматически выберет подходящий метод")
    print("-" * 50)
    
    try:
        service = GoogleDocsService(
            oauth_client_file=oauth_client_file,
            oauth_token_file=oauth_token_file
        )
        
        if await service.authenticate():
            print("\n" + "=" * 60)
            print("🎉 АВТОРИЗАЦИЯ УСПЕШНА!")
            print("=" * 60)
            print(f"⚙️ Метод авторизации: {service.auth_method}")
            print(f"📁 Токены сохранены в: {oauth_token_file}")
            print("🚀 Система готова к автоматической работе!")
            
            # Создаем тестовый документ
            print(f"\n📝 СОЗДАНИЕ ТЕСТОВОГО ДОКУМЕНТА...")
            
            test_title = f"🧪 Тест серверной авторизации {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            test_url = await service.create_enhanced_google_doc(
                title=test_title,
                clean_transcript="Тест серверной авторизации успешно выполнен!",
                full_transcript="[00:00] Тест серверной авторизации успешно выполнен!"
            )
            
            if test_url:
                print(f"✅ Тестовый документ создан: {test_url}")
                print(f"\n🎯 ГОТОВО К ИСПОЛЬЗОВАНИЮ В КОДЕ:")
                print("from services.google_docs_service import create_enhanced_google_docs_automated")
                print("result = await create_enhanced_google_docs_automated(title, clean, full)")
            else:
                print("⚠️ Тестовый документ создать не удалось, но авторизация работает")
            
            return True
            
        else:
            print("\n❌ АВТОРИЗАЦИЯ НЕ УДАЛАСЬ")
            print("📚 См. README_AUTOMATED_OAUTH_SETUP.md для помощи")
            return False
            
    except Exception as e:
        print(f"\n❌ ОШИБКА: {e}")
        print("📧 Сообщите об ошибке разработчикам")
        return False


async def main():
    """Главная функция"""
    
    try:
        success = await setup_server_oauth()
        
        if success:
            print(f"\n🤖 Создано Whisper AI | https://whisper-summary.ru")
            print("✅ Настройка завершена успешно!")
        else:
            print("\n❌ Настройка не завершена")
            print("📚 Обратитесь к документации для помощи")
            
    except KeyboardInterrupt:
        print("\n\n⏹️ Настройка прервана пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")


if __name__ == "__main__":
    print("🚀 Запуск утилиты настройки OAuth...")
    asyncio.run(main()) 