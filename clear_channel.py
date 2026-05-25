"""
Deletes all messages in the Telegram channel defined by BRIDGE_CHAT_ID.

Strategy:
  1. Send a probe message to discover the current max message ID.
  2. Delete the probe message.
  3. Read a checkpoint file to find the lowest ID that might still exist.
     Telegram message IDs are monotonically increasing and never reused, so
     any ID <= last checkpoint has already been deleted.
  4. Iterate IDs [checkpoint+1, max_id] in batches of 100 using deleteMessages.
     deleteMessages silently ignores IDs that don't exist.
  5. Save the new max_id as the checkpoint for next time.

Requires BOT_A_TOKEN and BRIDGE_CHAT_ID in .env (or environment).
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("BOT_A_TOKEN") or os.environ.get("BOT_B_TOKEN")
CHAT_ID = os.environ.get("BRIDGE_CHAT_ID")

if not TOKEN or not CHAT_ID:
    sys.exit("ERROR: BOT_A_TOKEN (or BOT_B_TOKEN) and BRIDGE_CHAT_ID must be set in .env")

BASE = f"https://api.telegram.org/bot{TOKEN}"
BATCH = 100          # deleteMessages accepts up to 100 IDs per call
DELETE_DELAY = 0.05  # seconds between batches to stay under rate limits

# One checkpoint file per chat so the script works with multiple channels.
CHECKPOINT_PATH = f".cache/clear_channel_{CHAT_ID}.checkpoint"


def load_checkpoint() -> int:
    try:
        return int(open(CHECKPOINT_PATH).read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_checkpoint(max_id: int) -> None:
    os.makedirs(os.path.dirname(CHECKPOINT_PATH), exist_ok=True)
    with open(CHECKPOINT_PATH, "w") as f:
        f.write(str(max_id))


async def tg(client: httpx.AsyncClient, method: str, **params) -> dict:
    r = await client.post(f"{BASE}/{method}", json=params, timeout=15)
    r.raise_for_status()
    return r.json()


async def get_max_message_id(client: httpx.AsyncClient) -> int:
    resp = await tg(client, "sendMessage", chat_id=CHAT_ID, text="probe")
    msg_id: int = resp["result"]["message_id"]
    await tg(client, "deleteMessage", chat_id=CHAT_ID, message_id=msg_id)
    return msg_id - 1  # exclude the probe itself


async def delete_range(client: httpx.AsyncClient, min_id: int, max_id: int) -> None:
    ids = list(range(max_id, min_id - 1, -1))
    total = len(ids)
    deleted = 0

    for start in range(0, total, BATCH):
        batch = ids[start : start + BATCH]
        try:
            await tg(client, "deleteMessages", chat_id=CHAT_ID, message_ids=batch)
            deleted += len(batch)
        except Exception:
            for mid in batch:
                try:
                    await tg(client, "deleteMessage", chat_id=CHAT_ID, message_id=mid)
                    deleted += 1
                except Exception:
                    pass
        done = start + len(batch)
        print(f"\r  {done}/{total}  deleted~{deleted}", end="", flush=True)
        if start + BATCH < total:
            await asyncio.sleep(DELETE_DELAY)

    print()


async def main() -> None:
    print(f"Channel: {CHAT_ID}")
    async with httpx.AsyncClient() as client:
        print("Probing current max message ID…")
        max_id = await get_max_message_id(client)

        checkpoint = load_checkpoint()
        min_id = checkpoint + 1

        if max_id < min_id:
            print(f"Nothing to delete (checkpoint={checkpoint}, max_id={max_id}).")
            return

        span = max_id - checkpoint
        skipped = checkpoint
        print(f"Checkpoint: {checkpoint}  →  scanning IDs {min_id}–{max_id} ({span} IDs, skipping {skipped} already-cleared)")
        await delete_range(client, min_id, max_id)

    save_checkpoint(max_id)
    print(f"Done. Checkpoint saved: {max_id}")


if __name__ == "__main__":
    asyncio.run(main())
