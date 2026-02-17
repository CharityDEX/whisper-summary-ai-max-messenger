from maxapi.context import State, StatesGroup


class UserAudioSession(StatesGroup):
    waiting_user_audio = State()
    wait_for_generation = State()
    user_wait = State()
    enter_language = State()
    dialogue = State()
    test_filename = State()


class AdminSpamSession(StatesGroup):
    waiting_spam_message = State()
    waiting_start_id = State()
    waiting_exclude_file = State()

class AdminGiveSubscription(StatesGroup):
    waiting_for_user_data = State()
    waiting_for_subscription_length = State()
    waiting_for_confirmation = State()

class CaptchaVerification(StatesGroup):
    waiting_for_captcha = State()
    waiting_for_payment_confirmation = State()

class ReferralSession(StatesGroup):
    viewing_referral_program = State()
    sending_invitation = State()
