"""
Max messenger admin keyboard definitions.

Converted from aiogram InlineKeyboardMarkup/InlineKeyboardButton
to maxapi CallbackButton / _kb() pattern.
"""

from maxapi.types import CallbackButton
from maxapi.types.attachments.attachment import ButtonsPayload, Attachment
from fluentogram import TranslatorRunner


def _kb(*rows: list) -> Attachment:
    """Helper: build an inline keyboard Attachment from rows of buttons."""
    return ButtonsPayload(buttons=list(rows)).pack()


def admin_menu(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.source_statistic_button(), payload='source_statistic')],
        [CallbackButton(text=i18n.source_with_subscription_button(), payload='statistic_data_period|subscriptions')],
        [CallbackButton(text=i18n.payments_with_subscription_button(), payload='statistic_data_period|payments')],
        [CallbackButton(text=i18n.unique_payments_sources_button(), payload='statistic_data_period|unique_payments')],
        [CallbackButton(text=i18n.spam_menu_button(), payload='spam_menu')],
        [CallbackButton(text=i18n.give_subscription_button(), payload='give_subscription')],
        [CallbackButton(text=i18n.data_export_button(), payload='data_export_menu')],
        [CallbackButton(text=i18n.logs_button(), payload='logs_menu')],
    )


def sub_type_menu(i18n: TranslatorRunner, data_type: str) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.stats_weekly_button(), payload=f'statistic_data|{data_type}|weekly'),
         CallbackButton(text=i18n.stats_monthly_button(), payload=f'statistic_data|{data_type}|monthly')],
        [CallbackButton(text=i18n.stats_all_button(), payload=f'statistic_data|{data_type}|all')],
        [CallbackButton(text=i18n.back_button(), payload='statistics_menu')],
    )


def statistic_source_menu(i18n: TranslatorRunner, data_type: str = 'subscriptions') -> Attachment:
    callback = 'statistics_menu' if data_type == 'sources' else f'statistic_data_period|{data_type}'
    return _kb([CallbackButton(text=i18n.back_button(), payload=callback)])


def statistic_source_menu_paginated(i18n: TranslatorRunner, data_type: str, page: int, total_pages: int,
                                     has_previous: bool, has_next: bool, **kwargs) -> Attachment:
    param_mapping = {'subscription_type': 'st', 'period_days': 'pd'}

    essential_params = {}
    for key, value in kwargs.items():
        if key not in ['period_text', 'subscription_text']:
            short_key = param_mapping.get(key, key)
            essential_params[short_key] = value

    nav_buttons = []
    if has_previous:
        prev_cb = f'source_page|{data_type}|{page - 1}'
        for key, value in essential_params.items():
            prev_cb += f'|{key}:{value}'
        nav_buttons.append(CallbackButton(text=i18n.previous_page_button(), payload=prev_cb))

    nav_buttons.append(CallbackButton(
        text=i18n.page_info(current_page=page, total_pages=total_pages),
        payload='page_info',
    ))

    if has_next:
        next_cb = f'source_page|{data_type}|{page + 1}'
        for key, value in essential_params.items():
            next_cb += f'|{key}:{value}'
        nav_buttons.append(CallbackButton(text=i18n.next_page_button(), payload=next_cb))

    rows = []
    if nav_buttons:
        rows.append(nav_buttons)

    callback = 'statistics_menu' if data_type == 'sources' else f'statistic_data_period|{data_type}'
    rows.append([CallbackButton(text=i18n.back_button(), payload=callback)])
    return _kb(*rows)


def spam_menu(i18n: TranslatorRunner, skip: bool = False, show_exclude_button: bool = False) -> Attachment:
    if skip:
        return _kb([CallbackButton(text=i18n.skip_button(), payload='skip_start_id')])

    rows = []
    if show_exclude_button:
        rows.append([CallbackButton(text="🚫 Исключить ID", payload='exclude_ids')])
        rows.append([CallbackButton(text="📝 Написать сообщение", payload='continue_to_message')])
    else:
        rows.append([CallbackButton(text=i18n.spam_all_button(), payload='spam_all')])
        rows.append([CallbackButton(text=i18n.spam_subscribed_button(), payload='spam_subscribed')])
        rows.append([CallbackButton(text=i18n.spam_unsubscribed_button(), payload='spam_unsubscribed')])
    rows.append([CallbackButton(text=i18n.back_button(), payload='statistics_menu')])
    return _kb(*rows)


def confirm_spam_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.confirm_spam_button(), payload='confirm_spam')],
        [CallbackButton(text=i18n.cancel_spam_button(), payload='statistics_menu')],
    )


def cancel_subscription_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.cancel_button(), payload='statistics_menu')])


def confirm_give_subscription_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text="✅ Подтвердить", payload='confirm_give_subscription')],
        [CallbackButton(text="❌ Отмена", payload='cancel_give_subscription')],
    )


def time_period_menu(i18n: TranslatorRunner, data_type: str, subscription_type: str) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.last_week_button(), payload=f'statistic_data_time|{data_type}|{subscription_type}|7'),
         CallbackButton(text=i18n.last_month_button(), payload=f'statistic_data_time|{data_type}|{subscription_type}|30')],
        [CallbackButton(text=i18n.all_time_button(), payload=f'statistic_data_time|{data_type}|{subscription_type}|0')],
        [CallbackButton(text=i18n.back_button(), payload=f'statistic_data_period|{data_type}')],
    )


def data_export_menu(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.export_telegram_ids_button(), payload='export_telegram_ids')],
        [CallbackButton(text=i18n.export_sources_excel_button(), payload='export_sources_excel')],
        [CallbackButton(text=i18n.back_button(), payload='statistics_menu')],
    )


def logs_time_menu(i18n: TranslatorRunner) -> Attachment:
    periods = [
        ("1 hour", i18n.logs_time_1h()),
        ("3 hours", i18n.logs_time_3h()),
        ("6 hours", i18n.logs_time_6h()),
        ("12 hours", i18n.logs_time_12h()),
        ("24 hours", i18n.logs_time_24h()),
        ("3 days", i18n.logs_time_3d()),
        ("7 days", i18n.logs_time_7d()),
    ]
    rows = []
    row = []
    for period_val, period_text in periods:
        row.append(CallbackButton(text=period_text, payload=f'logs_download|{period_val}'))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([CallbackButton(text=i18n.back_button(), payload='statistics_menu')])
    return _kb(*rows)
