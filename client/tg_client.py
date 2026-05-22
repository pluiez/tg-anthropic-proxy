import os
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from shared.framing import parse_frame

_app: Optional[Application] = None
_on_frame: Optional[Callable[[dict], Awaitable[None]]] = None
_bridge_chat_id: Optional[int] = None


async def _handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text or msg.chat_id != _bridge_chat_id:
        return
    frame = parse_frame(msg.text)
    if frame is not None and _on_frame is not None:
        await _on_frame(frame)


async def start(on_frame: Callable[[dict], Awaitable[None]]) -> None:
    global _app, _on_frame, _bridge_chat_id
    _on_frame = on_frame
    _bridge_chat_id = int(os.environ["BRIDGE_CHAT_ID"])
    token = os.environ["BOT_A_TOKEN"]

    # Group rate is intentionally higher than Telegram's documented 1 msg/s/chat
    # for private groups — PTB's AIORateLimiter handles 429 backoff automatically
    # via max_retries, so we stay close to the real ceiling without flooding.
    _app = (
        Application.builder()
        .token(token)
        .rate_limiter(
            AIORateLimiter(
                overall_max_rate=30,
                overall_time_period=1,
                group_max_rate=3,
                group_time_period=1,
                max_retries=5,
            )
        )
        .build()
    )
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handler))
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def stop() -> None:
    if _app is None:
        return
    if _app.updater is not None:
        await _app.updater.stop()
    await _app.stop()
    await _app.shutdown()


async def send_frame(text: str) -> None:
    assert _app is not None and _bridge_chat_id is not None
    await _app.bot.send_message(
        chat_id=_bridge_chat_id,
        text=text,
        disable_notification=True,
    )
