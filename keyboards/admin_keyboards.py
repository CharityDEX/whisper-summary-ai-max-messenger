from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fluentogram import TranslatorRunner

def admin_menu(i18n: TranslatorRunner):
    source_statistic_button = InlineKeyboardButton(
        text=i18n.source_statistic_button(),
        callback_data='source_statistic'
    )
    unique_subs_sources_button = InlineKeyboardButton(
        text=i18n.source_with_subscription_button(),
        callback_data='statistic_data_period|subscriptions'
    )

    payments_sources_button = InlineKeyboardButton(
        text=i18n.payments_with_subscription_button(),
        callback_data='statistic_data_period|payments'
    )

    unique_payments_sources_button = InlineKeyboardButton(
        text=i18n.unique_payments_sources_button(),
        callback_data='statistic_data_period|unique_payments'
    )

    spam_button = InlineKeyboardButton(
        text=i18n.spam_menu_button(),
        callback_data='spam_menu'
    )

    give_subscription_button = InlineKeyboardButton(
        text=i18n.give_subscription_button(),
        callback_data='give_subscription'
    )

    data_export_button = InlineKeyboardButton(
        text=i18n.data_export_button(),
        callback_data='data_export_menu'
    )

    logs_button = InlineKeyboardButton(
        text=i18n.logs_button(),
        callback_data='logs_menu'
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [source_statistic_button],
            [unique_subs_sources_button],
            [payments_sources_button],
            [unique_payments_sources_button],
            [spam_button],
            [give_subscription_button],
            [data_export_button],
            [logs_button]
        ]
    )
    return keyboard


def sub_type_menu(i18n: TranslatorRunner, data_type: str):

    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data=f'statistics_menu'
    )
    weekly = InlineKeyboardButton(
        text=i18n.stats_weekly_button(),
        callback_data=f'statistic_data|{data_type}|weekly'
    )
    monthly = InlineKeyboardButton(
        text=i18n.stats_monthly_button(),
        callback_data=f'statistic_data|{data_type}|monthly'
    )
    all_subs = InlineKeyboardButton(
        text=i18n.stats_all_button(),
        callback_data=f'statistic_data|{data_type}|all'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [weekly, monthly],
            [all_subs],
            [back_button]
        ]
    )
    return keyboard


def statistic_source_menu(i18n: TranslatorRunner, data_type: str = 'subscriptions'):
    if data_type == 'sources':
        callback = 'statistics_menu'
    else:
        callback = f'statistic_data_period|{data_type}'

    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data=callback
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [back_button]
        ]
    )
    return keyboard


def statistic_source_menu_paginated(i18n: TranslatorRunner, data_type: str, page: int, total_pages: int, has_previous: bool, has_next: bool, **kwargs):
    """
    Создает клавиатуру для статистики источников с пагинацией.
    
    Args:
        i18n: Переводчик
        data_type: Тип данных ('sources', 'subscriptions', 'payments', 'unique_payments')
        page: Текущая страница
        total_pages: Общее количество страниц
        has_previous: Есть ли предыдущая страница
        has_next: Есть ли следующая страница
        **kwargs: Дополнительные параметры для callback данных
    """
    buttons = []
    
    # Маппинг полных имен параметров на сокращенные для экономии места
    param_mapping = {
        'subscription_type': 'st',
        'period_days': 'pd'
    }
    
    # Фильтруем и сжимаем kwargs - исключаем текстовые параметры и используем короткие имена
    essential_params = {}
    for key, value in kwargs.items():
        # Включаем только существенные параметры, исключаем текстовые
        if key not in ['period_text', 'subscription_text']:
            # Используем сокращенное имя параметра если есть
            short_key = param_mapping.get(key, key)
            essential_params[short_key] = value
    
    # Кнопки навигации
    nav_buttons = []
    
    if has_previous:
        prev_callback = f'source_page|{data_type}|{page-1}'
        # Добавляем дополнительные параметры в callback
        for key, value in essential_params.items():
            prev_callback += f'|{key}:{value}'
        
        nav_buttons.append(InlineKeyboardButton(
            text=i18n.previous_page_button(),
            callback_data=prev_callback
        ))
    
    # Информация о странице
    nav_buttons.append(InlineKeyboardButton(
        text=i18n.page_info(current_page=page, total_pages=total_pages),
        callback_data='page_info'  # Неактивная кнопка
    ))
    
    if has_next:
        next_callback = f'source_page|{data_type}|{page+1}'
        # Добавляем дополнительные параметры в callback
        for key, value in essential_params.items():
            next_callback += f'|{key}:{value}'
            
        nav_buttons.append(InlineKeyboardButton(
            text=i18n.next_page_button(),
            callback_data=next_callback
        ))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    # Кнопка "Назад"
    if data_type == 'sources':
        callback = 'statistics_menu'
    else:
        callback = f'statistic_data_period|{data_type}'
    
    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data=callback
    )
    buttons.append([back_button])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


