"""Inspect exact historical campaign evidence; --apply repairs it without sending."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db.postgresql import async_session_factory  # noqa: E402
from app.services.campaign_delivery import reconcile  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true", help="Repair exact historical associations; does not send.")
    args = parser.parse_args()
    async with async_session_factory() as db:
        print(json.dumps(await reconcile(db, args.campaign_id, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
