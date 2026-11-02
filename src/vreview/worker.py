"""Worker entrypoint for vreview workflows."""
from __future__ import annotations

import logging
import os

from vreview.hatchet import get_hatchet
from vreview.workflows.review import pair_workflow, review_workflow, track_features_workflow

logger = logging.getLogger(__name__)


def main() -> None:
    """Start the Hatchet worker registering all vreview workflows."""
    logging.basicConfig(level=logging.INFO)
    slots_env = os.environ.get("VREVIEW_WORKER_SLOTS", "4")
    try:
        slots = int(slots_env)
    except ValueError:
        slots = 4

    hatchet = get_hatchet()
    worker = hatchet.worker(
        name="vreview-worker",
        slots=slots,
        workflows=[review_workflow, track_features_workflow, pair_workflow],
    )
    logger.info("Starting vreview-worker with %d slots...", slots)
    worker.start()


if __name__ == "__main__":
    main()
