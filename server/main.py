import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()

from server.relay import CcProxyUnavailable, serve  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Telegram Anthropic relay.")
    parser.add_argument(
        "--use-cc-proxy",
        action="store_true",
        help="send upstream Anthropic requests through the configured cc_proxy service",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(serve(use_cc_proxy=args.use_cc_proxy))
    except CcProxyUnavailable as exc:
        raise SystemExit(str(exc)) from None
