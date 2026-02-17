import base64
import io
import logging
import random
import string
from typing import Tuple

from captcha.image import ImageCaptcha

logger = logging.getLogger(__name__)

def generate_captcha_text(length: int = 6) -> str:
    """Generate a random captcha text of specified length"""
    # Use only uppercase letters and digits for better readability
    characters = string.ascii_uppercase + string.digits
    # Exclude similar looking characters
    characters = characters.replace('0', '').replace('O', '').replace('1', '').replace('I', '')
    return ''.join(random.choice(characters) for _ in range(length))

def generate_captcha_image(text: str) -> bytes:
    """Generate a captcha image and return as bytes"""
    try:
        image = ImageCaptcha(width=280, height=90)
        
        # Generate image to bytes
        buffer = io.BytesIO()
        image.write(text, buffer, format='PNG')
        buffer.seek(0)
        
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Error generating CAPTCHA image: {e}")
        # Return a simple fallback image if there's an error
        fallback_image = ImageCaptcha(width=280, height=90)
        buffer = io.BytesIO()
        fallback_image.write("ABCDE", buffer, format='PNG')
        buffer.seek(0)
        return buffer.getvalue()

def create_new_captcha() -> Tuple[str, bytes]:
    """
    Create a new captcha
    Returns: (captcha_text, image_bytes)
    """
    try:
        captcha_text = generate_captcha_text()
        logger.info(f"Generated CAPTCHA text: {captcha_text}")
        
        captcha_image = generate_captcha_image(captcha_text)
        
        return captcha_text, captcha_image
    except Exception as e:
        logger.error(f"Error in create_new_captcha: {e}")
        # Return a simple fallback
        fallback_text = "ABCDE"
        fallback_image = generate_captcha_image(fallback_text)
        return fallback_text, fallback_image

def verify_captcha(user_input: str, correct_text: str) -> bool:
    """
    Verify if the user input matches the stored captcha
    Returns: True if captcha is correct, False otherwise
    """
    if not correct_text:
        logger.warning("No captcha text provided for verification")
        return False
    
    # Case-insensitive comparison
    is_correct = user_input.upper() == correct_text.upper()
    
    if is_correct:
        logger.info("CAPTCHA verification successful")
    else:
        logger.info(f"CAPTCHA verification failed. Expected: {correct_text}, Got: {user_input.upper()}")
    
    return is_correct 