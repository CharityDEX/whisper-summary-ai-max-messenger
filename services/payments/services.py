import asyncio
import csv
import datetime
import logging
from typing import Dict, List, Any, Optional
import httpx
import base64
import json

from cloudpayments import CloudPayments
import stripe
from fluentogram import TranslatorRunner

from services.init_bot import config
from models.orm import add_free_days_to_subscription_db, confirm_referral_process, get_all_users, get_payments, get_user, cancel_autopay
from utils.i18n import create_translator_hub

logger = logging.getLogger(__name__)

async def create_bill_direct(account_id: str, subscription_type: str, i18n: TranslatorRunner) -> str:
    """
    Создает счет для оплаты подписки через прямой HTTP запрос к CloudPayments API.
    :param i18n:
    :param account_id: Telegram ID пользователя
    :param subscription_type: Тип подписки (annual, monthly, weekly)
    :return: URL счета для оплаты
    """
    public_id = config.payment.public_id
    api_secret = config.payment.api_secret
    api_url = "https://api.cloudpayments.ru/orders/create"

    if subscription_type == 'annual':
        description = f"{i18n.subscription_annual()} {i18n.subscription_service_name()}"
        amount = 1990.00 # Keep annual as constant unless configured later
    elif subscription_type == 'semiannual':
        description = f"{i18n.subscription_semiannual()} {i18n.subscription_service_name()}"
        amount = float(config.cloudpayments.price.semiannual)
    elif subscription_type == 'weekly':
        description = f"{i18n.subscription_weekly()} {i18n.subscription_service_name()}" 
        amount = float(config.cloudpayments.price.weekly)
    elif subscription_type == 'monthly':
        description = f"{i18n.subscription_monthly()} {i18n.subscription_service_name()}"
        amount = float(config.cloudpayments.price.monthly)
    elif subscription_type == 'monthly_discounted_notification':
        description = f"{i18n.subscription_monthly()} {i18n.subscription_service_name()}"
        amount = float(config.cloudpayments.price.monthly_discounted_notification)
        subscription_type = 'monthly'
    else: # Default to monthly
        description = f"{i18n.subscription_monthly()} {i18n.subscription_service_name()}"
        amount = float(config.cloudpayments.price.monthly)

    custom_data_payload = {"intended_sub_type": subscription_type}
    payload = {
        "Amount": amount,
        "Currency": "RUB",
        "Description": description,
        "AccountId": account_id,
        "JsonData": custom_data_payload # Pass the dict directly, httpx will serialize to JSON body
    }

    logger.info(f"Creating CloudPayments order DIRECTLY for account {account_id} with type {subscription_type}, payload: {payload}")

    try:
        async with httpx.AsyncClient(auth=(public_id, api_secret)) as client:
            response = await client.post(api_url, json=payload) # httpx handles JSON serialization
            response.raise_for_status() # Raises HTTPStatusError for 4xx/5xx responses
            
            response_data = response.json()
            if response_data.get("Success") and response_data.get("Model") and response_data["Model"].get("Url"):
                payment_url = response_data["Model"]["Url"]
                logger.info(f"CloudPayments order created successfully (direct) for account {account_id}. URL: {payment_url}")
                return payment_url
            else:
                error_message = response_data.get("Message", "Unknown error from CloudPayments")
                logger.error(f"Failed to create CloudPayments order (direct) or get URL for account {account_id}. Success: {response_data.get('Success')}, Message: {error_message}, FullResponse: {response_data}")
                raise ValueError(f"Failed to create CloudPayments order (direct): {error_message}")

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error creating CloudPayments order (direct) for {account_id}: {e.response.status_code} - {e.response.text}", exc_info=True)
        raise ValueError(f"HTTP error from CloudPayments: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error(f"Request error creating CloudPayments order (direct) for {account_id}: {e}", exc_info=True)
        raise ValueError(f"Network error connecting to CloudPayments: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from CloudPayments (direct) for {account_id}: {e}", exc_info=True)
        raise ValueError("Invalid JSON response from CloudPayments")
    except Exception as e:
        logger.error(f"Generic error in create_bill_direct for {account_id}: {e}", exc_info=True)
        raise ValueError(f"An unexpected error occurred while creating payment order: {e}")

# Keep the old create_bill for now, or decide to replace it.
# For simplicity, I'm showing the new one and assuming you'll replace usage.
# If you rename create_bill_direct to create_bill, ensure all callers are updated.

def create_bill(account_id: str, subscription_type: str, i18n: TranslatorRunner) -> str:
    # This function will now call the direct version.
    # This maintains the same interface for existing calling code.
    return asyncio.run(create_bill_direct(account_id, subscription_type, i18n))


def create_subscription(account_id: str, token: str, subscription_type: str, start_date: datetime = None,
                        is_referral_reward: bool = False) -> Optional[str]:
    """
    Возвращает ID подписки
    :param is_referral_reward:
    :param account_id:
    :param token:
    :param subscription_type:
    :param start_date: Optional start date for the subscription.
    :return: Subscription ID or None if creation failed.
    """
    client = CloudPayments(config.payment.public_id, config.payment.api_secret)
    translator_hub = create_translator_hub()
    i18n = translator_hub.get_translator_by_locale(locale='ru')  # Default to Russian for CloudPayments

    interval = 'Month'
    if subscription_type == 'monthly':
        description = f"{i18n.subscription_monthly_payment()} {i18n.subscription_service_name()}"
        period = 1
        amount = int(config.cloudpayments.price.monthly)
        days = 30
    elif subscription_type == 'annual':
        description = f"{i18n.subscription_annual_payment()} {i18n.subscription_service_name()}"
        period = 12
        amount = 1990
        days = 365
    elif subscription_type == 'semiannual':
        description = f"{i18n.subscription_semiannual_payment()} {i18n.subscription_service_name()}"
        period = 6
        amount = int(config.cloudpayments.price.semiannual)
        days = 180
    else:
        description = f"{i18n.subscription_weekly_payment()} {i18n.subscription_service_name()}"  # Using monthly payment text for weekly
        period = 1
        interval = 'Week'
        amount = int(config.cloudpayments.price.weekly)
        days = 7

    if start_date is None:
        start_date = datetime.datetime.utcnow() + datetime.timedelta(days=days)

    if is_referral_reward:
        start_date = start_date + datetime.timedelta(days=7)
    
    try:
        response = client.create_subscription(
            amount=amount,
            currency='RUB',
            email='',
            description=description,
            account_id=account_id,
            interval=interval,
            period=period,
            start_date=start_date,
            token=token
        )
        if response and hasattr(response, 'id') and response.id:
            logger.info(f"Successfully created CloudPayments subscription {response.id} for account {account_id}")
            return response.id
        else:
            logger.error(f"Failed to create CloudPayments subscription for account {account_id}. Response: {response}")
            return None
    except Exception as e:
        logger.error(f"Exception creating CloudPayments subscription for account {account_id}: {e}", exc_info=True)
        return None

async def add_free_days_to_subscription_cloudpayments(days: int,telegram_id: int = None) -> Optional[Dict]:

    user: dict = await get_user(telegram_id)
    subscription_id = user.get('subscription_id', '')
    if subscription_id.startswith('sc_'):
        subscription_data = await get_cloudpayments_subscription_details_by_sub_id(user['telegram_id'], subscription_id, config.payment.public_id, config.payment.api_secret)

        # Получаем дату в ISO формате и конвертируем в datetime
        next_transaction_date_iso = subscription_data.get('NextTransactionDateIso')
        if not next_transaction_date_iso:
            logger.error(f"NextTransactionDateIso not found in subscription data for {subscription_id}")
            return None
        
        # Парсим ISO дату и добавляем дни
        try:
            current_date = datetime.datetime.fromisoformat(next_transaction_date_iso.replace('Z', '+00:00'))
            new_date = current_date + datetime.timedelta(days=days)
            # Конвертируем обратно в ISO формат для API
            new_date_iso = new_date.strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError as e:
            logger.error(f"Failed to parse date {next_transaction_date_iso}: {e}")
            return None

        api_url = 'https://api.cloudpayments.ru/subscriptions/update'
        payload = {
            'Id': subscription_id,
            'NextTransactionDate': new_date_iso
        }
        async with httpx.AsyncClient(auth=(config.payment.public_id, config.payment.api_secret)) as client:
            response = await client.post(api_url, json=payload)
            response.raise_for_status()
            print('add_free_days_to_subscription_cloudpayments', response.json())
            return response.json()
    else:
        logger.error(f"Subscription {subscription_id} is not a CloudPayments subscription")
        return None

async def get_cloudpayments_subscription_details_by_sub_id(
    account_id: str, 
    subscription_id_to_find: str,
    cp_public_id: str,
    cp_api_secret: str
) -> Optional[Dict]:
    """
    Fetches all subscriptions for an accountId from CloudPayments 
    and returns details for a specific subscription_id.
    Uses Basic Auth with public_id as username and api_secret as password.
    """
    api_url = "https://api.cloudpayments.ru/subscriptions/find"
    payload = {"accountId": account_id}
    
    logger.debug(f"Attempting to fetch CP subscription details for account_id: {account_id}, sub_id: {subscription_id_to_find}")

    try:
        async with httpx.AsyncClient(auth=(cp_public_id, cp_api_secret), timeout=60) as http_client:
            response = await http_client.post(api_url, json=payload)
            response.raise_for_status() 
            data = response.json()

            if data.get("Success") and "Model" in data:
                for sub_details in data["Model"]:
                    if sub_details.get("Id") == subscription_id_to_find:
                        logger.info(f"Found matching CP subscription {subscription_id_to_find} for account {account_id}.")
                        return sub_details
                logger.warning(f"Subscription {subscription_id_to_find} not found for account {account_id} among {len(data['Model'])} subscriptions listed by CP.")
                return None
            else:
                logger.error(f"CloudPayments API call to subscriptions/find for account {account_id} was not successful or 'Model' missing. Message: {data.get('Message')}")
                return None
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching subscriptions from CloudPayments for account {account_id}: {e.response.status_code} - {e.response.text}", exc_info=True)
        return None
    except httpx.RequestError as e:
        logger.error(f"Request error fetching subscriptions from CloudPayments for account {account_id}: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Generic error fetching subscriptions from CloudPayments for account {account_id}: {e}", exc_info=True)
        return None

def determine_subscription_type_from_cp_data(cp_subscription_details: Dict) -> Optional[str]:
    """
    Determines internal subscription type string from CloudPayments subscription details.
    """
    interval = cp_subscription_details.get("Interval")
    period = cp_subscription_details.get("Period")

    if not interval or period is None:
        logger.warning(f"Missing Interval ('{interval}') or Period ('{period}') in CP subscription details: {cp_subscription_details.get('Id')}")
        return None

    if interval == "Month" and period == 1:
        return "monthly"
    elif interval == "Year" and period == 1:
        return "annual"
    elif interval == "Month" and period == 12:
        return "annual"
    elif interval == "Month" and period == 6:
        return "semiannual"
    elif interval == "Week" and period == 1:
        return "weekly"
    elif interval == "Day":
        if period == 7: return "weekly"
        if period in (28, 29, 30, 31): return "monthly"
        if period in (364, 365, 366): return "annual"
    
    logger.warning(f"Could not map CloudPayments subscription interval ('{interval}') and period ({period}) to a known internal type for sub ID {cp_subscription_details.get('Id')}.")
    return None


def load_subscriptions_from_csv(file_path: str) -> list[dict]:
    """
    Loads subscription data from a CSV file.

    :param file_path: Path to the CSV file.
    :return: A list of dictionaries, where each dictionary represents a subscription.
    """
    subscriptions = []
    try:
        with open(file_path, mode='r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile, delimiter=';')
            for row in reader:
                subscriptions.append(row)
        logger.info(f"Successfully loaded {len(subscriptions)} subscriptions from {file_path}")
    except FileNotFoundError:
        logger.error(f"CSV file not found at {file_path}")
        return []
    except Exception as e:
        logger.error(f"Error reading CSV file {file_path}: {e}")
        return []
    return subscriptions


def _parse_csv_datetime(datetime_str: Optional[str]) -> Optional[datetime.datetime]:
    """Helper to parse datetime strings from CSV, returns datetime object or None."""
    if not datetime_str:
        return None
    formats_to_try = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y"]
    for fmt in formats_to_try:
        try:
            return datetime.datetime.strptime(datetime_str, fmt)
        except ValueError:
            continue
    logger.warning(f"Could not parse datetime string: {datetime_str} with tried formats.")
    return None

def _normalize_csv_status(status_str: Optional[str]) -> Optional[str]:
    """Normalizes CSV status string to lowercase or None."""
    if not status_str:
        return None
    s = status_str.strip().lower()
    if s in ('active', 'активна'):
        return 'active'
    if s in ('inactive', 'отменена', 'просрочена', 'отклонена'):
        return 'inactive'
    return s

def _normalize_db_status(db_status_val: Any) -> Optional[str]:
    """Converts DB status (bool, None, str) to 'active', 'inactive', or None."""
    if db_status_val is None:
        return None
    if isinstance(db_status_val, bool):
        return 'active' if db_status_val else 'inactive'
    if isinstance(db_status_val, str):
        s = db_status_val.lower().strip()
        if s in ('true', 'active'):
            return 'active'
        if s in ('false', 'inactive'):
            return 'inactive'
        # Potentially handle other string values if your DB uses them
        logger.warning(f"Unknown string value for DB status normalization: {db_status_val}")
    return None # Default for unhandled types or values

def group_and_sort_csv_subscriptions(flat_subscriptions: List[Dict]) -> Dict[str, List[Dict]]:
    """
    Groups subscriptions by 'ID плательщика' and sorts them by 'Дата/время создания' (latest first).
    """
    users_csv_data: Dict[str, List[Dict]] = {}
    for sub in flat_subscriptions:
        user_id = sub.get('ID плательщика')
        if not user_id:
            logger.warning(f"Skipping subscription due to missing 'ID плательщика': {sub}")
            continue
        
        # Add creation_datetime object for sorting
        sub['_creation_datetime_obj'] = _parse_csv_datetime(sub.get('Дата/время создания'))
        
        if user_id not in users_csv_data:
            users_csv_data[user_id] = []
        users_csv_data[user_id].append(sub)

    # Sort each user's subscriptions
    for user_id in users_csv_data:
        users_csv_data[user_id].sort(
            key=lambda s: s['_creation_datetime_obj'] if s['_creation_datetime_obj'] else datetime.datetime.min,
            reverse=True # Latest first
        )
    return users_csv_data


async def compare_subscriptions_with_db(csv_subscriptions: list[dict]) -> list[dict]:
    """
    Compares subscriptions from a CSV file with data in the local database.
    Uses grouped and sorted CSV data per user for more accurate comparison.

    :param csv_subscriptions: A list of subscription dictionaries loaded from CSV.
    :return: A list of dictionaries, each representing a discrepancy.
    """
    discrepancies = []
    db_users_list: list[Dict] = await get_all_users()
    db_users_map = {str(user.get('telegram_id')): user for user in db_users_list}

    # Group CSV subscriptions by user_id and sort them (latest first)
    grouped_csv_data: Dict[str, List[Dict]] = group_and_sort_csv_subscriptions(csv_subscriptions)

    for payment_subscriber_id, user_csv_subs in grouped_csv_data.items():
        try:
            if not user_csv_subs:  # Should not happen if group_and_sort works correctly
                logger.warning(f"User {payment_subscriber_id} has no CSV subscriptions listed after grouping.")
                continue

            db_user_data = db_users_map.get(str(payment_subscriber_id))

            if not db_user_data:
                # If user is in CSV but not DB, report for each of their CSV subs or just once?
                # Reporting once per user seems more concise.
                discrepancies.append({
                    'type': 'user_not_found_in_db',
                    'csv_user_id': payment_subscriber_id,
                    'details': f'User found in CSV (with {len(user_csv_subs)} sub(s)) but not in local database.',
                    'first_csv_sub_id': user_csv_subs[0].get('ID') # Info about one of their subs
                })
                continue

            # --- Determine the most relevant CSV subscription for this user ---
            active_csv_subs = [s for s in user_csv_subs if _normalize_csv_status(s.get('Статус')) == 'active']
            
            relevant_csv_sub_for_comparison: Optional[Dict] = None

            if len(active_csv_subs) > 1:
                discrepancies.append({
                    'type': 'multiple_active_csv_subs',
                    'csv_user_id': payment_subscriber_id,
                    'active_csv_sub_ids': [s.get('ID') for s in active_csv_subs],
                    'details': f"User {payment_subscriber_id} has multiple 'Активна' subscriptions in CSV."
                })
                continue # Skip further comparison for this user if multiple actives found in CSV
            elif len(active_csv_subs) == 1:
                relevant_csv_sub_for_comparison = active_csv_subs[0]
            else: # No active subscriptions, take the latest one overall
                relevant_csv_sub_for_comparison = user_csv_subs[0]


            # --- Now compare the relevant_csv_sub_for_comparison with db_user_data ---
            cloudpayments_sub_id = relevant_csv_sub_for_comparison.get('ID')
            csv_status_raw = relevant_csv_sub_for_comparison.get('Статус') # Keep raw for specific checks like "Просрочена"
            csv_end_date_str = relevant_csv_sub_for_comparison.get('Дата/время следующего платежа')
            
            normalized_relevant_csv_status = _normalize_csv_status(csv_status_raw)
            
            db_status_raw = db_user_data.get('subscription') # Raw DB status
            db_subscription_id = db_user_data.get('subscription_id')
            normalized_db_status = _normalize_db_status(db_status_raw)


            # --- Status Comparison Logic ---
            is_status_mismatch = False # Initialize to False
            override_condition_met_no_cp_mismatch = False

            # 1. Check for override conditions provided by user or system logic
            if db_user_data.get('subscription_type') == 'annual':
                override_condition_met_no_cp_mismatch = True
            elif db_subscription_id and db_subscription_id.startswith('sub_'): # Stripe subscription
                override_condition_met_no_cp_mismatch = True
            elif not db_subscription_id and normalized_db_status == 'active':
                # Bot has an active subscription but no specific CloudPayments ID.
                # This is not a CP status mismatch; it might be a generic or other payment system sub.
                override_condition_met_no_cp_mismatch = True
            elif not db_subscription_id and normalized_db_status != 'active':
                # Bot has no CP ID and no active sub, consistent with no active CP sub.
                override_condition_met_no_cp_mismatch = True
            # elif str(db_user_data.get('telegram_id')) in ['449290141', '7255216586']: # Previous user hardcode for skipping
            #     override_condition_met_no_cp_mismatch = True

            if not override_condition_met_no_cp_mismatch:
                # 2. Main comparison logic if no override declared it NOT a mismatch
                if len(active_csv_subs) > 0: # User has an active subscription in CSV
                    # normalized_relevant_csv_status is 'active' because relevant_csv_sub is an active one
                    if normalized_db_status != 'active':
                        is_status_mismatch = True # Mismatch: CSV active, DB not active
                else: # User has NO active subscriptions in CSV
                    # relevant_csv_sub is the latest overall (e.g., "Отменена", "Просрочена")
                    # normalized_relevant_csv_status is 'inactive' (or specific like 'просрочена' if not mapped by _normalize_csv_status)
                    
                    if normalized_db_status == 'active':
                        # SCENARIO: CSV (CloudPayments) shows no active sub, but Bot DB shows active.
                        # This is NOT a status mismatch here. End-date check will determine if bot's active period is valid.
                        pass # is_status_mismatch remains False
                    else: # DB is also 'inactive' or None
                        # Both CSV and DB suggest inactivity. Check if CSV's inactivity is special (e.g. "Просрочена")
                        raw_csv_status_for_latest = relevant_csv_sub_for_comparison.get('Статус', '').strip().lower()
                        if raw_csv_status_for_latest == 'просрочена':
                            # Mismatch: CSV latest is "Просрочена", DB is 'inactive' or None.
                            # "Просрочена" is more severe than a simple cancellation that results in DB 'inactive'.
                            is_status_mismatch = True 
                        # Else (e.g. CSV latest is "Отменена" and DB is 'inactive'/None) -> This is a MATCH.
                        # is_status_mismatch remains False.
            
            # Temporary debugging skips by user (ensure these are intended to bypass discrepancy logging)
            # This was the user's edit: `if db_user_data['telegram_id'] in ['449290141', '7255216586']:`
            # If such IDs should truly report no mismatch, it should be part of `override_condition_met_no_cp_mismatch`
            # For now, if those specific IDs were meant to be fully skipped for status mismatch:
            if str(db_user_data.get('telegram_id')) in ['449290141', '7255216586']:
                 is_status_mismatch = False # Force no mismatch for these specific IDs, if intended.

            if is_status_mismatch:
                discrepancies.append({
                    'type': 'status_mismatch',
                    'csv_user_id': payment_subscriber_id,
                    'relevant_csv_subscription_id': cloudpayments_sub_id,
                    'csv_status_raw': csv_status_raw,
                    'normalized_csv_status': normalized_relevant_csv_status,
                    'db_status_raw': db_status_raw,
                    'normalized_db_status': normalized_db_status,
                    'details': f'Status mismatch for user {payment_subscriber_id}. Relevant CSV status: "{csv_status_raw}", DB status: "{db_status_raw}".'
                })

            # --- End Date Comparison (using the relevant_csv_sub_for_comparison) ---
            db_end_date_obj = db_user_data.get('end_date') # This should be a datetime object
            if csv_end_date_str and db_end_date_obj:
                csv_end_date_obj = _parse_csv_datetime(csv_end_date_str)
                if csv_end_date_obj and isinstance(db_end_date_obj, datetime.datetime):
                    if db_end_date_obj.date() != csv_end_date_obj.date():
                        discrepancies.append({
                            'type': 'end_date_mismatch',
                            'csv_user_id': payment_subscriber_id,
                            'relevant_csv_subscription_id': cloudpayments_sub_id,
                            'csv_end_date': csv_end_date_obj.strftime('%Y-%m-%d'),
                            'db_end_date': db_end_date_obj.strftime('%Y-%m-%d'),
                            'details': f'End date mismatch for user {payment_subscriber_id}.'
                        })
                elif csv_end_date_obj and not isinstance(db_end_date_obj, datetime.datetime):
                     logger.warning(f"DB end_date for user {payment_subscriber_id} is not a datetime object: {db_end_date_obj}")


        except Exception as e:
            logger.error(f"Error processing subscriptions for user ID {payment_subscriber_id}: {e} - First CSV sub: {user_csv_subs[0] if user_csv_subs else 'N/A'}", exc_info=True)
            discrepancies.append({
                'type': 'processing_error_user_level',
                'csv_user_id': payment_subscriber_id,
                'error_message': str(e),
                'details': f"Failed to process subscription comparison for user {payment_subscriber_id}."
            })

    if discrepancies:
        logger.info(f"Found {len(discrepancies)} discrepancies between CSV (user-grouped) and DB.")
    else:
        logger.info("No discrepancies found between CSV (user-grouped) and DB subscriptions.")
    return discrepancies

async def update_cloudpayments_subscription(
    subscription_id: str, 
    new_subscription_type: str = "monthly",
    i18n: TranslatorRunner = None
) -> Optional[Dict]:
    """
    Updates a CloudPayments subscription using the /subscriptions/update endpoint.
    Currently supports upgrading from weekly to monthly subscriptions.
    
    :param subscription_id: CloudPayments subscription ID to update
    :param new_subscription_type: Target subscription type ('monthly', 'weekly', 'annual')
    :param i18n: Translator for localized descriptions
    :return: Updated subscription details dict or None if failed
    """
    public_id = config.payment.public_id
    api_secret = config.payment.api_secret
    api_url = "https://api.cloudpayments.ru/subscriptions/update"
    
    # Set parameters based on subscription type
    if new_subscription_type == 'monthly':
        amount = float(config.cloudpayments.price.monthly)
        interval = 'Month'
        period = 1
        description = f"{i18n.subscription_monthly_payment()} {i18n.subscription_service_name()}" if i18n else "Monthly subscription payment"
    elif new_subscription_type == 'weekly':
        amount = float(config.cloudpayments.price.weekly)
        interval = 'Week'
        period = 1
        description = f"{i18n.subscription_weekly_payment()} {i18n.subscription_service_name()}" if i18n else "Weekly subscription payment"
    elif new_subscription_type == 'annual':
        amount = 1990.00
        interval = 'Month'
        period = 12
        description = f"{i18n.subscription_annual_payment()} {i18n.subscription_service_name()}" if i18n else "Annual subscription payment"
    elif new_subscription_type == 'semiannual':
        amount = float(config.cloudpayments.price.semiannual)
        interval = 'Month'
        period = 6
        description = f"{i18n.subscription_semiannual_payment()} {i18n.subscription_service_name()}" if i18n else "Semiannual subscription payment"
    else:
        logger.error(f"Unsupported subscription type for update: {new_subscription_type}")
        return None

    payload = {
        "Id": subscription_id,
        "Description": description,
        "Amount": amount,
        "Currency": "RUB",
        "Interval": interval,
        "Period": period
    }

    logger.info(f"Updating CloudPayments subscription {subscription_id} to {new_subscription_type} with payload: {payload}")

    try:
        async with httpx.AsyncClient(auth=(public_id, api_secret)) as client:
            response = await client.post(api_url, json=payload)
            response.raise_for_status()
            
            response_data = response.json()
            if response_data.get("Success") and response_data.get("Model"):
                updated_subscription = response_data["Model"]
                logger.info(f"Successfully updated CloudPayments subscription {subscription_id} to {new_subscription_type}")
                return updated_subscription
            else:
                error_message = response_data.get("Message", "Unknown error from CloudPayments")
                logger.error(f"Failed to update CloudPayments subscription {subscription_id}. Success: {response_data.get('Success')}, Message: {error_message}")
                return None

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error updating CloudPayments subscription {subscription_id}: {e.response.status_code} - {e.response.text}", exc_info=True)
        return None
    except httpx.RequestError as e:
        logger.error(f"Request error updating CloudPayments subscription {subscription_id}: {e}", exc_info=True)
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from CloudPayments for subscription {subscription_id}: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Generic error updating CloudPayments subscription {subscription_id}: {e}", exc_info=True)
        return None


        
        
        
        


if __name__ == "__main__":
    hub = create_translator_hub()
    i18n = hub.get_translator_by_locale(locale='ru')
    asyncio.run(create_bill_direct('449290141', 'monthly_discounted_notification', i18n))
    print(create_subscription(account_id='7795241433',
                              subscription_type='monthly',
                              token='tk_b5503a39c2c6081ad167860d61f7e'))
    # import asyncio
    # # Make sure the path to your CSV is correct
    # csv_path = '/home/vadim/PycharmProjects/maxim_voice_summary/Подписки (1).csv' # Define path for clarity
    # csv_data = load_subscriptions_from_csv(csv_path)
    # if csv_data:
    #     print(f"Loaded {len(csv_data)} records from {csv_path}")
    #     discrepancies = asyncio.run(compare_subscriptions_with_db(csv_data))
    #     if discrepancies:
    #         print(f"\nFound {len(discrepancies)} discrepancies:")
    #         for i, d in enumerate(discrepancies):
    #             print(f"--- Discrepancy {i+1} ---")
    #             print(f"  Type: {d.get('type')}")
    #             print(f"  CSV User ID: {d.get('csv_user_id')}")
    #             print(f"  CSV Subscription ID: {d.get('relevant_csv_subscription_id')}")
    #             if d.get('type') == 'status_mismatch':
    #                 print(f"  CSV Status: {d.get('csv_status_raw')}")
    #                 print(f"  DB Status: {d.get('db_status_raw')}")
    #                 print(f"  Normalized CSV Status: {d.get('normalized_csv_status')}")
    #                 print(f"  Normalized DB Status: {d.get('normalized_db_status')}")
    #             elif d.get('type') == 'end_date_mismatch':
    #                 print(f"  CSV End Date: {d.get('csv_end_date')}")
    #                 print(f"  DB End Date: {d.get('db_end_date')}")
    #             elif d.get('type') == 'user_not_found_in_db':
    #                  print(f"  Details: {d.get('details')}")
    #             elif d.get('type') == 'processing_error_user_level':
    #                  print(f"  Error: {d.get('error_message')}")
    #             print(f"  Full Details: {d.get('details', 'N/A')}")
    #         print("\n--- End of Discrepancy Report ---")
    #     else:
    #         print("\nNo discrepancies found between CSV and database after comparison.")
    # else:
    #     print(f"Could not load data from CSV file: {csv_path}")