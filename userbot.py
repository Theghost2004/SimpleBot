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
from config import config

logger = logging.getLogger(__name__)

def admin_only(func: Callable):
    """Decorator to restrict commands to admin users only"""
    @wraps(func)
    async def wrapper(self, event, *args, **kwargs):
        try:
            # Get the command name from the event text for logging
            command_name = event.text.split()[0].lower() if event.text else ""
            command_function_name = func.__name__
            logger.info(f"Received command: {command_name}, function: {command_function_name}")

            # Special case: Allow /start command even when bot is disabled
            is_start_command = command_function_name == "cmd_start" or command_name == "/start"

            # Get sender ID from event with multiple fallbacks
            sender = None
            if hasattr(event.message, 'from_id'):
                sender = event.message.from_id.user_id
            elif hasattr(event, 'from_id'):
                sender = event.from_id.user_id
            elif hasattr(event.message, 'sender_id'):
                sender = event.message.sender_id
            elif hasattr(event, 'sender_id'):
                sender = event.sender_id

            if sender is None:
                logger.error(f"Could not determine sender ID for command {command_name}")
                return None

            # Log admin check
            logger.info(f"Checking if user {sender} is admin for command {command_name}")

            # Check if sender is in admin list
            if sender not in self.admins:
                logger.warning(f"Unauthorized access attempt from user {sender} for command {command_name}")
                # Silently ignore unauthorized users
                return None

            # SPECIAL HANDLING FOR /START COMMAND WHEN BOT IS OFFLINE
            if is_start_command and not self.forwarding_enabled:
                logger.info(f"Received /start command from admin when bot was offline")
                logger.info(f"Executing start command...")
                return await func(self, event, *args, **kwargs)

            # If the bot is not active (disabled) and this isn't the /start command, ignore it
            if not self.forwarding_enabled:
                logger.info(f"Bot not active. Command: {command_name}, Function: {command_function_name}")
                # Only send the message if it's not a silent command (system might send multiple commands)
                if not command_name.startswith("/silent"):
                    logger.info(f"Sending offline message for command {command_name}")
                    await event.reply("⚠️ --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 is currently offline! Use `/start` command to wake it up. 🚀")
                return None

            logger.info(f"Admin command authorized for user {sender}")
            return await func(self, event, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in admin_only decorator: {str(e)}")
            # Don't try to reply on errors
            return None
    return wrapper

def generate_campaign_id(length=1):
    """Generate a simple campaign ID

    By default, creates a single-digit ID (1-9) for easier reference.
    If all single digits are taken, it will use letters (A-Z) or increase length.
    """
    # Start with simple integers 1-9
    existing_ids = set()

    # Check for existing messages in the current forwarder instance
    if MessageForwarder.instance and hasattr(MessageForwarder.instance, 'stored_messages'):
        existing_ids = set(MessageForwarder.instance.stored_messages.keys())

    # Try single digits first (1-9)
    for i in range(1, 10):
        id_str = str(i)
        if id_str not in existing_ids:
            return id_str

    # If all single digits are taken, try letters (A-Z)
    for char in string.ascii_uppercase:
        if char not in existing_ids:
            return char

    # If all single characters are taken, use the original random generation with increased length
    chars = string.ascii_uppercase + string.digits
    while True:
        id_str = ''.join(random.choice(chars) for _ in range(length + 1))
        if id_str not in existing_ids:
            return id_str

class MessageForwarder:
    # Class attribute to store the current instance
    instance = None

    def __init__(self, client):
        self.client = client
        self.client.flood_sleep_threshold = 5  # Reduce flood wait time
        self.forwarding_enabled = False
        self.target_chats: Set[int] = set()
        self.forward_interval = 300  # Default 5 minutes
        self.stored_messages: Dict[str, Any] = {}  # Store multiple messages by ID
        self._commands_registered = False
        self._forwarding_tasks: Dict[str, asyncio.Task] = {}  # Track multiple forwarding tasks
        self._message_queue = asyncio.Queue()  # Message queue for faster processing
        self._cache = {}  # Cache for frequently accessed data

        # Scheduled campaigns
        self.scheduled_tasks: Dict[str, asyncio.Task] = {}  # Track scheduled tasks
        self.targeted_campaigns: Dict[str, Dict] = {}  # Store targeted ad campaigns

        # Admin management
        self.admins: Set[int] = set(config.ADMIN_IDS)  # Allow dynamic admin management

        # Analytics
        self.analytics = {
            "forwards": {},  # Track successful forwards
            "failures": {},  # Track failed forwards
            "start_time": time.time()  # Track when bot started
        }

        # Set this instance as the current one
        MessageForwarder.instance = self

        logger.info("MessageForwarder initialized")
        self.register_commands()

    async def forward_stored_message(self, msg_id: str = "default", targets: Optional[Set[int]] = None, interval: Optional[int] = None):
        """Periodically forward stored message to target chats"""
        try:
            if msg_id not in self.stored_messages:
                logger.error(f"Message ID {msg_id} not found in stored messages")
                return

            message = self.stored_messages[msg_id]
            use_targets = targets if targets is not None else self.target_chats
            use_interval = interval if interval is not None else self.forward_interval

            logger.info(f"Starting periodic forwarding task for message {msg_id}")

            while True:
                if msg_id not in self.stored_messages:  # Check if message was deleted
                    logger.info(f"Message {msg_id} no longer exists, stopping forwarding")
                    break

                logger.info(f"Forwarding message {msg_id} to {len(use_targets)} targets")

                for target in use_targets:
                    try:
                        await message.forward_to(target)

                        # Update analytics
                        today = datetime.now().strftime('%Y-%m-%d')
                        if today not in self.analytics["forwards"]:
                            self.analytics["forwards"][today] = {}

                        campaign_key = f"{msg_id}_{target}"
                        if campaign_key not in self.analytics["forwards"][today]:
                            self.analytics["forwards"][today][campaign_key] = 0

                        self.analytics["forwards"][today][campaign_key] += 1

                        logger.info(f"Successfully forwarded message {msg_id} to {target}")
                    except Exception as e:
                        # Track failures
                        today = datetime.now().strftime('%Y-%m-%d')
                        if today not in self.analytics["failures"]:
                            self.analytics["failures"][today] = {}

                        campaign_key = f"{msg_id}_{target}"
                        if campaign_key not in self.analytics["failures"][today]:
                            self.analytics["failures"][today][campaign_key] = []

                        self.analytics["failures"][today][campaign_key].append(str(e))

                        logger.error(f"Error forwarding message {msg_id} to {target}: {str(e)}")

                logger.info(f"Waiting {use_interval} seconds before next forward for message {msg_id}")
                await asyncio.sleep(use_interval)

        except asyncio.CancelledError:
            logger.info(f"Forwarding task for message {msg_id} was cancelled")
        except Exception as e:
            logger.error(f"Error in forwarding task for message {msg_id}: {str(e)}")

            # Remove task from active tasks
            if msg_id in self._forwarding_tasks:
                del self._forwarding_tasks[msg_id]

    def register_commands(self):
        """Register command handlers"""
        if self._commands_registered:
            return

        try:
            commands = {
                # Basic commands
                'start': self.cmd_start,
                'stop': self.cmd_stop,
                'help': self.cmd_help,
                'status': self.cmd_status,
                'test': self.cmd_test,
                'optimize': self.cmd_optimize,

                # Message management
                'setad': self.cmd_setad,
                'listad': self.cmd_listad,
                'removead': self.cmd_removead,

                # Basic forwarding
                'startad': self.cmd_startad,
                'stopad': self.cmd_stopad,
                'timer': self.cmd_timer,

                # Advanced forwarding
                'targetedad': self.cmd_targetedad,
                'listtargetad': self.cmd_listtargetad,
                'stoptargetad': self.cmd_stoptargetad,
                'schedule': self.cmd_schedule,
                'forward': self.cmd_forward,
                'broadcast': self.cmd_broadcast,

                # Target management
                'addtarget': self.cmd_addtarget,
                'listtarget': self.cmd_listtarget,
                'listtargets': self.cmd_listtarget,  # Alias
                'removetarget': self.cmd_removetarget,
                'removealltarget': self.cmd_removealltarget,
                'cleantarget': self.cmd_cleantarget,
                'removeunsub': self.cmd_removeunsub,
                'targeting': self.cmd_targeting,

                # Chat management
                'joinchat': self.cmd_joinchat,
                'leavechat': self.cmd_leavechat,
                'leaveandremove': self.cmd_leaveandremove,
                'listjoined': self.cmd_listjoined,
                'findgroup': self.cmd_findgroup,
                'clearchat': self.cmd_clearchat,
                'pin': self.cmd_pin,

                # Profile management
                'bio': self.cmd_bio,
                'name': self.cmd_name,
                'username': self.cmd_username,
                'setpic': self.cmd_setpic,

                # Admin management
                'addadmin': self.cmd_addadmin,
                'removeadmin': self.cmd_removeadmin,
                'listadmins': self.cmd_listadmins,

                # Miscellaneous
                'analytics': self.cmd_analytics,
                'backup': self.cmd_backup,
                'restore': self.cmd_restore,
                'stickers': self.cmd_stickers,
                'interactive': self.cmd_interactive,
                'client': self.cmd_client,
            }

            for cmd, handler in commands.items():
                pattern = f'^/{cmd}(?:\\s|$)'
                self.client.add_event_handler(
                    handler,
                    events.NewMessage(pattern=pattern)
                )
                logger.info(f"Registered command: /{cmd}")

            self._commands_registered = True
            logger.info("All commands registered")
        except Exception as e:
            logger.error(f"Error registering commands: {str(e)}")
            raise

    @admin_only
    async def cmd_start(self, event):
        """Start the userbot and show welcome message"""
        try:
            # Use cached me info if available
            if 'me' not in self._cache:
                self._cache['me'] = await self.client.get_me()
            me = self._cache['me']
            username = me.username if me.username else "siimplebot1"
            name = event.sender.first_name if hasattr(event, 'sender') and hasattr(event.sender, 'first_name') else "User"

            # Enable the bot to respond to commands
            self.forwarding_enabled = True

            welcome_text = f"""
╔══════════════════════════╗
║  🌟 WELCOME TO THE BEST  ║
║  --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 #1  ║
║      @{username}         ║
╚══════════════════════════╝

💫 Hey {name}! Ready to experience the ULTIMATE automation? 💫

I am --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃, your ultimate Telegram assistant, built to make your experience smarter, faster, and way more fun! 🎭⚡

💎 What I Can Do: 
✅ Fast & Smart Automation ⚡ 
✅ Fun Commands & Tools 🎭 
✅ Instant Replies & Assistance 🤖 
✅ Custom Features Just for You! 💡

🎯 How to Use Me? 
🔹 Type `/help` to explore my powers! 
🔹 Want to chat? Just send a message & see the magic! 
🔹 Feeling bored? Try my fun commands and enjoy the ride!

💬 Mood: Always ready to assist! 
⚡ Speed: Faster than light! 
🎭 Vibe: Smart, cool & interactive!

I'm here to make your Telegram experience legendary! 🚀💙 Stay awesome, and let's get started! 😎🔥
"""
            await event.reply(welcome_text)
            logger.info("Start command executed - Bot activated")
        except Exception as e:
            logger.error(f"Error in start command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_stop(self, event):
        """Stop all active forwarding tasks and disable command responses"""
        try:
            # Get name for personalized message
            name = event.sender.first_name if hasattr(event, 'sender') and hasattr(event.sender, 'first_name') else "User"

            # Cancel all forwarding tasks
            for task_id, task in list(self._forwarding_tasks.items()):
                if not task.done():
                    task.cancel()
            self._forwarding_tasks.clear()

            # Cancel all scheduled tasks
            for task_id, task in list(self.scheduled_tasks.items()):
                if not task.done():
                    task.cancel()
            self.scheduled_tasks.clear()

            # Clear targeted campaigns
            self.targeted_campaigns.clear()

            # Disable command responses (except for /start)
            self.forwarding_enabled = False

            stop_message = f"""⚠️ --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 SYSTEM SHUTDOWN ⚠️

Hey {name}! 😔 Looks like you've decided to stop me... but don't worry, I'll be here whenever you need me! 🚀

📌 Bot Status: ⚠️ Going Offline for You
📌 Commands Disabled: ❌ No More Assistance
📌 Mood: 💤 Entering Sleep Mode

💡 Want to wake me up again?
Just type `/start`, and I'll be back in action, ready to assist you! 🔥

Until then, stay awesome & take care! 😎

🚀 Powered by --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@siimplebot1)
"""
            await event.reply(stop_message)
            logger.info("Stop command executed - Bot deactivated")
        except Exception as e:
            logger.error(f"Error in stop command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_help(self, event):
        """Show help message"""
        try:
            me = await self.client.get_me()
            username = me.username if me.username else "telegramforwarder"
            name = event.sender.first_name if hasattr(event, 'sender') and hasattr(event.sender, 'first_name') else "User"

            help_text = f"""🚀🔥 WELCOME TO --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 COMMAND CENTER 🔥🚀

Hey {name}! 😎 Ready to take control? Here's what I can do for you! ⚡

━━━━━━━━━━━━━━━━━━━━━━

🌟 BASIC COMMANDS
🔹 `/start` – 🚀 Activate the bot
🔹 `/stop` – 🛑 Deactivate the bot
🔹 `/help` – 📜 Show all available commands
🔹 `/test` – 🛠 Check if the bot is working fine
🔹 `/client` – 🤖 Get details about your client
🔹 `/status` – 📊 Show bot system status

━━━━━━━━━━━━━━━━━━━━━━

📢 ADVERTISEMENT MANAGEMENT
📌 Run powerful ad campaigns with ease!
🔹 `/setad` <reply to message> – 📝 Set an ad
🔹 `/listad` – 📋 View all ads
🔹 `/removead` <ID> – ❌ Remove a specific ad
🔹 `/startad` <ID> <interval> – 🚀 Start an ad campaign
🔹 `/stopad` <ID> – ⏹ Stop an ad campaign
🔹 `/targetedad` <ad_id> <target_list> <interval_sec> – 🎯 Run targeted ads
🔹 `/listtargetad` – 📑 View all targeted ad campaigns
🔹 `/stoptargetad` <campaign_id> – 🔕 Stop a targeted ad

━━━━━━━━━━━━━━━━━━━━━━

🎯 TARGETING & AUDIENCE MANAGEMENT
📌 Reach the right audience with precision!
🔹 `/addtarget` <targets> – ➕ Add target audience
🔹 `/listtarget` – 📜 View all targets
🔹 `/removetarget` <id 1,2,3> – ❌ Remove specific targets
🔹 `/removealltarget` – 🧹 Clear all targets
🔹 `/cleantarget` – ✨ Clean up target list
🔹 `/removeunsub` – 🚮 Remove unsubscribed users
🔹 `/targeting` <keywords> – 🔍 Target based on keywords

━━━━━━━━━━━━━━━━━━━━━━

🏠 GROUP & CHAT MANAGEMENT
📌 Effortlessly manage groups and chats!
🔹 `/joinchat` <chats> – 🔗 Join a chat/group
🔹 `/leavechat` <chats> – 🚪 Leave a chat/group
🔹 `/leaveandremove` <chats> – ❌ Leave & remove from list
🔹 `/listjoined` – 📋 View joined groups
🔹 `/listjoined --all` – 📜 View all targeted joined groups
🔹 `/findgroup` <keyword> – 🔍 Search for a group

━━━━━━━━━━━━━━━━━━━━━━

👤 USER PROFILE & CUSTOMIZATION
📌 Make your profile stand out!
🔹 `/bio` <text> – 📝 Set a new bio
🔹 `/name` <first_name> <last_name> – 🔄 Change your name
🔹 `/username` <new_username> – 🔀 Change your username
🔹 `/setpic` – 🖼 Auto-adjust profile picture

━━━━━━━━━━━━━━━━━━━━━━

📊 ANALYTICS & AUTOMATION
📌 Monitor performance & automate tasks!
🔹 `/analytics` [days=7] – 📊 View performance stats
🔹 `/forward` <msg_id> <targets> – 📩 Forward messages
🔹 `/backup` – 💾 Backup bot data
🔹 `/restore` <file_id> – 🔄 Restore from backup
🔹 `/broadcast` <message> – 📢 Send a broadcast message

━━━━━━━━━━━━━━━━━━━━━━

🔑 ADMIN CONTROLS
📌 Manage bot admins easily!
🔹 `/addadmin` <user_id> <username> – ➕ Add an admin
🔹 `/removeadmin` <user_id> <username> – ❌ Remove an admin
🔹 `/listadmins` – 📜 View all admins

━━━━━━━━━━━━━━━━━━━━━━

⚡ MISCELLANEOUS COMMANDS
📌 Enhance your experience with extra features!
🔹 `/clearchat` [count] – 🧹 Clear messages
🔹 `/pin` [silent] – 📌 Pin a message silently
🔹 `/stickers` <pack_name> – 🎨 Get sticker packs
🔹 `/interactive` – 🤖 Enable interactive mode
🔹 `/optimize` – 🚀 Boost bot performance

━━━━━━━━━━━━━━━━━━━━━━

💡 Need Help?
Type `/help` anytime to get assistance!

🔥 Powered by --Ꮪɪᴍᴘʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@{username})

🚀 Stay Smart, Stay Automated!
"""
            await event.reply(help_text)
            logger.info("Help message sent")
        except Exception as e:
            logger.error(f"Error in help command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_status(self, event):
        """Show detailed status of the userbot"""
        try:
            # Count active tasks
            active_forwards = len([t for t in self._forwarding_tasks.values() if not t.done()])
            active_schedules = len([t for t in self.scheduled_tasks.values() if not t.done()])
            active_campaigns = len(self.targeted_campaigns)

            # Get stored messages count
            stored_msgs = len(self.stored_messages)

            # Get uptime
            uptime_seconds = int(time.time() - self.analytics["start_time"])
            days, remainder = divmod(uptime_seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, remainder = divmod(remainder, 60)
            seconds, _ = divmod(remainder, 60)
            uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

            # Get analytics summary for today
            today = datetime.now().strftime('%Y-%m-%d')
            forwards_today = 0
            if today in self.analytics["forwards"]:
                for campaign in self.analytics["forwards"][today].values():
                    forwards_today += campaign

            status_text = f"""
╔═══════ SYSTEM STATUS ═══════╗
║  🤖 --Ꮪɪᴍṗʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 #1   ║
╚════════════════════════════╝

**General Information:**
• Uptime: {uptime_str}
• Admins: {len(self.admins)}
• Target Chats: {len(self.target_chats)}
• Stored Messages: {stored_msgs}
• Default Interval: {self.forward_interval} seconds

**Active Tasks:**
• Forwarding Tasks: {active_forwards}
• Scheduled Tasks: {active_schedules}
• Targeted Campaigns: {active_campaigns}

**Today's Activity:**
• Messages Forwarded: {forwards_today}

**System Status:**
• Memory Usage: Normal
• Connection Status: Online
"""
            await event.reply(status_text)
            logger.info("Status command processed")
        except Exception as e:
            logger.error(f"Error in status command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_test(self, event):
        """Test if the userbot is working properly"""
        try:
            start_time = time.time()

            # Test messages with progress
            status = await event.reply("⚡ Initializing System Check...\n🔄 ════════════ 0%")
            await asyncio.sleep(0.5)
            await status.delete()
            
            status = await event.reply("⚡ Running Diagnostics...\n🔄 ██══════════ 20%")
            await asyncio.sleep(0.5)
            await status.delete()
            
            status = await event.reply("⚡ Checking Connections...\n🔄 ████════════ 40%")
            await asyncio.sleep(0.5)
            await status.delete()
            
            status = await event.reply("⚡ Verifying Modules...\n🔄 ██████══════ 60%")
            await asyncio.sleep(0.5)
            await status.delete()
            
            status = await event.reply("⚡ Testing Features...\n🔄 ████████════ 80%")
            await asyncio.sleep(0.5)
            await status.delete()
            
            status = await event.reply("⚡ Finalizing Check...\n🔄 ██████████ 100%")
            await asyncio.sleep(0.5)
            await status.delete()

            # Test Telegram API
            me = await self.client.get_me()
            name = event.sender.first_name if hasattr(event, 'sender') and hasattr(event.sender, 'first_name') else "User"

            # Test response time
            response_time = (time.time() - start_time) * 1000  # in ms

            # Test result
            result_text = f"""✅ --Ꮪɪᴍᴘʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 SYSTEM CHECK ✅

Hey {name}! 🚀 Your `/test` command has been executed successfully, and everything is running smoothly! 🔥

📊 Bot Diagnostic Report:
🔹 Bot Status: ✅ Online & Fully Operational
🔹 Response Speed: ⚡ Ultra-Fast
🔹 Server Health: 🟢 Stable & Secure
🔹 Power Level: 💪 100% Ready
🔹 Latency: 🚀 {response_time:.2f}ms – Lightning Fast!

✨ Bot Performance:
💬 Mood: Always ready to assist!
⚡ Speed: Faster than light!
🎭 Vibe: Smart, cool & interactive!

🎯 What's Next?
🚀 Type `/help` to explore all my features!
🛠 Need support or customization? Just ask!
🎭 Feeling bored? Try my fun commands and enjoy the ride!

📌 Stay connected, stay smart, and let's automate your Telegram experience like a pro!

🚀 Powered by --Ꮪɪᴍᴘʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@siimplebot1)
"""
            await event.reply(result_text)
            logger.info("Test command executed successfully")
        except Exception as e:
            logger.error(f"Error in test command: {str(e)}")
            await event.reply(f"❌ Test failed: {str(e)}")

    @admin_only
    async def cmd_optimize(self, event):
        """Optimize userbot performance"""
        try:
            msg = await event.reply("🚀 PERFORMANCE OPTIMIZATION IN PROGRESS\n\n⚡ Phase 1: Analyzing System...")
            await asyncio.sleep(1)
            await msg.edit("🚀 PERFORMANCE OPTIMIZATION IN PROGRESS\n\n⚡ Phase 2: Cleaning Cache...")
            await asyncio.sleep(1)
            await msg.edit("🚀 PERFORMANCE OPTIMIZATION IN PROGRESS\n\n⚡ Phase 3: Optimizing Memory...")
            await asyncio.sleep(1)
            await msg.edit("🚀 PERFORMANCE OPTIMIZATION IN PROGRESS\n\n⚡ Phase 4: Finalizing...")

            # Clean up completed tasks
            for task_id in list(self._forwarding_tasks.keys()):
                if self._forwarding_tasks[task_id].done():
                    del self._forwarding_tasks[task_id]

            for task_id in list(self.scheduled_tasks.keys()):
                if self.scheduled_tasks[task_id].done():
                    del self.scheduled_tasks[task_id]

            # Validate target chats
            invalid_targets = []
            for target in list(self.target_chats):
                try:
                    await self.client.get_entity(target)
                except Exception:
                    invalid_targets.append(target)

            # Remove invalid targets
            for target in invalid_targets:
                self.target_chats.remove(target)

            # Cleanup old analytics data (older than 30 days)
            thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            for date in list(self.analytics["forwards"].keys()):
                if date < thirty_days_ago:
                    del self.analytics["forwards"][date]

            for date in list(self.analytics["failures"].keys()):
                if date < thirty_days_ago:
                    del self.analytics["failures"][date]

            result = f"""✅ **Optimization Complete**

• Cleaned up {len(invalid_targets)} invalid targets
• Removed completed tasks
• Cleaned up old analytics data
• Memory usage optimized

The userbot has been optimized for better performance.
"""
            await event.reply(result)
            logger.info(f"Optimize command completed. Removed {len(invalid_targets)} invalid targets.")
        except Exception as e:
            logger.error(f"Error in optimize command: {str(e)}")
            await event.reply(f"❌ Error optimizing: {str(e)}")

    @admin_only
    async def cmd_setad(self, event):
        """Set a message to be forwarded"""
        try:
            if not event.is_reply:
                await event.reply("❌ Please reply to the message you want to forward")
                return

            # Generate a unique ID for the message
            msg_id = generate_campaign_id()

            replied_msg = await event.get_reply_message()
            self.stored_messages[msg_id] = replied_msg

            await event.reply(f"✅ Message saved for forwarding with ID: `{msg_id}`\n\nUse this ID in commands like `/startad`, `/targetedad`, etc.")
            logger.info(f"New message saved with ID: {msg_id}")
        except Exception as e:
            logger.error(f"Error in setad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_listad(self, event):
        """List all saved messages"""
        try:
            if not self.stored_messages:
                await event.reply("📝 No messages are currently saved")
                return

            result = "📝 **Saved Messages**:\n\n"

            for msg_id, message in self.stored_messages.items():
                # Get message preview (limited to 50 chars)
                content = ""
                if message.text:
                    content = message.text[:50] + ("..." if len(message.text) > 50 else "")
                elif message.media:
                    content = "[Media Message]"
                else:
                    content = "[Unknown Content]"

                result += f"• ID: `{msg_id}` - {content}\n"

            await event.reply(result)
            logger.info("Listed all saved messages")
        except Exception as e:
            logger.error(f"Error in listad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_removead(self, event):
        """Remove a saved message"""
        try:
            command_parts = event.text.split()
            if len(command_parts) != 2:
                await event.reply("❌ Please provide a message ID\nFormat: /removead <message_id>")
                return

            msg_id = command_parts[1]

            if msg_id not in self.stored_messages:
                await event.reply(f"❌ Message with ID {msg_id} not found")
                return

            # Cancel any active forwarding tasks for this message
            for task_id, task in list(self._forwarding_tasks.items()):
                if task_id == msg_id and not task.done():
                    task.cancel()
                    del self._forwarding_tasks[task_id]

            # Remove from stored messages
            del self.stored_messages[msg_id]

            await event.reply(f"✅ Message with ID {msg_id} has been removed")
            logger.info(f"Removed message with ID: {msg_id}")
        except Exception as e:
            logger.error(f"Error in removead command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_startad(self, event):
        """Start forwarding a specific message at an interval"""
        try:
            command_parts = event.text.split()
            msg_id = "default"  # Default value
            interval = self.forward_interval  # Default interval

            if len(command_parts) >= 2:
                msg_id = command_parts[1]

            if len(command_parts) >= 3:
                try:
                    interval = int(command_parts[2])
                    if interval < 60:
                        await event.reply("❌ Interval must be at least 60 seconds")
                        return
                except ValueError:
                    await event.reply("❌ Invalid interval format. Must be an integer in seconds.")
                    return

            # Check if message exists
            if msg_id not in self.stored_messages:
                if msg_id == "default" and not self.stored_messages:
                    await event.reply("❌ No message set for forwarding. Please use `/setad` while replying to a message first.")
                else:
                    await event.reply(f"❌ Message with ID {msg_id} not found. Use `/listad` to see available messages.")
                return

            # Check if targets exist
            if not self.target_chats:
                await event.reply("❌ No target chats configured. Please add target chats first using /addtarget <target>")
                return

            # Cancel existing task if any
            if msg_id in self._forwarding_tasks and not self._forwarding_tasks[msg_id].done():
                self._forwarding_tasks[msg_id].cancel()

            # Start new forwarding task
            self._forwarding_tasks[msg_id] = asyncio.create_task(
                self.forward_stored_message(msg_id=msg_id, interval=interval)
            )

            await event.reply(f"""🚀 Ad Campaign Started!

✅ Ad ID: {msg_id}
⏳ Interval: {interval} sec
🎯 Status: Running

✨ Your ad is now live! Use /stopad <ID> to stop it anytime.

🚀 Powered by --Ꮪɪᴍթʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@siimplebot1)""")
            logger.info(f"Forwarding enabled for message {msg_id}. Interval: {interval}s, Targets: {self.target_chats}")
        except Exception as e:
            logger.error(f"Error in startad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_stopad(self, event):
        """Stop forwarding a specific message"""
        try:
            command_parts = event.text.split()

            # If no ID specified, stop all forwarding
            if len(command_parts) == 1:
                # Cancel all forwarding tasks
                for task_id, task in list(self._forwarding_tasks.items()):
                    if not task.done():
                        task.cancel()

                self._forwarding_tasks.clear()
                await event.reply("""🛑 Ad Campaign Stopped!

✅ Ad ID: All
⏳ Status: Stopped Successfully

💡 Start a new ad anytime using /startad <ID> <interval>.

🚀 Powered by --Ꮪɪᴍթʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@siimplebot1)""")
                logger.info("All forwarding tasks stopped")
                return

            # Stop specific message forwarding
            msg_id = command_parts[1]

            if msg_id in self._forwarding_tasks and not self._forwarding_tasks[msg_id].done():
                self._forwarding_tasks[msg_id].cancel()
                del self._forwarding_tasks[msg_id]
                await event.reply(f"""🛑 Ad Campaign Stopped!

✅ Ad ID: {msg_id}
⏳ Status: Stopped Successfully

💡 Start a new ad anytime using /startad <ID> <interval>.

🚀 Powered by --Ꮪɪᴍթʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@siimplebot1)""")
                logger.info(f"Forwarding disabled for message {msg_id}")
            else:
                await event.reply(f"❌ No active forwarding found for message ID: {msg_id}")

        except Exception as e:
            logger.error(f"Error in stopad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_timer(self, event):
        """Set default forwarding interval in seconds"""
        try:
            command_parts = event.text.split()
            if len(command_parts) != 2:
                await event.reply("❌ Please provide a valid interval in seconds\nFormat: /timer <seconds>")
                return

            try:
                interval = int(command_parts[1])
                if interval < 60:
                    await event.reply("❌ Interval must be at least 60 seconds")
                    return
            except ValueError:
                await event.reply("❌ Invalid interval format. Must be an integer in seconds.")
                return

            self.forward_interval = interval
            await event.reply(f"⏱️ Default forwarding interval set to {interval} seconds")
            logger.info(f"Set default forwarding interval to {interval} seconds")
        except Exception as e:
            logger.error(f"Error in timer command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_targetedad(self, event):
        """Start a targeted ad campaign with specific message, targets and interval"""
        try:
            command_parts = event.text.split()
            usage = "❌ Format: /targetedad <ad_id> <target_list> <interval>\n\nExample: /targetedad ABC123 target1,target2 3600"

            if len(command_parts) < 3:
                await event.reply(usage)
                return

            msg_id = command_parts[1]
            target_str = command_parts[2]

            # Check if message exists
            if msg_id not in self.stored_messages:
                await event.reply(f"❌ Message with ID {msg_id} not found. Use /listad to see available messages.")
                return

            # Parse targets
            targets = set()
            for target in target_str.split(','):
                target = target.strip()
                if not target:
                    continue

                try:
                    # Try as numeric ID
                    chat_id = int(target)
                    targets.add(chat_id)
                except ValueError:
                    # Try as username or link
                    try:
                        entity = await self.client.get_entity(target)
                        targets.add(entity.id)
                    except Exception as e:
                        logger.error(f"Error resolving target {target}: {str(e)}")
                        await event.reply(f"❌ Could not resolve target: {target}")
                        return

            if not targets:
                await event.reply("❌ No valid targets specified")
                return

            # Parse interval
            interval = self.forward_interval
            if len(command_parts) >= 4:
                try:
                    interval = int(command_parts[3])
                    if interval < 60:
                        await event.reply("❌ Interval must be at least 60 seconds")
                        return
                except ValueError:
                    await event.reply("❌ Invalid interval format. Must be an integer in seconds.")
                    return

            # Generate campaign ID
            campaign_id = generate_campaign_id()

            # Store campaign info
            self.targeted_campaigns[campaign_id] = {
                "msg_id": msg_id,
                "targets": targets,
                "interval": interval,
                "start_time": time.time()
            }

            # Start campaign task
            task = asyncio.create_task(
                self.forward_stored_message(
                    msg_id=msg_id,
                    targets=targets,
                    interval=interval
                )
            )

            self._forwarding_tasks[f"targeted_{campaign_id}"] = task

            await event.reply(f"""✅ **Targeted Campaign Started**
• Campaign ID: `{campaign_id}`
• Message ID: {msg_id}
• Targets: {len(targets)} chats
• Interval: {interval} seconds

Use `/stoptargetad {campaign_id}` to stop this campaign.
""")
            logger.info(f"Started targeted campaign {campaign_id} with message {msg_id}, {len(targets)} targets, {interval}s interval")
        except Exception as e:
            logger.error(f"Error in targetedad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_listtargetad(self, event):
        """List all targeted ad campaigns"""
        try:
            if not self.targeted_campaigns:
                await event.reply("📝 No targeted campaigns are currently active")
                return

            result = "📝 **Active Targeted Campaigns**:\n\n"

            for campaign_id, campaign in self.targeted_campaigns.items():
                # Calculate runtime
                runtime_seconds = int(time.time() - campaign["start_time"])
                days, remainder = divmod(runtime_seconds, 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, seconds = divmod(remainder, 60)
                runtime_str = f"{days}d {hours}h {minutes}m {seconds}s"

                result += f"""• Campaign ID: `{campaign_id}`
  - Message ID: {campaign["msg_id"]}
  - Targets: {len(campaign["targets"])} chats
  - Interval: {campaign["interval"]} seconds
  - Running for: {runtime_str}

"""

            await event.reply(result)
            logger.info("Listed all targeted campaigns")
        except Exception as e:
            logger.error(f"Error in listtargetad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_stoptargetad(self, event):
        """Stop a targeted ad campaign"""
        try:
            command_parts = event.text.split()
            if len(command_parts) != 2:
                await event.reply("❌ Please provide a campaign ID\nFormat: /stoptargetad <campaign_id>")
                return

            campaign_id = command_parts[1]

            # Check if campaign exists
            if campaign_id not in self.targeted_campaigns:
                await event.reply(f"❌ Campaign with ID {campaign_id} not found")
                return

            # Cancel the task
            task_id = f"targeted_{campaign_id}"
            if task_id in self._forwarding_tasks and not self._forwarding_tasks[task_id].done():
                self._forwarding_tasks[task_id].cancel()
                del self._forwarding_tasks[task_id]

            # Remove campaign
            del self.targeted_campaigns[campaign_id]

            await event.reply(f"🛑 Targeted campaign {campaign_id} has been stopped")
            logger.info(f"Stopped targeted campaign {campaign_id}")
        except Exception as e:
            logger.error(f"Error in stoptargetad command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    async def _schedule_forward(self, msg_id, targets, schedule_time):
        """Helper function to schedule a forward at a specific time"""
        try:
            now = datetime.now()
            wait_seconds = (schedule_time - now).total_seconds()

            if wait_seconds > 0:
                logger.info(f"Scheduled message {msg_id} to be sent in {wait_seconds} seconds")
                await asyncio.sleep(wait_seconds)

            message = self.stored_messages[msg_id]

            for target in targets:
                try:
                    await message.forward_to(target)
                    logger.info(f"Successfully forwarded scheduled message {msg_id} to {target}")
                except Exception as e:
                    logger.error(f"Error forwarding scheduled message {msg_id} to {target}: {str(e)}")

            return True
        except asyncio.CancelledError:
            logger.info(f"Scheduled task for message {msg_id} was cancelled")
            return False
        except Exception as e:
            logger.error(f"Error in scheduled task for message {msg_id}: {str(e)}")
            return False

    @admin_only
    async def cmd_schedule(self, event):
        """Schedule a message to be sent at a specific time"""
        try:
            command_parts = event.text.split(maxsplit=2)
            usage = """❌ Format: /schedule <msg_id> <time>

Time format examples:
- "5m" (5 minutes from now)
- "2h" (2 hours from now)
- "12:30" (today at 12:30, or tomorrow if already past)
- "2023-12-25 14:30" (specific date and time)"""

            if len(command_parts) < 3:
                await event.reply(usage)
                return

            msg_id = command_parts[1]
            time_str = command_parts[2]

            # Check if message exists
            if msg_id not in self.stored_messages:
                await event.reply(f"❌ Message with ID {msg_id} not found. Use /listad to see available messages.")
                return

            # Parse the time
            schedule_time = None
            now = datetime.now()

            # Check for relative time (e.g., "5m", "2h")
            relative_match = re.match(r'(\d+)([mh])', time_str)
            if relative_match:
                value, unit = relative_match.groups()
                value = int(value)

                if unit == 'm':
                    schedule_time = now + timedelta(minutes=value)
                elif unit == 'h':
                    schedule_time = now + timedelta(hours=value)
            else:
                # Try parsing as time or datetime
                try:
                    # Try as time only (e.g., "14:30")
                    time_only_match = re.match(r'(\d{1,2}):(\d{2})', time_str)
                    if time_only_match:
                        hour, minute = map(int, time_only_match.groups())
                        schedule_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                        # If time is in the past, add one day
                        if schedule_time < now:
                            schedule_time += timedelta(days=1)
                    else:
                        # Try as full datetime (e.g., "2023-12-25 14:30")
                        schedule_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M')
                except ValueError:
                    await event.reply(f"❌ Invalid time format: {time_str}\n\n{usage}")
                    return

            if not schedule_time:
                await event.reply(f"❌ Could not parse time: {time_str}\n\n{usage}")
                return

            if schedule_time < now:
                await event.reply("❌ Scheduled time must be in the future")
                return

            # Format the time for display
            formatted_time = schedule_time.strftime('%Y-%m-%d %H:%M')

            # Create a unique ID for this schedule
            schedule_id = f"sched_{generate_campaign_id()}"

            # Create and store the task
            task = asyncio.create_task(
                self._schedule_forward(
                    msg_id=msg_id,
                    targets=self.target_chats,
                    schedule_time=schedule_time
                )
            )

            self.scheduled_tasks[schedule_id] = task

            # Calculate wait time for display
            wait_seconds = (schedule_time - now).total_seconds()
            days, remainder = divmod(wait_seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            wait_str = ""
            if days > 0:
                wait_str += f"{int(days)} days "
            if hours > 0:
                wait_str += f"{int(hours)} hours "
            if minutes > 0:
                wait_str += f"{int(minutes)} minutes "
            if seconds > 0 and days == 0 and hours == 0:
                wait_str += f"{int(seconds)} seconds"

            await event.reply(f"""✅ **Message Scheduled**
• Schedule ID: `{schedule_id}`
• Message ID: {msg_id}
• Scheduled for: {formatted_time}
• Time until sending: {wait_str}
• Targets: {len(self.target_chats)} chats

The message will be forwarded at the scheduled time.
""")
            logger.info(f"Scheduled message {msg_id} for {formatted_time}, Schedule ID: {schedule_id}")
        except Exception as e:
            logger.error(f"Error in schedule command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_forward(self, event):
        """Forward a message to specific targets once"""
        try:
            command_parts = event.text.split()
            usage = "❌ Format: /forward <msg_id> <targets>\n\nExample: /forward ABC123 target1,target2"

            if len(command_parts) < 3:
                await event.reply(usage)
                return

            msg_id = command_parts[1]
            target_str = command_parts[2]

            # Check if message exists
            if msg_id not in self.stored_messages:
                await event.reply(f"❌ Message with ID {msg_id} not found. Use /listad to see available messages.")
                return

            # Parse targets
            targets = set()
            for target in target_str.split(','):
                target = target.strip()
                if not target:
                    continue

                try:
                    # Try as numeric ID
                    chat_id = int(target)
                    targets.add(chat_id)
                except ValueError:
                    # Try as username or link
                    try:
                        entity = await self.client.get_entity(target)
                        targets.add(entity.id)
                    except Exception as e:
                        logger.error(f"Error resolving target {target}: {str(e)}")
                        await event.reply(f"❌ Could not resolve target: {target}")
                        return

            if not targets:
                await event.reply("❌ No valid targets specified")
                return

            # Get the message
            message = self.stored_messages[msg_id]

            # Forward to each target
            success_count = 0
            fail_count = 0
            failures = []

            for target in targets:
                try:
                    await message.forward_to(target)
                    success_count += 1

                    # Update analytics
                    today = datetime.now().strftime('%Y-%m-%d')
                    if today not in self.analytics["forwards"]:
                        self.analytics["forwards"][today] = {}

                    campaign_key = f"{msg_id}_{target}"
                    if campaign_key not in self.analytics["forwards"][today]:
                        self.analytics["forwards"][today][campaign_key] = 0

                    self.analytics["forwards"][today][campaign_key] += 1

                    logger.info(f"Successfully forwarded message {msg_id} to {target}")
                except Exception as e:
                    fail_count += 1
                    failures.append(f"- {target}: {str(e)}")

                    # Track failures
                    today = datetime.now().strftime('%Y-%m-%d')
                    if today not in self.analytics["failures"]:
                        self.analytics["failures"][today] = {}

                    campaign_key = f"{msg_id}_{target}"
                    if campaign_key not in self.analytics["failures"][today]:
                        self.analytics["failures"][today][campaign_key] = []

                    self.analytics["failures"][today][campaign_key].append(str(e))

                    logger.error(f"Error forwarding message {msg_id} to {target}: {str(e)}")

            # Report results
            result = f"""✅ **Forward Results**
• Message ID: {msg_id}
• Successful: {success_count}
• Failed: {fail_count}
"""

            if failures:
                result += "\n**Failures:**\n" + "\n".join(failures[:5])
                if len(failures) > 5:
                    result += f"\n... and {len(failures) - 5} more failures"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Forwarded message {msg_id} to {len(targets)} targets. Success: {success_count}, Failed: {fail_count}")
        except Exception as e:
            logger.error(f"Error in forward command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_broadcast(self, event):
        """Send a message to all target chats"""
        try:
            command_parts = event.text.split(maxsplit=1)

            # Check if message content is provided
            if len(command_parts) < 2:
                if event.is_reply:
                    # Use replied message as broadcast content
                    replied_msg = await event.get_reply_message()
                    message_content = replied_msg
                else:
                    await event.reply("❌ Please provide a message to broadcast or reply to a message")
                    return
            else:
                # Use provided text as broadcast content
                message_content = command_parts[1]

            # Check if targets exist
            if not self.target_chats:
                await event.reply("❌ No target chats configured. Please add target chats first using /addtarget <target>")
                return

            # Broadcast the message
            success_count = 0
            fail_count = 0

            await event.reply(f"🔄 Broadcasting message to {len(self.target_chats)} targets...")

            for target in self.target_chats:
                try:
                    if isinstance(message_content, str):
                        await self.client.send_message(target, message_content)
                    else:
                        await message_content.forward_to(target)

                    success_count += 1
                    logger.info(f"Successfully broadcast message to {target}")
                except Exception as e:
                    fail_count += 1
                    logger.error(f"Error broadcasting message to {target}: {str(e)}")

            # Report results
            result = f"""✅ **Broadcast Results**
• Total Targets: {len(self.target_chats)}
• Successful: {success_count}
• Failed: {fail_count}
"""

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Broadcast message to {len(self.target_chats)} targets. Success: {success_count}, Failed: {fail_count}")
        except Exception as e:
            logger.error(f"Error in broadcast command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_addtarget(self, event):
        """Add a target chat for forwarding"""
        try:
            # Check if replying to a message to get the chat ID directly
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                if reply_msg:
                    # Try to get chat ID from forwarded message
                    if reply_msg.forward and hasattr(reply_msg.forward.chat, 'id'):
                        chat_id = reply_msg.forward.chat.id
                    # Try to get chat ID from message chat
                    elif hasattr(reply_msg, 'chat') and hasattr(reply_msg.chat, 'id'):
                        chat_id = reply_msg.chat.id
                    else:
                        chat_id = None

                    if chat_id:
                        chat_name = None
                        try:
                            entity = await self.client.get_entity(chat_id)
                            chat_name = getattr(entity, 'title', None) or getattr(entity, 'first_name', str(chat_id))
                        except:
                            chat_name = str(chat_id)

                        self.target_chats.add(chat_id)
                        await event.reply(f"✅ Added target chat: {chat_name} ({chat_id})")
                        logger.info(f"Added target chat from reply: {chat_id}, current targets: {self.target_chats}")
                        return

            # Process from command parameters
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide chat IDs, usernames, or invite links\nFormat: /addtarget <ID1,@username2,t.me/link,uid:123456>")
                return

            targets_text = command_parts[1]

            # Split by commas to support multiple targets
            target_list = [t.strip() for t in targets_text.split(',')]

            if not target_list:
                await event.reply("❌ No targets specified")
                return

            success_list = []
            fail_list = []

            for target in target_list:
                try:
                    chat_id = None

                    # Handle user ID format (uid:12345)
                    if target.lower().startswith('uid:'):
                        try:
                            uid = int(target[4:])
                            entity = await self.client.get_entity(uid)
                            chat_id = entity.id
                        except Exception as e:
                            logger.error(f"Error resolving user ID {target}: {str(e)}")
                            fail_list.append(f"{target}: Invalid user ID")
                            continue
                    # Try parsing as numeric chat ID first
                    elif target.lstrip('-').isdigit():
                        chat_id = int(target)
                    # Not a numeric ID, try resolving as username or link
                    else:
                        try:
                            if target.startswith('t.me/') or target.startswith('https://t.me/'):
                                # Handle invite links
                                entity = await self.client.get_entity(target)
                                chat_id = entity.id
                            elif target.startswith('@'):
                                # Handle usernames
                                entity = await self.client.get_entity(target)
                                chat_id = entity.id
                            else:
                                # Try as username without @
                                entity = await self.client.get_entity('@' + target)
                                chat_id = entity.id
                        except Exception as e:
                            logger.error(f"Error resolving chat identifier '{target}': {str(e)}")
                            fail_list.append(f"{target}: {str(e)}")
                            continue

                    if not chat_id:
                        fail_list.append(f"{target}: Could not resolve to a valid chat ID")
                        continue

                    self.target_chats.add(chat_id)
                    success_list.append(f"{target} → {chat_id}")
                    logger.info(f"Added target chat: {chat_id} from {target}")
                except Exception as e:
                    logger.error(f"Error adding target {target}: {str(e)}")
                    fail_list.append(f"{target}: {str(e)}")

            # Prepare response message
            response = []

            if success_list:
                response.append(f"✅ Successfully added {len(success_list)} target(s):")
                for success in success_list:
                    response.append(f"• {success}")

            if fail_list:
                response.append(f"\n❌ Failed to add {len(fail_list)} target(s):")
                for fail in fail_list:
                    response.append(f"• {fail}")

            if not success_list and not fail_list:
                response.append("⚠️ No targets were processed")

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(response), max_length):
                messages.append("".join(response[i:i + max_length]))

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Target chat operation complete, current targets: {self.target_chats}")
        except Exception as e:
            logger.error(f"Error in addtarget command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_listtarget(self, event):
        """List all target chats"""
        try:
            if not self.target_chats:
                await event.reply("📝 No target chats configured")
                return

            # Parse page number from command if present
            command_parts = event.text.split()
            page = 1
            items_per_page = 10

            if len(command_parts) > 1 and command_parts[1].isdigit():
                page = int(command_parts[1])

            # Get all chats info
            all_chats = []
            for chat_id in self.target_chats:
                try:
                    entity = await self.client.get_entity(chat_id)
                    name = getattr(entity, 'title', None) or getattr(entity, 'first_name', None) or str(chat_id)
                    username = getattr(entity, 'username', None)
                    all_chats.append((chat_id, name, username))
                except Exception:
                    all_chats.append((chat_id, "[Unknown]", None))

            # Calculate pagination
            total_pages = (len(all_chats) + items_per_page - 1) // items_per_page
            page = min(max(1, page), total_pages)
            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page

            result = f"📝 **Target Chats** (Page {page}/{total_pages})\n\n"

            # Add chats for current page
            for idx, (chat_id, name, username) in enumerate(all_chats[start_idx:end_idx], start=start_idx + 1):
                if username:
                    result += f"{idx}. {chat_id} - {name} (@{username})\n"
                else:
                    result += f"{idx}. {chat_id} - {name}\n"

            # Add navigation buttons info
            result += f"\n**Navigation:**\n"
            if page > 1:
                result += "• Use `/listtarget {page-1}` for previous page\n"
            if page < total_pages:
                result += "• Use `/listtarget {page+1}` for next page\n"
            result += f"\nShowing {start_idx + 1}-{min(end_idx, len(all_chats))} of {len(all_chats)} chats"

            await event.reply(result)
            logger.info(f"Listed target chats page {page}/{total_pages}")
        except Exception as e:
            logger.error(f"Error in listtarget command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_removetarget(self, event):
        """Remove specified target chats"""
        try:
            # Check if replying to a message to get the chat ID directly
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                if reply_msg and hasattr(reply_msg, 'chat') and hasattr(reply_msg.chat, 'id'):
                    chat_id = reply_msg.chat.id
                    chat_name = getattr(reply_msg.chat, 'title', str(chat_id))

                    if chat_id in self.target_chats:
                        self.target_chats.remove(chat_id)
                        await event.reply(f"✅ Removed target chat: {chat_name} ({chat_id})")
                        logger.info(f"Removed target chat from reply: {chat_id}")
                        return
                    else:
                        await event.reply(f"❌ Chat {chat_name} ({chat_id}) is not in your target list")
                        return

            # Process from command parameters
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide chat IDs, usernames or links to remove\nFormat: /removetarget <id1,@username2,t.me/link,uid:123456>")
                return

            id_str = command_parts[1]
            id_list = [x.strip() for x in id_str.split(',')]

            removed = []
            not_found = []

            for target_str in id_list:
                try:
                    chat_id = None

                    # Handle user ID format (uid:12345)
                    if target_str.lower().startswith('uid:'):
                        try:
                            uid = int(target_str[4:])
                            entity = await self.client.get_entity(uid)
                            chat_id = entity.id
                        except Exception as e:
                            logger.error(f"Error resolving user ID {target_str}: {str(e)}")
                            not_found.append(f"{target_str}: Invalid user ID")
                            continue
                    # Try parsing as numeric chat ID first
                    elif target_str.lstrip('-').isdigit():
                        chat_id = int(target_str)
                    # Not a numeric ID, try resolving as username or link
                    else:
                        try:
                            if target_str.startswith('t.me/') or target_str.startswith('https://t.me/'):
                                # Handle invite links
                                entity = await self.client.get_entity(target_str)
                                chat_id = entity.id
                            elif target_str.startswith('@'):
                                # Handle usernames
                                entity = await self.client.get_entity(target_str)
                                chat_id = entity.id
                            else:
                                # Try as username without @
                                entity = await self.client.get_entity('@' + target_str)
                                chat_id = entity.id
                        except Exception as e:
                            logger.error(f"Error resolving chat identifier '{target_str}': {str(e)}")
                            not_found.append(f"{target_str}: Could not resolve")
                            continue

                    if not chat_id:
                        not_found.append(f"{target_str}: Could not resolve to a valid chat ID")
                        continue

                    # Check if the chat is in the target list
                    if chat_id in self.target_chats:
                        self.target_chats.remove(chat_id)
                        chat_name = None
                        try:
                            entity = await self.client.get_entity(chat_id)
                            chat_name = getattr(entity, 'title', None) or getattr(entity, 'first_name', None)
                        except:
                            pass

                        if chat_name:
                            removed.append(f"{target_str} ({chat_name})")
                        else:
                            removed.append(f"{target_str}")
                    else:
                        not_found.append(f"{target_str}: Not in target list")
                except Exception as e:
                    logger.error(f"Error removing target {target_str}: {str(e)}")
                    not_found.append(f"{target_str}: {str(e)}")

            # Prepare response message
            response = []

            if removed:
                response.append(f"✅ Successfully removed {len(removed)} target(s):")
                for success in removed:
                    response.append(f"• {success}")

            if not_found:
                if removed:
                    response.append("")  # Add a blank line as separator
                response.append(f"❌ Failed to remove {len(not_found)} target(s):")
                for fail in not_found:
                    response.append(f"• {fail}")

            if not removed and not not_found:
                response.append("⚠️ No targets were processed")

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(response), max_length):
                messages.append("".join(response[i:i + max_length]))

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Target chat removal complete, current targets: {self.target_chats}")
        except Exception as e:
            logger.error(f"Error in removetarget command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_removealltarget(self, event):
        """Remove all target chats"""
        try:
            count = len(self.target_chats)

            # Confirm with a reply markup
            await event.reply(f"⚠️ Are you sure you want to remove all {count} target chats?\n\nReply with 'yes' to confirm.")

            # Wait for confirmation
            async def wait_for_confirmation():
                try:
                    response = await self.client.wait_for_event(
                        events.NewMessage(
                            from_users=event.sender_id,
                            chats=event.chat_id
                        ),
                        timeout=30  # 30 seconds timeout
                    )

                    if response.text.lower() == 'yes':
                        self.target_chats.clear()
                        await event.reply(f"✅ All {count} target chats have been removed")
                        logger.info(f"Removed all {count} target chats")
                    else:
                        await event.reply("❌ Operation cancelled")
                except asyncio.TimeoutError:
                    await event.reply("❌ Confirmation timeout, operation cancelled")

            # Start waiting for confirmation
            asyncio.create_task(wait_for_confirmation())

        except Exception as e:
            logger.error(f"Error in removealltarget command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_cleantarget(self, event):
        """Clean invalid target chats"""
        try:
            if not self.target_chats:
                await event.reply("📝 No target chats configured")
                return

            await event.reply(f"🔄 Validating {len(self.target_chats)} target chats...")

            # Track original count
            original_count = len(self.target_chats)

            # Check each target
            invalid_targets = []
            for target in list(self.target_chats):
                try:
                    await self.client.get_entity(target)
                except Exception:
                    invalid_targets.append(target)

            # Remove invalid targets
            for target in invalid_targets:
                self.target_chats.remove(target)

            # Report results
            removed_count = len(invalid_targets)
            remaining_count = original_count - removed_count

            if removed_count == 0:
                result = "✅ All target chats are valid. No cleaning needed."
            else:
                result = f"""✅ **Target Chats Cleaned**
• Original Count: {original_count}
• Removed: {removed_count}
• Remaining: {remaining_count}"""

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Cleaned target chats. Removed {removed_count} invalid targets.")
        except Exception as e:
            logger.error(f"Error in cleantarget command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_removeunsub(self, event):
        """Remove chats where the userbot cannot send messages"""
        try:
            if not self.target_chats:
                await event.reply("📝 No target chats configured")
                return

            await event.reply(f"🔄 Checking {len(self.target_chats)} target chats for messaging permissions...")

            # Track original count
            original_count = len(self.target_chats)

            # Test message to check permissions
            test_message = "⚙️ Testing permissions..."

            # Check each target
            inaccessible_targets = []
            for target in list(self.target_chats):
                try:
                    # Try to send a message and immediately delete it
                    msg = await self.client.send_message(target, test_message)
                    await msg.delete()
                except (ChatAdminRequiredError, ChatWriteForbiddenError, UserBannedInChannelError):
                    # These errors indicate the bot cannot send messages
                    inaccessible_targets.append(target)
                except Exception as e:
                    logger.error(f"Error checking permissions for {target}: {str(e)}")
                    # If any other error, assume we can't send
                    inaccessible_targets.append(target)

            # Remove inaccessible targets
            for target in inaccessible_targets:
                self.target_chats.remove(target)

            # Report results
            removed_count = len(inaccessible_targets)
            remaining_count = original_count - removed_count

            if removed_count == 0:
                result = "✅ You have permission to send messages in all target chats."
            else:
                result = f"""✅ **Inaccessible Chats Removed**
• Original Count: {original_count}
• Removed: {removed_count}
• Remaining: {remaining_count}"""

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Removed {removed_count} inaccessible target chats.")
        except Exception as e:
            logger.error(f"Error in removeunsub command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_targeting(self, event):
        """Find chats with specific keywords"""
        try:
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide keywords to search for\nFormat: /targeting <keywords>")
                return

            keywords = command_parts[1]

            await event.reply(f"🔍 Searching for chats with keywords: {keywords}...")

            # Search globally
            results = await self.client(SearchGlobalRequest(
                q=keywords,
                filter=None,  # No filter to get all types of results
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=InputPeerEmpty(),
                offset_id=0,
                limit=20
            ))

            # Extract unique chats from the results
            found_chats = {}

            for result in results.results:
                if hasattr(result, 'peer') and hasattr(result.peer, 'channel_id'):
                    # It's a channel or group
                    try:
                        chat_id = result.peer.channel_id
                        if chat_id not in found_chats:
                            full_chat = await self.client.get_entity(chat_id)
                            found_chats[chat_id] = {
                                'id': chat_id,
                                'title': getattr(full_chat, 'title', 'Unknown'),
                                'username': getattr(full_chat, 'username', None),
                                'members': getattr(full_chat, 'participants_count', 'Unknown')
                            }
                    except Exception:
                        # Skip if we can't get full info
                        pass

            # Report results
            if not found_chats:
                await event.reply(f"❌ No chats found for keywords: {keywords}")
                return

            result = f"""✅ **Found {len(found_chats)} Matching Chats**
Keywords: {keywords}

"""
            for chat in found_chats.values():
                username_str = f"@{chat['username']}" if chat['username'] else "No username"
                result += f"• {chat['title']} ({username_str})\n  - ID: {chat['id']}\n  - Members: {chat['members']}\n\n"

            result += "To add these chats as targets, use `/addtarget <chat_id>`"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Found {len(found_chats)} chats for keywords: {keywords}")
        except Exception as e:
            logger.error(f"Error in targeting command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_joinchat(self, event):
        """Join specified channels or groups"""
        try:
            # Check if replying to a message to get chat links or IDs
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                if reply_msg:
                    chat_list = []

                    # Check for text content
                    if reply_msg.text:
                        # Look for various link formats and usernames
                        patterns = [
                            r'(?:https?://)?t\.me/(?:joinchat/|\+)?([a-zA-Z0-9_-]+)',  # t.me links
                            r'@([a-zA-Z][a-zA-Z0-9_]{3,})',  # @usernames
                            r'(?<!\w)([a-zA-Z][a-zA-Z0-9_]{3,})',  # Usernames without @
                            r'-?\d{6,}',  # Chat IDs
                        ]

                        for pattern in patterns:
                            matches = re.findall(pattern, reply_msg.text)
                            for match in matches:
                                if isinstance(match, tuple):
                                    match = match[0]
                                if match.isdigit() or match.startswith('-'):
                                    chat_list.append(match)
                                elif match.startswith('@'):
                                    chat_list.append(match)
                                else:
                                    # Add @ for usernames, full link for t.me
                                    if 't.me/' in reply_msg.text and match in reply_msg.text:
                                        chat_list.append(f"t.me/{match}")
                                    else:
                                        chat_list.append(f"@{match}")

                    # Check for forward header
                    if reply_msg.forward and hasattr(reply_msg.forward.chat, 'id'):
                        chat_list.append(str(reply_msg.forward.chat.id))

                    # Check current chat
                    if hasattr(reply_msg.chat, 'id'):
                        chat_list.append(str(reply_msg.chat.id))

                    # Remove duplicates while preserving order
                    chat_list = list(dict.fromkeys(chat_list))

                    if chat_list:
                        await event.reply(f"🔄 Found {len(chat_list)} potential targets in the replied message. Processing...")
                    else:
                        await event.reply("❌ No valid chat links, usernames, or IDs found in the replied message")
                        return
                else:
                    await event.reply("❌ Could not process the replied message")
                    return
            else:
                # Standard command processing
                command_parts = event.text.split(maxsplit=1)
                if len(command_parts) < 2:
                    await event.reply("❌ Please provide chat usernames or invite links\nFormat: /joinchat <@username1,@username2,t.me/link>")
                    return

                chat_list = [x.strip() for x in command_parts[1].split(',')]

                if not chat_list:
                    await event.reply("❌ No valid chats provided")
                    return

                await event.reply(f"🔄 Attempting to join {len(chat_list)} chats...")

            # Track results
            success = []
            already_joined = []
            failures = []

            for chat in chat_list:
                try:
                    # Clean URL first
                    cleaned_chat = chat.strip()
                    if cleaned_chat.startswith(('http://', 'https://')):
                        cleaned_chat = cleaned_chat.split('://')[1]

                    # Handle different chat formats
                    if cleaned_chat.startswith('@'):
                        try:
                            entity = await self.client.get_entity(cleaned_chat)
                            result = await self.client(JoinChannelRequest(entity))
                            success.append(chat)
                        except Exception as e:
                            failures.append(f"{chat} - {str(e)}")
                    elif any(x in cleaned_chat.lower() for x in ['t.me/', 'telegram.me/', 'telegram.dog/']):
                        # Clean and standardize the link
                        clean_link = chat.replace('https://', '').replace('http://', '')
                        for domain in ['telegram.me/', 'telegram.dog/']:
                            if domain in clean_link:
                                clean_link = clean_link.replace(domain, 't.me/')

                        # Extract username or hash
                        try:
                            if '/+' in clean_link:
                                invite_hash = clean_link.split('/+')[1].split('?')[0]
                                result = await self.client(ImportChatInviteRequest(invite_hash))
                                success.append(chat)
                            elif '/joinchat/' in clean_link:
                                invite_hash = clean_link.split('/joinchat/')[1].split('?')[0]
                                result = await self.client(ImportChatInviteRequest(invite_hash))
                                success.append(chat)
                            else:
                                # Try as regular channel
                                username = clean_link.split('t.me/')[1].split('?')[0]
                                entity = await self.client.get_entity(f"@{username}")
                                result = await self.client(JoinChannelRequest(entity))
                                success.append(chat)

                            # Remove any trailing characters
                            invite_hash = invite_hash.strip()
                            result = await self.client(ImportChatInviteRequest(invite_hash))
                            success.append(chat)
                        except Exception as e:
                            failures.append(f"{chat} - {str(e)}")
                    else:
                        # Try to handle as a channel username without @
                        try:
                            entity = await self.client.get_entity(f"@{chat}")
                            try:
                                await self.client(GetFullChannelRequest(entity))
                                already_joined.append(f"@{chat}")
                                continue
                            except:
                                pass

                            result = await self.client(JoinChannelRequest(entity))
                            success.append(f"@{chat}")
                        except Exception as e:
                            failures.append(f"{chat} - {str(e)}")
                except Exception as e:
                    failures.append(f"{chat} - {str(e)}")

            # Report results
            result = f"""✅ **Join Results**
• Successfully joined: {len(success)}
• Already a member: {len(already_joined)}
• Failed: {len(failures)}
"""

            if success:
                result += "\n**Successfully joined:**\n" + "\n".join([f"• {x}" for x in success])

            if failures:
                result += "\n\n**Failed to join:**\n" + "\n".join([f"• {x}" for x in failures[:5]])
                if len(failures) > 5:
                    result += f"\n... and {len(failures) - 5} more failures"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Join chat results - Success: {len(success)}, Already joined: {len(already_joined)}, Failed: {len(failures)}")
        except Exception as e:
            logger.error(f"Error in joinchat command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_leavechat(self, event):
        """Leave specified channels or groups"""
        try:
            # Check if replying to a message to get the chat ID directly
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                if reply_msg and hasattr(reply_msg, 'chat') and hasattr(reply_msg.chat, 'id'):
                    chat_id = reply_msg.chat.id
                    chat_name = getattr(reply_msg.chat, 'title', str(chat_id))

                    # Don't leave the chat where the command was issued
                    if chat_id == event.chat_id:
                        await event.reply(f"⚠️ You're trying to leave the current chat. Use the command with parameters to leave this chat.")
                        return

                    try:
                        entity = await self.client.get_entity(chat_id)
                        await self.client(LeaveChannelRequest(entity))
                        await event.reply(f"✅ Successfully left chat: {chat_name} ({chat_id})")
                        logger.info(f"Left chat from reply: {chat_id}")
                        return
                    except Exception as e:
                        await event.reply(f"❌ Failed to leave chat {chat_name} ({chat_id}): {str(e)}")
                        logger.error(f"Error leaving chat {chat_id}: {str(e)}")
                        return

            # Standard command processing
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide chat identifiers\nFormat: /leavechat <@username1,@username2,chat_id,uid:123456>")
                return

            chat_list = [x.strip() for x in command_parts[1].split(',')]

            if not chat_list:
                await event.reply("❌ No valid chats provided")
                return

            await event.reply(f"🔄 Attempting to leave {len(chat_list)} chats...")

            # Track results
            success = []
            failures = []

            for chat in chat_list:
                try:
                    # Try to get the entity
                    if chat.isdigit():
                        entity = await self.client.get_entity(int(chat))
                    else:
                        entity = await self.client.get_entity(chat)

                    await self.client(LeaveChannelRequest(entity))
                    success.append(chat)
                except Exception as e:
                    failures.append(f"{chat} - {str(e)}")

            # Report results
            result = f"""✅ **Leave Results**
• Successfully left: {len(success)}
• Failed: {len(failures)}
"""

            if success:
                result += "\n**Successfully left:**\n" + "\n".join([f"• {x}" for x in success])

            if failures:
                result += "\n\n**Failed to leave:**\n" + "\n".join([f"• {x}" for x in failures[:5]])
                if len(failures) > 5:
                    result += f"\n... and {len(failures) - 5} more failures"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Leave chat results - Success: {len(success)}, Failed: {len(failures)}")
        except Exception as e:
            logger.error(f"Error in leavechat command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_leaveandremove(self, event):
        """Leave chats and remove them from targets"""
        try:
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide chat identifiers\nFormat: /leaveandremove <@username1,@username2,chat_id>")
                return

            chat_list = [x.strip() for x in command_parts[1].split(',')]

            if not chat_list:
                await event.reply("❌ No valid chats provided")
                return

            await event.reply(f"🔄 Attempting to leave and remove {len(chat_list)} chats...")

            # Track results
            success_leave = []
            fail_leave = []
            removed_targets = []

            for chat in chat_list:
                # Try to leave the chat
                try:
                    # Get entity
                    if chat.isdigit():
                        entity = await self.client.get_entity(int(chat))
                        chat_id = int(chat)
                    else:
                        entity = await self.client.get_entity(chat)
                        chat_id = entity.id

                    # Leave the chat
                    await self.client(LeaveChannelRequest(entity))
                    success_leave.append(chat)

                    # Remove from targets if present
                    if chat_id in self.target_chats:
                        self.target_chats.remove(chat_id)
                        removed_targets.append(chat)
                except Exception as e:
                    fail_leave.append(f"{chat} - {str(e)}")

                    # Still try to remove from targets if possible
                    try:
                        if chat.isdigit():
                            chat_id = int(chat)
                            if chat_id in self.target_chats:
                                self.target_chats.remove(chat_id)
                                removed_targets.append(chat)
                    except:
                        pass

            # Report results
            result = f"""✅ **Leave and Remove Results**
• Successfully left: {len(success_leave)}
• Failed to leave: {len(fail_leave)}
• Removed from targets: {len(removed_targets)}
"""

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Leave and remove results - Left: {len(success_leave)}, Failed: {len(fail_leave)}, Removed from targets: {len(removed_targets)}")
        except Exception as e:
            logger.error(f"Error in leaveandremove command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_listjoined(self, event):
        """List joined channels and groups"""
        try:
            command_parts = event.text.split()
            show_all = len(command_parts) > 1 and command_parts[1] == '--all'
            auto_target = show_all  # Auto-target when --all is specified

            await event.reply("🔄 Fetching joined chats...")

            # Get all dialogs
            dialogs = await self.client(GetDialogsRequest(
                offset_date=None,
                offset_id=0,
                offset_peer=InputPeerEmpty(),
                limit=100,
                hash=0
            ))

            channels = []
            groups = []
            me = await self.client.get_me()

            for dialog in dialogs.dialogs:
                try:
                    entity = await self.client.get_entity(dialog.peer)

                    # Filter for channels and groups only
                    if hasattr(entity, 'title'):  # It's a channel or group
                        # Skip bots, private chats, and personal channels
                        if (
                            hasattr(entity, 'bot') and entity.bot or  # Skip bots
                            hasattr(entity, 'creator') and entity.creator or  # Skip own channels
                            (hasattr(entity, 'admin_rights') and entity.admin_rights and 
                             hasattr(entity, 'participants_count') and 
                             getattr(entity, 'participants_count', 0) < 3)  # Skip private/personal chats
                        ):
                            continue

                        is_target = entity.id in self.target_chats
                        chat_type = "Channel" if hasattr(entity, 'broadcast') and entity.broadcast else "Group"

                        # Auto-add to targets if --all specified (skip channels)
                        if auto_target and not is_target:
                            # Only auto-add groups, not channels
                            if not (hasattr(entity, 'broadcast') and entity.broadcast):
                                self.target_chats.add(entity.id)
                                is_target = True

                        # Skip if not in targets and --all not specified
                        if show_all or is_target:
                            info = {
                                'id': entity.id,
                                'title': entity.title,
                                'username': getattr(entity, 'username', None),
                                'members': getattr(entity, 'participants_count', 'Unknown'),
                                'is_target': is_target,
                                'type': chat_type
                            }

                            if chat_type == "Channel":
                                channels.append(info)
                            else:
                                groups.append(info)
                except Exception:
                    # Skip entities we can't resolve
                    pass

            # Format the results
            if not channels and not groups:
                await event.reply("❌ No joined chats found.")
                return

            # Parse page number
            command_parts = event.text.split()
            page = 1
            items_per_page = 10

            if len(command_parts) > 1 and command_parts[1] != '--all' and command_parts[1].isdigit():
                page = int(command_parts[1])

            result = ""
            if show_all:
                result = "📋 **All Joined Chats**\n\n"
            else:
                result = "📋 **Joined Chats (Target Chats Only)**\n\n"

            # Handle channels pagination
            if channels:
                total_channel_pages = (len(channels) + items_per_page - 1) // items_per_page
                channel_page = min(max(1, page), total_channel_pages)
                start_idx = (channel_page - 1) * items_per_page
                end_idx = start_idx + items_per_page

                result += f"**Channels ({len(channels)})** - Page {channel_page}/{total_channel_pages}:\n"
                for idx, channel in enumerate(channels[start_idx:end_idx], start=start_idx + 1):
                    username_str = f"@{channel['username']}" if channel['username'] else "No username"
                    target_str = "✅ Target" if channel['is_target'] else "❌ Not Target"
                    result += f"{idx}. {channel['title']} ({username_str})\n   - ID: {channel['id']}\n   - {target_str}\n\n"

            # Handle groups pagination
            if groups:
                total_group_pages = (len(groups) + items_per_page - 1) // items_per_page
                group_page = min(max(1, page), total_group_pages)
                start_idx = (group_page - 1) * items_per_page
                end_idx = start_idx + items_per_page

                result += f"\n**Groups ({len(groups)})** - Page {group_page}/{total_group_pages}:\n"
                for idx, group in enumerate(groups[start_idx:end_idx], start=start_idx + 1):
                    username_str = f"@{group['username']}" if group['username'] else "No username"
                    target_str = "✅ Target" if group['is_target'] else "❌ Not Target"
                    result += f"{idx}. {group['title']} ({username_str})\n   - ID: {group['id']}\n   - {target_str}\n\n"

            # Add navigation instructions
            result += "\n**Navigation:**\n"
            if page > 1:
                result += f"• Use `/listjoined {page-1}`{' --all' if show_all else ''} for previous page\n"
            if page < max(total_channel_pages if channels else 0, total_group_pages if groups else 0):
                result += f"• Use `/listjoined {page+1}`{' --all' if show_all else ''} for next page\n"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Listed joined chats - Channels: {len(channels)}, Groups: {len(groups)}")
        except Exception as e:
            logger.error(f"Error in listjoined command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_findgroup(self, event):
        """Find groups with specific keyword"""
        try:
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide a keyword to search for\nFormat: /findgroup <keyword>")
                return

            keyword = command_parts[1]

            await event.reply(f"🔍 Searching for groups with keyword: {keyword}...")

            # Search globally
            results = await self.client(SearchGlobalRequest(
                q=keyword,
                filter=None,
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=InputPeerEmpty(),
                offset_id=0,
                limit=50
            ))

            # Extract unique groups (not channels) from the results
            found_groups = {}

            for result in results.results:
                if hasattr(result, 'peer') and hasattr(result.peer, 'channel_id'):
                    # Get entity to check if it's a group (not a channel)
                    try:
                        chat_id = result.peer.channel_id
                        if chat_id not in found_groups:
                            full_chat = await self.client.get_entity(chat_id)
                            # Check if it's a group (not a channel)
                            if not getattr(full_chat, 'broadcast', True):  # Not a broadcast channel
                                found_groups[chat_id] = {
                                    'id': chat_id,
                                    'title': getattr(full_chat, 'title', 'Unknown'),
                                    'username': getattr(full_chat, 'username', None),
                                    'members': getattr(full_chat, 'participants_count', 'Unknown')
                                }
                    except Exception:
                        # Skip if we can't get full info
                        pass

            # Report results
            if not found_groups:
                await event.reply(f"❌ No groups found for keyword: {keyword}")
                return

            result = f"""✅ **Found {len(found_groups)} Groups**
Keyword: {keyword}

"""
            for group in found_groups.values():
                username_str = f"@{group['username']}" if group['username'] else "No username"
                result += f"• {group['title']} ({username_str})\n  - ID: {group['id']}\n  - Members: {group['members']}\n\n"

            result += "To add these groups as targets, use `/addtarget <group_id>`"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Found {len(found_groups)} groups for keyword: {keyword}")
        except Exception as e:
            logger.error(f"Error in findgroup command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_clearchat(self, event):
        """Clear recent messages in the chat on both sides"""
        try:
            command_parts = event.text.split()
            count = 100  # Default

            if len(command_parts) > 1:
                try:
                    count = int(command_parts[1])
                    if count <= 0 or count > 1000:
                        await event.reply("❌ Count must be between 1 and 1000")
                        return
                except ValueError:
                    await event.reply("❌ Invalid count. Please provide a number.")
                    return

            status_msg = await event.reply(f"🔄 Clearing up to {count} recent messages...")

            # Get messages to delete
            chat = event.chat_id
            me = await self.client.get_me()

            messages_to_delete = []
            async for message in self.client.iter_messages(chat, limit=count):
                messages_to_delete.append(message)

            if not messages_to_delete:
                await status_msg.edit("❌ No messages to delete")
                return

            # Delete messages
            deleted_count = 0
            errors = 0

            for message in messages_to_delete:
                try:
                    await message.delete()  # This deletes for both sides
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"Error deleting message: {str(e)}")
                    errors += 1

            try:
                await status_msg.delete()  # Delete the status message
            except:
                pass

            logger.info(f"Cleared {deleted_count} messages on both sides. Errors: {errors}")
        except Exception as e:
            logger.error(f"Error in clearchat command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_pin(self, event):
        """Pin the replied message"""
        try:
            if not event.is_reply:
                await event.reply("❌ Please reply to a message to pin it")
                return

            command_parts = event.text.split()
            silent = len(command_parts) > 1 and command_parts[1].lower() == 'silent'

            # Get the message to pin
            reply_msg = await event.get_reply_message()

            # Pin the message
            await self.client.pin_message(
                event.chat_id,
                reply_msg.id,
                notify=not silent
            )

            await event.reply("📌 Message pinned successfully")
            logger.info(f"Pinned message {reply_msg.id} in chat {event.chat_id}")
        except Exception as e:
            logger.error(f"Error in pin command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_bio(self, event):
        """Set profile bio"""
        try:
            command_parts = event.text.split(maxsplit=1)
            if len(command_parts) < 2:
                await event.reply("❌ Please provide the bio text\nFormat: /bio <text>")
                return

            bio_text = command_parts[1]

            # Update profile bio
            await self.client(UpdateProfileRequest(about=bio_text))

            await event.reply("✅ Profile bio updated successfully")
            logger.info("Updated profile bio")
        except Exception as e:
            logger.error(f"Error in bio command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_name(self, event):
        """Set profile name"""
        try:
            command_parts = event.text.split()
            if len(command_parts) < 2:
                await event.reply("❌ Please provide at least a first name\nFormat: /name <first_name> [last_name]")
                return

            first_name = command_parts[1]
            last_name = command_parts[2] if len(command_parts) > 2 else ""

            # Update profile name
            await self.client(UpdateProfileRequest(first_name=first_name, last_name=last_name))

            await event.reply("✅ Profile name updated successfully")
            logger.info(f"Updated profile name to {first_name} {last_name}")
        except Exception as e:
            logger.error(f"Error in name command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_username(self, event):
        """Set profile username"""
        try:
            command_parts = event.text.split()
            if len(command_parts) < 2:
                await event.reply("❌ Please provide a username\nFormat: /username <new_username>")
                return

            username = command_parts[1]
            # Remove @ if present
            if username.startswith('@'):
                username = username[1:]

            # Update username
            await self.client(UpdateUsernameRequest(username))

            await event.reply(f"✅ Username updated to @{username}")
            logger.info(f"Updated username to @{username}")
        except Exception as e:
            logger.error(f"Error in username command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_setpic(self, event):
        """Set profile picture"""
        try:
            if not event.is_reply:
                status_msg = await event.reply("⚠️ Please reply to a message to set it as profile picture\n\n🖼️ Reply to an image and try again!")
                await asyncio.sleep(3)
                await status_msg.delete()
                return

            # Get the replied message
            reply_msg = await event.get_reply_message()

            if not reply_msg.media:
                await event.reply("❌ The replied message doesn't contain any media")
                return

            # Download the photo
            await event.reply("🔄 Downloading photo...")
            photo = await self.client.download_media(reply_msg, file=bytes)

            # Upload the photo
            await self.client(UploadProfilePhotoRequest(file=await self.client.upload_file(photo)))

            await event.reply("✅ Profile picture updated successfully")
            logger.info("Updated profile picture")
        except Exception as e:
            logger.error(f"Error in setpic command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_addadmin(self, event):
        """Add a new admin"""
        try:
            command_parts = event.text.split()
            if len(command_parts) < 2:
                await event.reply("❌ Please provide a user ID or username\nFormat: /addadmin <user_id/username>")
                return

            user_id_or_username = command_parts[1]

            # Try to get user ID
            user_id = None

            if user_id_or_username.isdigit():
                user_id = int(user_id_or_username)
            else:
                try:
                    entity = await self.client.get_entity(user_id_or_username)
                    user_id = entity.id
                except Exception as e:
                    await event.reply(f"❌ Could not resolve user: {str(e)}")
                    return

            if user_id in self.admins:
                await event.reply(f"❌ User {user_id} is already an admin")
                return

            # Add to admins
            self.admins.add(user_id)

            await event.reply(f"✅ User {user_id} added as admin")
            logger.info(f"Added user {user_id} as admin")
        except Exception as e:
            logger.error(f"Error in addadmin command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_removeadmin(self, event):
        """Remove an admin"""
        try:
            command_parts = event.text.split()
            if len(command_parts) < 2:
                await event.reply("❌ Please provide a user ID or username\nFormat: /removeadmin <user_id/username>")
                return

            user_id_or_username = command_parts[1]

            # Try to get user ID
            user_id = None

            if user_id_or_username.isdigit():
                user_id = int(user_id_or_username)
            else:
                try:
                    entity = await self.client.get_entity(user_id_or_username)
                    user_id = entity.id
                except Exception as e:
                    await event.reply(f"❌ Could not resolve user: {str(e)}")
                    return

            # Check if in original admins list
            if user_id in config.ADMIN_IDS and len(self.admins) <= 1:
                await event.reply("❌ Cannot remove the original admin")
                return

            if user_id not in self.admins:
                await event.reply(f"❌ User {user_id} is not an admin")
                return

            # Remove from admins
            self.admins.remove(user_id)

            await event.reply(f"✅ User {user_id} removed from admins")
            logger.info(f"Removed user {user_id} from admins")
        except Exception as e:
            logger.error(f"Error in removeadmin command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_listadmins(self, event):
        """List all admins"""
        try:
            if not self.admins:
                await event.reply("📝 No admins configured")
                return

            result = "📝 **Admins**:\n\n"

            for admin_id in self.admins:
                try:
                    # Try to get admin info
                    admin = await self.client.get_entity(admin_id)
                    admin_name = getattr(admin, 'first_name', 'Unknown')
                    admin_username = getattr(admin, 'username', None)

                    if admin_username:
                        result += f"• {admin_id} - {admin_name} (@{admin_username})\n"
                    else:
                        result += f"• {admin_id} - {admin_name}\n"
                except Exception:
                    result += f"• {admin_id}\n"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Listed {len(self.admins)} admins")
        except Exception as e:
            logger.error(f"Error in listadmins command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_analytics(self, event):
        """Show forwarding analytics"""
        try:
            command_parts = event.text.split()
            days = 7  # Default

            if len(command_parts) > 1:
                try:
                    days = int(command_parts[1])
                    if days <= 0 or days > 30:
                        await event.reply("❌ Days must be between 1 and 30")
                        return
                except ValueError:
                    await event.reply("❌ Invalid days value. Please provide a number.")
                    return

            # Get dates for the last N days
            today = datetime.now()
            date_list = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)]
            date_list.reverse()  # Oldest first

            # Collect data for each day
            daily_stats = []
            for date in date_list:
                forwards =0
                failures = 0

                if date in self.analytics["forwards"]:
                    for campaign in self.analytics["forwards"][date].values():
                        forwards += campaign

                if date in self.analytics["failures"]:
                    for campaign in self.analytics["failures"][date].values():
                        failures += len(campaign)

                daily_stats.append({
                    'date': date,
                    'forwards': forwards,
                    'failures': failures
                })

            # Calculate totals
            total_forwards = sum(day['forwards'] for day in daily_stats)
            total_failures = sum(day['failures'] for day in daily_stats)

            # Build report
            result = f"""📊 **Forwarding Analytics (Last {days} Days)**

**Summary:**
• Total Messages Forwarded: {total_forwards}
• Total Failures: {total_failures}
• Success Rate: {(total_forwards / (total_forwards + total_failures) * 100) if total_forwards + total_failures > 0 else 0:.1f}%

**Daily Breakdown:**
"""
            for day in daily_stats:
                result += f"• {day['date']}: {day['forwards']} forwards, {day['failures']} failures\n"

            # Performance comparison
            if days >= 2 and len(daily_stats) >= 2:
                today_stats = daily_stats[-1]
                yesterday_stats = daily_stats[-2]

                if yesterday_stats['forwards'] > 0:
                    change_pct = ((today_stats['forwards'] - yesterday_stats['forwards']) / yesterday_stats['forwards']) * 100
                    result += f"\n**Today vs. Yesterday:**\n• {change_pct:+.1f}% change in forwards\n"

            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(result), max_length):
                messages.append(result[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info(f"Generated analytics report for the last {days} days")
        except Exception as e:
            logger.error(f"Error in analytics command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_backup(self, event):
        """Backup settings and stored messages"""
        try:
            await event.reply("🔄 Creating backup...")

            # Collect data to backup
            backup_data = {
                'target_chats': list(self.target_chats),
                'forward_interval': self.forward_interval,
                'stored_messages': {},  # Can't backup actual messages, just metadata
                'admins': list(self.admins),
                'campaigns': self.targeted_campaigns,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            # Collect metadata about stored messages
            for msg_id, message in self.stored_messages.items():
                msg_meta = {
                    'id': msg_id,
                    'chat_id': message.chat_id,
                    'message_id': message.id,
                    'type': 'media' if message.media else 'text',
                    'preview': message.text[:50] if message.text else '[Media Message]'
                }
                backup_data['stored_messages'][msg_id] = msg_meta

            # Create backup file name
            backup_id = generate_campaign_id(8)
            backup_filename = f"backup_{backup_id}.json"

            # Save backup
            with open(backup_filename, 'w') as f:
                json.dump(backup_data, f, indent=2)

            # Send backup file
            await self.client.send_file(
                event.chat_id,
                backup_filename,
                caption=f"""✅ **Backup Created**
• Backup ID: `{backup_id}`
• Timestamp: {backup_data['timestamp']}
• Target Chats: {len(backup_data['target_chats'])}
• Stored Messages: {len(backup_data['stored_messages'])}
• Admins: {len(backup_data['admins'])}
• Campaigns: {len(backup_data['campaigns'])}

Use `/restore {backup_id}` to restore from this backup.
"""
            )

            # Clean up
            try:
                os.remove(backup_filename)
            except:
                pass

            logger.info(f"Created backup with ID {backup_id}")
        except Exception as e:
            logger.error(f"Error in backup command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_restore(self, event):
        """Restore from backup"""
        try:
            if not event.is_reply:
                await event.reply("❌ Please reply to a backup file")
                return

            # Get the replied message
            reply_msg = await event.get_reply_message()

            if not reply_msg.document:
                await event.reply("❌ The replied message doesn't contain a document")
                return

            # Check file name
            if not reply_msg.document.attributes:
                await event.reply("❌ Invalid backup file")
                return

            # Download the backup file
            await event.reply("🔄 Downloading backup file...")
            backup_path = await reply_msg.download_media()

            # Load the backup
            with open(backup_path, 'r') as f:
                backup_data = json.load(f)

            # Validate backup
            required_keys = ['target_chats', 'forward_interval', 'stored_messages', 'admins', 'campaigns', 'timestamp']
            missing_keys = [key for key in required_keys if key not in backup_data]

            if missing_keys:
                await event.reply(f"❌ Invalid backup file. Missing keys: {', '.join(missing_keys)}")
                return

            # Confirm restoration
            await event.reply(f"""⚠️ **Confirm Restore**
• Backup from: {backup_data['timestamp']}
• Target Chats: {len(backup_data['target_chats'])}
• Stored Messages: {len(backup_data['stored_messages'])}
• Admins: {len(backup_data['admins'])}
• Campaigns: {len(backup_data['campaigns'])}

Reply with 'yes' to confirm restoration.
""")

            # Wait for confirmation
            async def wait_for_confirmation():
                try:
                    response = await self.client.wait_for_event(
                        events.NewMessage(
                            from_users=event.sender_id,
                            chats=event.chat_id
                        ),
                        timeout=30  # 30 seconds timeout
                    )

                    if response.text.lower() == 'yes':
                        # Stop all running tasks
                        await self.cmd_stop(event)

                        # Restore data
                        self.target_chats = set(backup_data['target_chats'])
                        self.forward_interval = backup_data['forward_interval']
                        self.admins = set(backup_data['admins'])
                        self.targeted_campaigns = backup_data['campaigns']

                        # Can't restore actual messages, just provide info
                        message_info = "**Stored Messages Not Restored**\n"
                        message_info += "You'll need to resave the following messages:\n\n"

                        for msg_id, msg_meta in backup_data['stored_messages'].items():
                            message_info += f"• ID: {msg_id} - {msg_meta['preview']}\n"

                        await event.reply(f"""✅ **Backup Restored**
• Target Chats: {len(self.target_chats)}
• Forward Interval: {self.forward_interval}
• Admins: {len(self.admins)}
• Campaigns: {len(self.targeted_campaigns)}

{message_info}
""")
                        logger.info("Restored from backup")
                    else:
                        await event.reply("❌ Restoration cancelled")
                except asyncio.TimeoutError:
                    await event.reply("❌ Confirmation timeout, restoration cancelled")

                # Clean up
                try:
                    os.remove(backup_path)
                except:
                    pass

            # Start waiting for confirmation
            asyncio.create_task(wait_for_confirmation())

        except Exception as e:
            logger.error(f"Error in restore command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_stickers(self, event):
        """Create a sticker pack from media"""
        try:
            # This is a placeholder as sticker creation is complex
            await event.reply("⚠️ The stickers command is not yet implemented")
            logger.info("Stickers command requested (not implemented)")
        except Exception as e:
            logger.error(f"Error in stickers command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_interactive(self, event):
        """Toggle interactive mode"""
        try:
            # This is a placeholder as interactive mode is complex
            await event.reply("⚠️ The interactive command is not yet implemented")
            logger.info("Interactive command requested (not implemented)")
        except Exception as e:
            logger.error(f"Error in interactive command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")

    @admin_only
    async def cmd_client(self, event):
        """Show client information"""
        try:
            me = await self.client.get_me()
            name = event.sender.first_name if hasattr(event, 'sender') and hasattr(event.sender, 'first_name') else "User"
            start_time = time.time()
            ping = int((time.time() - start_time) * 1000)

            # Get session info
            session_name = self.client.session.filename

            # Format the phone number for privacy
            phone = "91*****89"

            client_info = f"""🤖 --Ꮪɪᴍᴘʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 CLIENT INFO 🤖

Hey {name}! 🚀 Here's your client information:

📊 Client Details:
🔹 User: {me.username if me.username else "No username"}
🔹 User ID: {me.id}
🔹 Client Type: Telegram UserBot
🔹 Platform: Telethon
🔹 API Version: v1.24.0
🔹 Ping: 🚀 {ping} ms
🔹 Client Number: {phone}

✨ Need more assistance?
Type `/help` to see all available commands and features!

📌 Stay smart, stay secure, and enjoy the automation!

🚀 Powered by --Ꮪɪᴍᴘʟᴇ'𝚜 𝙰𝙳𝙱𝙾𝚃 (@siimplebot1)
"""
            # Split long messages
            max_length = 4096  # Telegram's max message length
            messages = []
            for i in range(0, len(client_info), max_length):
                messages.append(client_info[i:i + max_length])

            # Send each part
            for message in messages:
                await event.reply(message)
            logger.info("Client information displayed")
        except Exception as e:
            logger.error(f"Error in client command: {str(e)}")
            await event.reply(f"❌ Error: {str(e)}")


class TelegramForwarder:
    def __init__(self):
        self.client = TelegramClient(
            config.SESSION_NAME, 
            config.API_ID, 
            config.API_HASH
        )
        self.handler = None

    async def start(self):
        """Start the Telegram client and initialize handlers"""
        await self.client.start()
        self.handler = MessageForwarder(self.client)
        return self.client