import asyncio

from dotenv import load_dotenv

load_dotenv()

from server.relay import serve  # noqa: E402


if __name__ == "__main__":
    asyncio.run(serve())
