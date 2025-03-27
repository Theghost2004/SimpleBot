
import os
import json
import time
import random
import logging
import asyncio
import string
import re
from typing import Set, Dict, List, Callable, Optional, Union, Tuple, Any
from functools import wraps
from datetime import datetime, timedelta
from telethon import TelegramClient, events, utils
from telethon.sync import TelegramClient as SyncTelegramClient
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import GetDialogsRequest, SearchGlobalRequest, ImportChatInviteRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser, InputPeerChat, Photo
from telethon.errors import ChatAdminRequiredError, ChatWriteForbiddenError, UserBannedInChannelError
from userbot import MessageForwarder

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s - [%(filename)s:%(lineno)d]',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration class
class Config:
    def __init__(self):
        self.API_ID = int(os.getenv("API_ID", "0"))
        self.API_HASH = os.getenv("API_HASH", "")
        self.SESSION_NAME = "forwarder_userbot"
        self.ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

# All the MessageForwarder class code from userbot.py
# [Previous MessageForwarder class implementation remains exactly the same]

async def main():
    """Main function to run the userbot"""
    try:
        config = Config()
        if not config.API_ID or not config.API_HASH:
            logger.error("API_ID and API_HASH are required. Set them in .env file")
            return
            
        client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
        await client.start()
        
        # Initialize message forwarder
        forwarder = MessageForwarder(client)
        logger.info("Userbot started successfully")
        
        # Keep the bot running
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Error in main function: {str(e)}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
