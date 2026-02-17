from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from fluentogram import TranslatorRunner

from services.init_bot import config
from services.payments.services import determine_subscription_type_from_cp_data, get_cloudpayments_subscription_details_by_sub_id
from services.payments.stripe_tools import get_stripe_subscription_details


def inline_cancel(i18n: TranslatorRunner):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.cancel_button(), callback_data='cancel')]
        ]
    )
    return keyboard

def back_to_menu_keyboard(i18n: TranslatorRunner) -> ReplyKeyboardMarkup:
    keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=i18n.back_to_menu_button())]], resize_keyboard=True)
    return keyboard

def inline_main_menu(i18n: TranslatorRunner, notetaker_link: bool = False):
    button1 = InlineKeyboardButton(
        text=i18n.new_session_button(),
        callback_data='new_audio'
    )
    subscription_button = InlineKeyboardButton(
        text=i18n.subscription_button(),
        callback_data='subscription_menu')

    support = InlineKeyboardButton(
        text=i18n.support_button(),
        callback_data='support'
    )

    settings = InlineKeyboardButton(
        text=i18n.settings_button(),
        callback_data='settings'
    )

    referral_button = InlineKeyboardButton(
        text=i18n.referral_program_button(),
        callback_data='referral_program'
    )
    notetaker_button = InlineKeyboardButton(
        text=i18n.notetaker_button(),
        callback_data='notetaker_menu'
    )
    if notetaker_link:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[subscription_button], [referral_button], [notetaker_button], [settings, support]]
        )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[subscription_button], [referral_button], [settings, support]]
        )
    return keyboard

def main_menu_keyboard(i18n: TranslatorRunner) -> ReplyKeyboardMarkup:
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=i18n.new_session_button())],
            [KeyboardButton(text=i18n.subscription_button())],
            [KeyboardButton(text=i18n.restart_bot_button())],
            [KeyboardButton(text=i18n.support_button()), KeyboardButton(text=i18n.faq_button())],
            [KeyboardButton(text=i18n.settings_button())]
        ],
        resize_keyboard=True
    )
    return keyboard

def notetaker_menu_keyboard(i18n: TranslatorRunner) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=i18n.open_note_taker(), url='https://t.me/notetaker_ai_bot?start=whisperbutton')],]
    )
    return keyboard

def inline_new_session(i18n: TranslatorRunner, is_summary: bool = False, download_button: bool = False, chat_session: str = None, session_id: str = None):
    button1 = InlineKeyboardButton(
        text=i18n.new_session_button(),
        callback_data='new_audio'
    )

    if is_summary:
        # Если есть chat_session, включаем его в callback_data
        ask_questions_callback = f'ask_questions:{chat_session}' if chat_session else 'ask_questions'
        button2 = InlineKeyboardButton(
            text=i18n.you_can_ask_2_button(),
            callback_data=ask_questions_callback)
        if download_button and session_id:
            button3 = InlineKeyboardButton(
                text=i18n.get_video_button(),
                callback_data=f'get_video:{session_id}'
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[button2],
                                 [button3]]
            )
        else:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[button2]]
            )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[]
        )

    return keyboard

def payment_methods_keyboard(i18n: TranslatorRunner):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n.cloud_payment_button(),
                    callback_data='payment_method|cloudpayments'
                )
            ],
            [
                InlineKeyboardButton(
                    text=i18n.stripe_payment_button(),
                    callback_data='payment_method|stripe'
                )
            ],
            [
                InlineKeyboardButton(
                    text=i18n.back_button(),
                    callback_data='subscription_offer')
            ]
        ]
    )
    return keyboard


def inline_cancel_queue(i18n: TranslatorRunner, message_id: int):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.cancel_button(), callback_data=f'cancel_queue|{message_id}')]
        ]
    )
    return keyboard


