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
from vreview.webhook_guard import WebhookResolutionError, WebhookURLError, resolve_webhook_target

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 10.0


def _post_once(webhook_url: str, body: bytes, headers: dict[str, str]) -> int:
    """One POST to the webhook; returns the HTTP status code. The host is resolved and checked here, and the request
    goes to the address that passed the check (never to a second DNS answer). Redirects are not followed and the
    response body is not read.

    Raises WebhookURLError when the target is not acceptable (WebhookResolutionError when it did not resolve) and
    the last connection error when no validated address accepts a connection.
    """
    target = resolve_webhook_target(webhook_url)
    request_headers = {**headers, "Host": target.host_header}
    last_error: Exception | None = None
    # trust_env=False: an HTTP(S)_PROXY from the environment would receive the pinned IP instead of the target.
    with httpx.Client(timeout=REQUEST_TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
        for address in target.addresses:
            try:
                # TLS is verified against the original host name: the IP is only where the socket connects.
                with client.stream(
                    "POST",
                    target.request_url(address),
                    content=body,
                    headers=request_headers,
                    extensions={"sni_hostname": target.host},
                ) as resp:
                    return resp.status_code
            except httpx.TransportError as exc:
                last_error = exc
    raise last_error  # type: ignore[misc]  # addresses is never empty, so a transport error was recorded


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
                status_code = _post_once(webhook_url, body, headers)
            except WebhookResolutionError as exc:
                logger.warning(
                    "Webhook host did not resolve for run %s (attempt %d): %s", run_id, attempt + 1, exc
                )
            except WebhookURLError as exc:
                # The URL was acceptable when the run was created; its host no longer is (DNS changed).
                logger.warning("Webhook for run %s not sent, target rejected: %s", run_id, exc)
                return
            except Exception as exc:
                logger.warning(
                    "Webhook delivery error for run %s (attempt %d): %s",
                    run_id,
                    attempt + 1,
                    exc,
                )
            else:
                if 200 <= status_code < 300:
                    logger.info(
                        "Webhook delivered successfully for run %s to %s (attempt %d)",
                        run_id,
                        webhook_url,
                        attempt + 1,
                    )
                    return
                if 300 <= status_code < 400:
                    logger.warning(
                        "Webhook for run %s answered %d; redirects are not followed, giving up",
                        run_id,
                        status_code,
                    )
                    return
                logger.warning(
                    "Webhook delivery got status %d for run %s (attempt %d)",
                    status_code,
                    run_id,
                    attempt + 1,
                )
            if attempt < 2:
                time.sleep(2 ** attempt)
    except Exception as exc:
        logger.error("Unexpected error delivering webhook for run %s: %s", run_id, exc)
