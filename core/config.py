import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    discord_token: str
    database_url: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN")
        db_url = os.getenv("DATABASE_URL")
        if not token:
            raise RuntimeError("DISCORD_TOKEN not set")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set")
        return cls(discord_token=token, database_url=db_url)


config = Config.from_env()