def captcha_keyboard(i18n: TranslatorRunner, payment_method: str, subscription_type: str):
    """Keyboard for CAPTCHA verification with cancel button and refresh CAPTCHA button"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n.refresh_captcha_button(),
                    callback_data=f'refresh_captcha|{payment_method}|{subscription_type}'
                )
            ],
            [
                InlineKeyboardButton(
                    text=i18n.cancel_button(),
                    callback_data='payment_methods'
                )
            ]
        ]
    )
    return keyboard

def subscription_type_keyboard(i18n: TranslatorRunner, payment_method: str, ):
    pricing_labels = {
        'stripe': {
            'monthly': i18n.buy_monthly_subscription_stripe_button(price=config.stripe.price.monthly),
            'semiannual': i18n.buy_semiannual_subscription_stripe_button(price=config.stripe.price.semiannual),
            'weekly': i18n.buy_weekly_subscription_stripe_button(price=config.stripe.price.weekly)
        },
        'cloudpayments': {
            'monthly': i18n.buy_monthly_subscription_cloudpayments_button(price=config.cloudpayments.price.monthly),
            'semiannual': i18n.buy_semiannual_subscription_cloudpayments_button(price=config.cloudpayments.price.semiannual),
            'weekly': i18n.buy_weekly_subscription_cloudpayments_button(price=config.cloudpayments.price.weekly)
        }
    }

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=pricing_labels[payment_method]['monthly'],
                    callback_data=f'create_bill|{payment_method}|monthly'
                )
            ],
            [
                InlineKeyboardButton(
                    text=pricing_labels[payment_method]['weekly'],
                    callback_data=f'create_bill|{payment_method}|weekly'
                )
            ],
            [
                InlineKeyboardButton(
                    text=pricing_labels[payment_method]['semiannual'],
                    callback_data=f'create_bill|{payment_method}|semiannual'
                )
            ],
            [InlineKeyboardButton(text=i18n.back_button(), callback_data='payment_methods')]
        ]
    )
    return keyboard

async def inline_subscription_menu(i18n: TranslatorRunner, user: dict = None):
    #По идее, это условие никогда не срабатывает и его можно убрать. TODO
    if user['subscription'] == 'False':
        monthly_subscription = InlineKeyboardButton(
            text=i18n.buy_monthly_subscription_button(),
            callback_data='choose_subscription|monthly'
        )
        weekly_subscription = InlineKeyboardButton(
            text=i18n.buy_weekly_subscription_button(),
            callback_data='choose_subscription|weekly'
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [weekly_subscription],
                [monthly_subscription]
            ]
        )
    elif user['subscription'] == 'PastDue':
        cancel_subscription = InlineKeyboardButton(
            text=i18n.cancel_subscription_button(),
            callback_data='cancel_subscription'
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[cancel_subscription]]
        )
    else:
        if user['subscription_autopay']:
            subscription_action = InlineKeyboardButton(
                text=i18n.cancel_subscription_button(),
                callback_data='cancel_subscription'
            )
            if user['subscription_id'].startswith('sc_'):
                sub_data = await get_cloudpayments_subscription_details_by_sub_id(
                    account_id=user['telegram_id'],
                    subscription_id_to_find=user['subscription_id'],
                    cp_public_id=config.payment.public_id,
                    cp_api_secret=config.payment.api_secret
                )
                sub_type = determine_subscription_type_from_cp_data(sub_data)
                if sub_type == 'weekly':
                    upgrade_subscription = InlineKeyboardButton(
                        text=i18n.upgrade_subscription_button_to_monthly(price=f'{config.cloudpayments.price.monthly}₽'),
                        callback_data='upgrade_subscription|to_monthly'
                    )
                    upgrade_subscription_semiannual = InlineKeyboardButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.cloudpayments.price.semiannual}₽'),
                        callback_data='upgrade_subscription|to_semiannual'
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[subscription_action], [upgrade_subscription], [upgrade_subscription_semiannual]]
                    )
                elif sub_type == 'monthly':
                    upgrade_subscription = InlineKeyboardButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.cloudpayments.price.semiannual}₽'),
                        callback_data='upgrade_subscription|to_semiannual'
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[subscription_action], [upgrade_subscription]]
                    )
                else:
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[subscription_action]]
                    )
            else:
                sub_data = await get_stripe_subscription_details(user['subscription_id'])
                if (sub_data and
                    sub_data.get('items') and
                    len(sub_data['items']) > 0 and
                    sub_data['items'][0]['price']['id'] in config.stripe.weekly_price_id):
                    upgrade_subscription = InlineKeyboardButton(
                        text=i18n.upgrade_subscription_button_to_monthly(price=f'{config.stripe.price.monthly}$'),
                        callback_data='upgrade_subscription|to_monthly'
                    )
                    upgrade_subscription_semiannual = InlineKeyboardButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.stripe.price.semiannual}$'),
                        callback_data='upgrade_subscription|to_semiannual'
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[subscription_action], [upgrade_subscription], [upgrade_subscription_semiannual]]
                    )
                elif (sub_data and
                    sub_data.get('items') and
                    len(sub_data['items']) > 0 and
                    sub_data['items'][0]['price']['id'] in config.stripe.monthly_price_id):
                    upgrade_subscription = InlineKeyboardButton(
                        text=i18n.upgrade_subscription_button_to_semiannual(price=f'{config.stripe.price.semiannual}$'),
                        callback_data='upgrade_subscription|to_semiannual'
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[subscription_action], [upgrade_subscription]]
                    )
                else:
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[subscription_action]]
                    )
        else:
            renew_subscription = InlineKeyboardButton(
                text=i18n.renew_subscription_button(),
                callback_data='renew_subscription'
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[renew_subscription]]
            )
    return keyboard


def bill_keyboard(i18n: TranslatorRunner, bill_url: str, oferta_confirm: bool = False, payment_method: str = None, subscription_type: str = None):

    back_button = InlineKeyboardButton(text=i18n.back_button(), callback_data=f'payment_method|{payment_method}')
    if payment_method == 'cloudpayments':
        payment_button = InlineKeyboardButton(text=i18n.pay_button(), url=bill_url) if oferta_confirm else InlineKeyboardButton(text=i18n.pay_button(), callback_data=f'url|{bill_url}')
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=i18n.oferta_button(oferta_status='✅' if oferta_confirm else '❌'), callback_data=f'oferta_status|{oferta_confirm}')],
                [payment_button],
                [back_button]]
        )
    else:
        payment_button = InlineKeyboardButton(text=i18n.pay_button(), url=bill_url)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[payment_button],
                             [back_button]]
        )

    return keyboard


def sure_cancel_keyboard(i18n: TranslatorRunner):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.sure_cancel_subscription_button(), callback_data='sure_cancel')]
        ]
    )
    return keyboard


def subscription_menu(i18n: TranslatorRunner):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.buy_subscription_button(), callback_data='payment_methods')],
        ]
    )
    return keyboard

def subscription_forward(i18n: TranslatorRunner):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.buy_subscription_button(), callback_data='payment_methods')],
        ]
    )
    return keyboard


def inline_user_settings(i18n: TranslatorRunner, user: dict):
    change_model = InlineKeyboardButton(
        text=i18n.change_llm_model_button(),
        callback_data='setting_menu|llm_model'
    )
    specify_language = InlineKeyboardButton(
        text=i18n.specify_language_button(),
        callback_data='setting_menu|specify_audio_language'
    )
    transcription_format = InlineKeyboardButton(
        text=i18n.transcription_format_button(),
        callback_data='setting_menu|transcription_format'
    )
    # download_video = InlineKeyboardButton(
    #     text=i18n.download_video_button(status='✅' if user.get('download_video', False) else '❌'),
    #     callback_data='setting_menu|download_video'
    # )
    change_language = InlineKeyboardButton(
        text=i18n.change_language_button(),
        callback_data='change_language'
    )

    subscription_button = InlineKeyboardButton(
        text=i18n.subscription_button(),
        callback_data='subscription_menu')

    menu_button = InlineKeyboardButton(
        text=i18n.back_to_menu_button(),
        callback_data='main_menu'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[change_language], [transcription_format], [change_model], [specify_language], [menu_button]]
    )
    return keyboard

def inline_change_model_menu(i18n: TranslatorRunner, user: dict, new_user: bool = False):
    models = {
        'gpt-4o': i18n.gpt4_model_button(),
        'claude-3-5-sonnet': i18n.claude_model_button()
    }
    buttons = []
    if not new_user:
        for model in models:
            if model == user['llm_model']:
                buttons.append([InlineKeyboardButton(
                    text=i18n.model_selected_prefix(name=models[model]),
                    callback_data=f'change_setting|{model}|llm_model'
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=models[model],
                    callback_data=f'change_setting|{model}|llm_model'
                )])
        buttons.append([InlineKeyboardButton(text=i18n.back_button(), callback_data='setting_menu')])
    else:
        for model in models:
            buttons.append([InlineKeyboardButton(
                text=models[model],
                callback_data=f'new_user_model|{model}'
            )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard

def inline_change_specify_language_menu(i18n: TranslatorRunner, user: dict):
    titles = {True: '✅', False: '❌'}

    if not user.get('specify_audio_language', False):
        option_inactive = InlineKeyboardButton(text=f'{i18n.specify_language_inactive_button()} ✅',
                                      callback_data=f'change_setting|active|specify_audio_language')
        option_active = InlineKeyboardButton(text=i18n.specify_language_active_button(),
                                             callback_data=f'change_setting|active|specify_audio_language')
    else:
        option_active = InlineKeyboardButton(text=f'{i18n.specify_language_active_button()} ✅',
                                             callback_data=f'change_setting|active|specify_audio_language')
        option_inactive = InlineKeyboardButton(text=i18n.specify_language_inactive_button(),
                                      callback_data=f'change_setting|inactive|specify_audio_language')


    back_button = InlineKeyboardButton(text=i18n.back_button(),
                                       callback_data='setting_menu')
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[option_active], [option_inactive], [back_button]])
    return keyboard

def inline_change_transcription_format_menu(i18n: TranslatorRunner, user: dict):
    formats = {
        'google_docs': i18n.format_google_docs_button(),
        'docx': i18n.format_docx_button(),
        'pdf': i18n.format_pdf_button(),
        'txt': i18n.format_txt_button(),
        'md': i18n.format_md_button(),


    }
    buttons = []
    
    for format_key in formats:
        if format_key == user.get('transcription_format', 'txt'):
            buttons.append([InlineKeyboardButton(
                text=i18n.format_selected_prefix(name=formats[format_key]),
                callback_data=f'change_setting|{format_key}|transcription_format'
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text=formats[format_key],
                callback_data=f'change_setting|{format_key}|transcription_format'
            )])
    
    buttons.append([InlineKeyboardButton(text=i18n.back_button(), callback_data='setting_menu')])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard

def new_user_change_specify_language_menu(i18n: TranslatorRunner):
    yes_button = InlineKeyboardButton(text=i18n.yes_specify_lang_button(),
                                      callback_data='new_user_lang|True')
    no_button = InlineKeyboardButton(text=i18n.no_specify_lang_button(),
                                     callback_data='new_user_lang|False')
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[yes_button], [no_button]])
    return keyboard

def continue_without_language(i18n: TranslatorRunner, audio_key: str = None):
    callback_data = f"cont_lang_{audio_key}" if audio_key is not None else continue_without_language
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.continue_without_language_button(), callback_data=callback_data)]
        ]
    )
    return keyboard


def faq_keyboard(i18n: TranslatorRunner) -> InlineKeyboardMarkup:

    full_instruction_button = InlineKeyboardButton(
        text=i18n.full_instruction_button(),
        url=i18n.full_instructions_link()
    )

    faq_button = InlineKeyboardButton(
        text=i18n.faq_button(),
        url=i18n.faq_link()
    )
    support = InlineKeyboardButton(
        text=i18n.support_button(),
        url='https://t.me/WAI_support'
    )

    contact_form = InlineKeyboardButton(
        text=i18n.contact_form_button(),
        url=i18n.contact_form_url()
    )

    back_button = InlineKeyboardButton(text=i18n.back_to_menu_button(), callback_data='main_menu')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[full_instruction_button],[faq_button], [support], [contact_form], [back_button]]
    )
    return keyboard

def inline_change_language_menu(i18n: TranslatorRunner, user: dict):
    languages = {
        'ru': i18n.ru_lang_button(),
        'en': i18n.en_lang_button()
    }
    buttons = []
    for lang in languages.items():
        if user['user_language'] == lang[0]:
            text = f'{lang[1]} ✅'
        else:
            text = lang[1]
        buttons.append([InlineKeyboardButton(text=text, callback_data=f'change_language|{lang[0]}')])
    buttons.append([InlineKeyboardButton(text=i18n.back_button(), callback_data='setting_menu')])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard

def inline_download_file(i18n: TranslatorRunner, session_id: str = None):
    callback_data = f'download_file:{session_id}' if session_id else 'download_file'
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.download_file_button(), callback_data=callback_data)]
        ]
    )
    return keyboard


def sure_upgrade_keyboard(i18n: TranslatorRunner):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.sure_upgrade_subscription_button(), callback_data='sure_upgrade_subscription')]
        ]
    )
    return keyboard


def referral_program_keyboard(i18n: TranslatorRunner):
    """Клавиатура для меню реферальной программы"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.send_invitation_button(), callback_data='referral_send_invitation')],
            [InlineKeyboardButton(text=i18n.back_to_menu_button(), callback_data='main_menu')]
        ]
    )
    return keyboard


