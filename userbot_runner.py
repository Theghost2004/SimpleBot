import asyncio
import logging
import sys
from userbot import TelegramForwarder

# Configure logging
logging.basicConfig(
    format=
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s - [%(filename)s:%(lineno)d]',
    level=logging.INFO,
    stream=sys.stdout)
logger = logging.getLogger(__name__)


async def main():
    """Main function to run the userbot"""
    try:
        # Create and start the client
        userbot = TelegramForwarder()
        await userbot.start()
        logger.info("Userbot started successfully")

        # Keep the bot running
        await userbot.client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Error in main function: {str(e)}")
        raise


if __name__ == "__main__":
    # Run the Telegram bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
