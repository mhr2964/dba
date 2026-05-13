import asyncio
from bot.client import DBABot
from core.config import config
from core.logging import setup_logging


async def main():
    setup_logging()
    async with DBABot() as bot:
        await bot.start(config.discord_token)


asyncio.run(main())