def referral_invitation_keyboard(i18n: TranslatorRunner, referral_link: str):
    """Клавиатура для приглашения с реферальной ссылкой"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n.try_whisper_ai_button(), url=referral_link)]
        ]
    )
    return keyboard


def transcription_no_summary_keyboard(i18n: TranslatorRunner, session_id: str = None, get_file_button: bool = True, show_video_button: bool = False):
    get_full_transcription_button = InlineKeyboardButton(text=i18n.get_full_transcription_button(), callback_data=f'get_full_transcription|{session_id}')
    get_video_button = InlineKeyboardButton(
        text=i18n.get_video_button(),
        callback_data=f'get_video:{session_id}'
    )
    get_summary_button = InlineKeyboardButton(text=i18n.get_summary_button(), callback_data=f'get_summary|{session_id}')
    ask_questions_callback = f'ask_questions|{session_id}' if session_id else 'ask_questions'
    ask_questions_button = InlineKeyboardButton(
            text=i18n.you_can_ask_2_button(),
            callback_data=ask_questions_callback)
    inline_keyboard = [[get_summary_button], [ask_questions_button]]
    if show_video_button:
        inline_keyboard.append([get_video_button])
    if get_file_button:
        inline_keyboard.insert(0, [get_full_transcription_button])

    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    return keyboard

def initial_payment_notification_keyboard(i18n: TranslatorRunner):
    button = InlineKeyboardButton(
        text=i18n.buy_subscription_reminder_button(),
        callback_data='payment_methods',
    )
    inline_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[button]]
    )
    return inline_keyboard

def second_payment_notification_keyboard(i18n: TranslatorRunner, bill_url: str, user_data: dict = None, payment_method: str = 'cloudpayments'):
                                        
    button = InlineKeyboardButton(
        text=i18n.pay_now_button(),
        url=bill_url,
    )
    inline_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[button]]
    )
    return inline_keyboard