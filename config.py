import os
import logging
from typing import List
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

class Config:
    """Configuration class for the Telegram userbot"""
    def __init__(self):
        # Telegram API credentials
        self.API_ID = self._get_api_id()
        self.API_HASH = self._get_api_hash()

        # Session name for Telethon
        self.SESSION_NAME = "forwarder_userbot"

        # Admin user IDs for command access control
        self.ADMIN_IDS = self._get_admin_ids()

        # Logging configuration
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        # Performance configuration
        self.MAX_CONCURRENT_TASKS = self._get_int_env("MAX_CONCURRENT_TASKS", 10)
        self.CACHE_TIMEOUT = self._get_int_env("CACHE_TIMEOUT", 300)
        self.REQUEST_RETRIES = self._get_int_env("REQUEST_RETRIES", 3)


        # Validate configuration
        self._validate_config()

    def _get_api_id(self) -> int:
        """Get API ID from environment variables"""
        api_id = os.getenv("API_ID")
        if not api_id:
            logger.error("API_ID not found in environment variables")
            return 0

        try:
            return int(api_id)
        except ValueError:
            logger.error("API_ID must be an integer")
            return 0

    def _get_api_hash(self) -> str:
        """Get API Hash from environment variables"""
        api_hash = os.getenv("API_HASH", "")
        if not api_hash:
            logger.error("API_HASH not found in environment variables")
        return api_hash

    def _get_admin_ids(self) -> List[int]:
        """Get admin IDs from environment variables"""
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        if not admin_ids_str:
            logger.warning("No admin IDs configured. No one will be able to control the userbot.")
            return []

        admin_ids = []
        for admin_id in admin_ids_str.split(","):
            if admin_id.strip():
                try:
                    admin_ids.append(int(admin_id.strip()))
                except ValueError:
                    logger.error(f"Invalid admin ID: {admin_id}")

        return admin_ids

    def _get_int_env(self, key: str, default: int) -> int:
        value = os.getenv(key)
        try:
            return int(value) if value else default
        except ValueError:
            logger.warning(f"Invalid value for {key}, using default: {default}")
            return default

    def _validate_config(self):
        """Validate that all required configuration is present"""
        if self.API_ID == 0:
            logger.error("Valid API_ID is required")

        if not self.API_HASH:
            logger.error("API_HASH is required")

        if not self.ADMIN_IDS:
            logger.warning("No admin IDs configured. No one will be able to control the userbot.")

# Create a global config object
config = Config()