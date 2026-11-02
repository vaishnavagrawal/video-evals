"""Outgoing webhook delivery for vreview runs."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

from vreview.config import settings
from vreview.db.models import ReviewRow, RunRow
from vreview.db.session import get_sessionmaker

logger = logging.getLogger(__name__)


def deliver(run_id: str) -> None:
    """Delivers webhook notification for a run.

    Reads run's webhook_url from DB; POST JSON {run_id, status, score, review_url};
    header X-VReview-Signature: sha256=<hex hmac of body with settings().webhook_secret>;
    3 tries with backoff; never raises.
    """
    try:
        with get_sessionmaker()() as db:
            run = db.get(RunRow, run_id)
            if not run or not run.webhook_url:
                return

            webhook_url = run.webhook_url
            status = run.status

            review = db.get(ReviewRow, run_id)
            score = review.score if review else None

        review_url = f"{settings().public_base_url.rstrip('/')}/v1/reviews/{run_id}"
        payload = {
            "run_id": run_id,
            "status": status,
            "score": score,
            "review_url": review_url,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if settings().webhook_secret:
            sig = hmac.new(
                settings().webhook_secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            headers["X-VReview-Signature"] = f"sha256={sig}"

        for attempt in range(3):
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(webhook_url, content=body, headers=headers)
                    if resp.is_success:
                        logger.info(
                            "Webhook delivered successfully for run %s to %s (attempt %d)",
                            run_id,
                            webhook_url,
                            attempt + 1,
                        )
                        return
                    logger.warning(
                        "Webhook delivery got status %d for run %s (attempt %d)",
                        resp.status_code,
                        run_id,
                        attempt + 1,
                    )
            except Exception as exc:
                logger.warning(
                    "Webhook delivery error for run %s (attempt %d): %s",
                    run_id,
                    attempt + 1,
                    exc,
                )
            if attempt < 2:
                time.sleep(2 ** attempt)
    except Exception as exc:
        logger.error("Unexpected error delivering webhook for run %s: %s", run_id, exc)