def spam_menu(i18n: TranslatorRunner, skip: bool = False, show_exclude_button: bool = False):
    if skip:
        skip_button = InlineKeyboardButton(
            text=i18n.skip_button(),
            callback_data='skip_start_id'
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [skip_button]
            ]
        )
        return keyboard

    buttons = []
    
    if show_exclude_button:
        # Если уже выбрана группа пользователей, показываем кнопки для дальнейших действий
        exclude_ids_button = InlineKeyboardButton(
            text="🚫 Исключить ID",
            callback_data='exclude_ids'
        )
        continue_button = InlineKeyboardButton(
            text="📝 Написать сообщение",
            callback_data='continue_to_message'
        )
        buttons.extend([
            [exclude_ids_button],
            [continue_button]
        ])
    else:
        # Показываем кнопки выбора группы пользователей
        all_users_button = InlineKeyboardButton(
            text=i18n.spam_all_button(),
            callback_data='spam_all'
        )
        subscribed_users_button = InlineKeyboardButton(
            text=i18n.spam_subscribed_button(),
            callback_data='spam_subscribed'
        )
        unsubscribed_users_button = InlineKeyboardButton(
            text=i18n.spam_unsubscribed_button(),
            callback_data='spam_unsubscribed'
        )
        buttons.extend([
            [all_users_button],
            [subscribed_users_button],
            [unsubscribed_users_button]
        ])
    
    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data='statistics_menu'
    )
    buttons.append([back_button])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


def confirm_spam_keyboard(i18n: TranslatorRunner):
    confirm_button = InlineKeyboardButton(
        text=i18n.confirm_spam_button(),
        callback_data='confirm_spam'
    )
    cancel_button = InlineKeyboardButton(
        text=i18n.cancel_spam_button(),
        callback_data='statistics_menu'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [confirm_button], [cancel_button]
        ]
    )
    return keyboard

def cancel_subscription_keyboard(i18n: TranslatorRunner):
    cancel_button = InlineKeyboardButton(
        text=i18n.cancel_button(),
        callback_data='statistics_menu'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [cancel_button]
        ]
    )
    return keyboard


def confirm_give_subscription_keyboard(i18n: TranslatorRunner):
    """Клавиатура подтверждения перезаписи существующей подписки"""
    confirm_button = InlineKeyboardButton(
        text="✅ Подтвердить",
        callback_data='confirm_give_subscription'
    )
    cancel_button = InlineKeyboardButton(
        text="❌ Отмена",
        callback_data='cancel_give_subscription'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [confirm_button],
            [cancel_button]
        ]
    )
    return keyboard


def time_period_menu(i18n: TranslatorRunner, data_type: str, subscription_type: str):
    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data=f'statistic_data_period|{data_type}'
    )
    
    last_week = InlineKeyboardButton(
        text=i18n.last_week_button(),
        callback_data=f'statistic_data_time|{data_type}|{subscription_type}|7'
    )
    
    last_month = InlineKeyboardButton(
        text=i18n.last_month_button(),
        callback_data=f'statistic_data_time|{data_type}|{subscription_type}|30'
    )
    
    all_time = InlineKeyboardButton(
        text=i18n.all_time_button(),
        callback_data=f'statistic_data_time|{data_type}|{subscription_type}|0'
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [last_week, last_month],
            [all_time],
            [back_button]
        ]
    )
    return keyboard


def data_export_menu(i18n: TranslatorRunner):
    export_telegram_ids_button = InlineKeyboardButton(
        text=i18n.export_telegram_ids_button(),
        callback_data='export_telegram_ids'
    )
    
    export_sources_excel_button = InlineKeyboardButton(
        text=i18n.export_sources_excel_button(),
        callback_data='export_sources_excel'
    )
    
    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data='statistics_menu'
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [export_telegram_ids_button],
            [export_sources_excel_button],
            [back_button]
        ]
    )
    return keyboard

def logs_time_menu(i18n: TranslatorRunner):
    periods = [
        ("1 hour", i18n.logs_time_1h()),
        ("3 hours", i18n.logs_time_3h()),
        ("6 hours", i18n.logs_time_6h()),
        ("12 hours", i18n.logs_time_12h()),
        ("24 hours", i18n.logs_time_24h()),
        ("3 days", i18n.logs_time_3d()),
        ("7 days", i18n.logs_time_7d())
    ]
    
    buttons = []
    row = []
    for i, (period_val, period_text) in enumerate(periods):
        button = InlineKeyboardButton(
            text=period_text,
            callback_data=f'logs_download|{period_val}'
        )
        row.append(button)
        if len(row) == 2:
            buttons.append(row)
            row = []
    
    if row:
        buttons.append(row)
        
    back_button = InlineKeyboardButton(
        text=i18n.back_button(),
        callback_data='statistics_menu'
    )
    buttons.append([back_button])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard
