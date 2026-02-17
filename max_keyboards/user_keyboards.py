"""
Max messenger keyboard definitions.

In Max, all keyboards are inline (CallbackButton / LinkButton).
There is NO ReplyKeyboardMarkup equivalent — persistent bottom keyboards
are replaced with inline keyboards sent as messages.

Keyboards are returned as Attachment objects (type=INLINE_KEYBOARD)
and passed via the `attachments` parameter of send_message / message.answer.
"""

from maxapi.types import CallbackButton, LinkButton
from maxapi.types.attachments.attachment import ButtonsPayload, Attachment
from maxapi.enums.attachment import AttachmentType
from fluentogram import TranslatorRunner

from services.init_max_bot import config
from services.payments.services import determine_subscription_type_from_cp_data, get_cloudpayments_subscription_details_by_sub_id
from services.payments.stripe_tools import get_stripe_subscription_details


def _kb(*rows: list) -> Attachment:
    """Helper: build an inline keyboard Attachment from rows of buttons."""
    return ButtonsPayload(buttons=list(rows)).pack()


def inline_cancel(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.cancel_button(), payload='cancel')])


def inline_main_menu(i18n: TranslatorRunner, notetaker_link: bool = False) -> Attachment:
    subscription_button = CallbackButton(text=i18n.subscription_button(), payload='subscription_menu')
    referral_button = CallbackButton(text=i18n.referral_program_button(), payload='referral_program')
    notetaker_button = CallbackButton(text=i18n.notetaker_button(), payload='notetaker_menu')
    settings = CallbackButton(text=i18n.settings_button(), payload='settings')
    support = CallbackButton(text=i18n.support_button(), payload='support')

    rows = [[subscription_button], [referral_button]]
    if notetaker_link:
        rows.append([notetaker_button])
    rows.append([settings, support])
    return _kb(*rows)


def inline_new_session(i18n: TranslatorRunner, is_summary: bool = False,
                       download_button: bool = False, chat_session: str = None,
                       session_id: str = None) -> Attachment:
    if is_summary:
        ask_questions_payload = f'ask_questions:{chat_session}' if chat_session else 'ask_questions'
        button2 = CallbackButton(text=i18n.you_can_ask_2_button(), payload=ask_questions_payload)
        rows = [[button2]]
        if download_button and session_id:
            button3 = CallbackButton(text=i18n.get_video_button(), payload=f'get_video:{session_id}')
            rows.append([button3])
    else:
        rows = []
    return _kb(*rows) if rows else _kb()


def payment_methods_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.cloud_payment_button(), payload='payment_method|cloudpayments')],
        [CallbackButton(text=i18n.stripe_payment_button(), payload='payment_method|stripe')],
        [CallbackButton(text=i18n.back_button(), payload='subscription_offer')],
    )


def inline_cancel_queue(i18n: TranslatorRunner, message_id: int) -> Attachment:
    return _kb([CallbackButton(text=i18n.cancel_button(), payload=f'cancel_queue|{message_id}')])


def captcha_keyboard(i18n: TranslatorRunner, payment_method: str, subscription_type: str) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.refresh_captcha_button(), payload=f'refresh_captcha|{payment_method}|{subscription_type}')],
        [CallbackButton(text=i18n.cancel_button(), payload='payment_methods')],
    )


def subscription_type_keyboard(i18n: TranslatorRunner, payment_method: str) -> Attachment:
    pricing_labels = {
        'stripe': {
            'monthly': i18n.buy_monthly_subscription_stripe_button(price=config.stripe.price.monthly),
            'semiannual': i18n.buy_semiannual_subscription_stripe_button(price=config.stripe.price.semiannual),
            'weekly': i18n.buy_weekly_subscription_stripe_button(price=config.stripe.price.weekly),
        },
        'cloudpayments': {
            'monthly': i18n.buy_monthly_subscription_cloudpayments_button(price=config.cloudpayments.price.monthly),
            'semiannual': i18n.buy_semiannual_subscription_cloudpayments_button(price=config.cloudpayments.price.semiannual),
            'weekly': i18n.buy_weekly_subscription_cloudpayments_button(price=config.cloudpayments.price.weekly),
        },
    }

    return _kb(
        [CallbackButton(text=pricing_labels[payment_method]['monthly'], payload=f'create_bill|{payment_method}|monthly')],
        [CallbackButton(text=pricing_labels[payment_method]['weekly'], payload=f'create_bill|{payment_method}|weekly')],
        [CallbackButton(text=pricing_labels[payment_method]['semiannual'], payload=f'create_bill|{payment_method}|semiannual')],
        [CallbackButton(text=i18n.back_button(), payload='payment_methods')],
    )


async def inline_subscription_menu(i18n: TranslatorRunner, user: dict = None) -> Attachment:
    if user['subscription'] == 'False':
        return _kb(
            [CallbackButton(text=i18n.buy_weekly_subscription_button(), payload='choose_subscription|weekly')],
            [CallbackButton(text=i18n.buy_monthly_subscription_button(), payload='choose_subscription|monthly')],
        )
    elif user['subscription'] == 'PastDue':
        return _kb([CallbackButton(text=i18n.cancel_subscription_button(), payload='cancel_subscription')])
    else:
        cancel_btn = CallbackButton(text=i18n.cancel_subscription_button(), payload='cancel_subscription')
        if user['subscription_autopay']:
            rows = [[cancel_btn]]
            if user['subscription_id'].startswith('sc_'):
                sub_data = await get_cloudpayments_subscription_details_by_sub_id(
                    account_id=user['telegram_id'],
                    subscription_id_to_find=user['subscription_id'],
                    cp_public_id=config.payment.public_id,
                    cp_api_secret=config.payment.api_secret,
                )
                sub_type = determine_subscription_type_from_cp_data(sub_data)
                if sub_type == 'weekly':
                    rows.append([CallbackButton(
                        text=i18n.upgrade_subscription_button_to_monthly(price=f'{config.cloudpayments.price.monthly}₽'),
                        payload='upgrade_subscription|to_monthly',
                    )])
                    rows.append([CallbackButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.cloudpayments.price.semiannual}₽'),
                        payload='upgrade_subscription|to_semiannual',
                    )])
                elif sub_type == 'monthly':
                    rows.append([CallbackButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.cloudpayments.price.semiannual}₽'),
                        payload='upgrade_subscription|to_semiannual',
                    )])
            else:
                sub_data = await get_stripe_subscription_details(user['subscription_id'])
                if (sub_data and sub_data.get('items') and len(sub_data['items']) > 0
                        and sub_data['items'][0]['price']['id'] in config.stripe.weekly_price_id):
                    rows.append([CallbackButton(
                        text=i18n.upgrade_subscription_button_to_monthly(price=f'{config.stripe.price.monthly}$'),
                        payload='upgrade_subscription|to_monthly',
                    )])
                    rows.append([CallbackButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.stripe.price.semiannual}$'),
                        payload='upgrade_subscription|to_semiannual',
                    )])
                elif (sub_data and sub_data.get('items') and len(sub_data['items']) > 0
                      and sub_data['items'][0]['price']['id'] in config.stripe.monthly_price_id):
                    rows.append([CallbackButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.stripe.price.semiannual}$'),
                        payload='upgrade_subscription|to_semiannual',
                    )])
            return _kb(*rows)
        else:
            return _kb([CallbackButton(text=i18n.renew_subscription_button(), payload='renew_subscription')])


def bill_keyboard(i18n: TranslatorRunner, bill_url: str, oferta_confirm: bool = False,
                  payment_method: str = None, subscription_type: str = None) -> Attachment:
    back_button = CallbackButton(text=i18n.back_button(), payload=f'payment_method|{payment_method}')
    if payment_method == 'cloudpayments':
        payment_button = LinkButton(text=i18n.pay_button(), url=bill_url) if oferta_confirm \
            else CallbackButton(text=i18n.pay_button(), payload=f'url|{bill_url}')
        return _kb(
            [CallbackButton(text=i18n.oferta_button(oferta_status='✅' if oferta_confirm else '❌'),
                            payload=f'oferta_status|{oferta_confirm}')],
            [payment_button],
            [back_button],
        )
    else:
        return _kb(
            [LinkButton(text=i18n.pay_button(), url=bill_url)],
            [back_button],
        )


def sure_cancel_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.sure_cancel_subscription_button(), payload='sure_cancel')])


def subscription_menu(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.buy_subscription_button(), payload='payment_methods')])


def subscription_forward(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.buy_subscription_button(), payload='payment_methods')])


def inline_user_settings(i18n: TranslatorRunner, user: dict) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.change_language_button(), payload='change_language')],
        [CallbackButton(text=i18n.transcription_format_button(), payload='setting_menu|transcription_format')],
        [CallbackButton(text=i18n.change_llm_model_button(), payload='setting_menu|llm_model')],
        [CallbackButton(text=i18n.specify_language_button(), payload='setting_menu|specify_audio_language')],
        [CallbackButton(text=i18n.back_to_menu_button(), payload='main_menu')],
    )


def inline_change_model_menu(i18n: TranslatorRunner, user: dict, new_user: bool = False) -> Attachment:
    models = {
        'gpt-4o': i18n.gpt4_model_button(),
        'claude-3-5-sonnet': i18n.claude_model_button(),
    }
    rows = []
    if not new_user:
        for model, label in models.items():
            text = i18n.model_selected_prefix(name=label) if model == user['llm_model'] else label
            rows.append([CallbackButton(text=text, payload=f'change_setting|{model}|llm_model')])
        rows.append([CallbackButton(text=i18n.back_button(), payload='setting_menu')])
    else:
        for model, label in models.items():
            rows.append([CallbackButton(text=label, payload=f'new_user_model|{model}')])
    return _kb(*rows)


def inline_change_specify_language_menu(i18n: TranslatorRunner, user: dict) -> Attachment:
    if not user.get('specify_audio_language', False):
        option_inactive = CallbackButton(text=f'{i18n.specify_language_inactive_button()} ✅',
                                         payload='change_setting|active|specify_audio_language')
        option_active = CallbackButton(text=i18n.specify_language_active_button(),
                                       payload='change_setting|active|specify_audio_language')
    else:
        option_active = CallbackButton(text=f'{i18n.specify_language_active_button()} ✅',
                                       payload='change_setting|active|specify_audio_language')
        option_inactive = CallbackButton(text=i18n.specify_language_inactive_button(),
                                         payload='change_setting|inactive|specify_audio_language')
    return _kb([option_active], [option_inactive], [CallbackButton(text=i18n.back_button(), payload='setting_menu')])


def inline_change_transcription_format_menu(i18n: TranslatorRunner, user: dict) -> Attachment:
    formats = {
        'google_docs': i18n.format_google_docs_button(),
        'docx': i18n.format_docx_button(),
        'pdf': i18n.format_pdf_button(),
        'txt': i18n.format_txt_button(),
        'md': i18n.format_md_button(),
    }
    rows = []
    for fmt_key, label in formats.items():
        text = i18n.format_selected_prefix(name=label) if fmt_key == user.get('transcription_format', 'txt') else label
        rows.append([CallbackButton(text=text, payload=f'change_setting|{fmt_key}|transcription_format')])
    rows.append([CallbackButton(text=i18n.back_button(), payload='setting_menu')])
    return _kb(*rows)


def new_user_change_specify_language_menu(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.yes_specify_lang_button(), payload='new_user_lang|True')],
        [CallbackButton(text=i18n.no_specify_lang_button(), payload='new_user_lang|False')],
    )


def continue_without_language(i18n: TranslatorRunner, audio_key: str = None) -> Attachment:
    payload = f"cont_lang_{audio_key}" if audio_key is not None else "continue_without_language"
    return _kb([CallbackButton(text=i18n.continue_without_language_button(), payload=payload)])


def faq_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [LinkButton(text=i18n.full_instruction_button(), url=i18n.full_instructions_link())],
        [LinkButton(text=i18n.faq_button(), url=i18n.faq_link())],
        [LinkButton(text=i18n.support_button(), url='https://t.me/WAI_support')],
        [LinkButton(text=i18n.contact_form_button(), url=i18n.contact_form_url())],
        [CallbackButton(text=i18n.back_to_menu_button(), payload='main_menu')],
    )


def inline_change_language_menu(i18n: TranslatorRunner, user: dict) -> Attachment:
    languages = {'ru': i18n.ru_lang_button(), 'en': i18n.en_lang_button()}
    rows = []
    for lang_code, label in languages.items():
        text = f'{label} ✅' if user['user_language'] == lang_code else label
        rows.append([CallbackButton(text=text, payload=f'change_language|{lang_code}')])
    rows.append([CallbackButton(text=i18n.back_button(), payload='setting_menu')])
    return _kb(*rows)


def inline_download_file(i18n: TranslatorRunner, session_id: str = None) -> Attachment:
    payload = f'download_file:{session_id}' if session_id else 'download_file'
    return _kb([CallbackButton(text=i18n.download_file_button(), payload=payload)])


def sure_upgrade_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.sure_upgrade_subscription_button(), payload='sure_upgrade_subscription')])


def referral_program_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb(
        [CallbackButton(text=i18n.send_invitation_button(), payload='referral_send_invitation')],
        [CallbackButton(text=i18n.back_to_menu_button(), payload='main_menu')],
    )


def referral_invitation_keyboard(i18n: TranslatorRunner, referral_link: str) -> Attachment:
    return _kb([LinkButton(text=i18n.try_whisper_ai_button(), url=referral_link)])


def transcription_no_summary_keyboard(i18n: TranslatorRunner, session_id: str = None,
                                       get_file_button: bool = True,
                                       show_video_button: bool = False) -> Attachment:
    get_summary_btn = CallbackButton(text=i18n.get_summary_button(), payload=f'get_summary|{session_id}')
    ask_questions_payload = f'ask_questions|{session_id}' if session_id else 'ask_questions'
    ask_questions_btn = CallbackButton(text=i18n.you_can_ask_2_button(), payload=ask_questions_payload)

    rows = []
    if get_file_button:
        rows.append([CallbackButton(text=i18n.get_full_transcription_button(), payload=f'get_full_transcription|{session_id}')])
    rows.append([get_summary_btn])
    rows.append([ask_questions_btn])
    if show_video_button:
        rows.append([CallbackButton(text=i18n.get_video_button(), payload=f'get_video:{session_id}')])
    return _kb(*rows)


def initial_payment_notification_keyboard(i18n: TranslatorRunner) -> Attachment:
    return _kb([CallbackButton(text=i18n.buy_subscription_reminder_button(), payload='payment_methods')])


def second_payment_notification_keyboard(i18n: TranslatorRunner, bill_url: str,
                                          user_data: dict = None,
                                          payment_method: str = 'cloudpayments') -> Attachment:
    return _kb([LinkButton(text=i18n.pay_now_button(), url=bill_url)])


def notetaker_menu_keyboard(i18n: TranslatorRunner) -> Attachment:
    # TODO: Replace Telegram bot link with Max equivalent when available
    return _kb(
        [LinkButton(text=i18n.open_note_taker(), url='https://t.me/notetaker_ai_bot?start=whisperbutton')],
    )
