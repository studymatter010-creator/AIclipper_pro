"""
AIClipper Huey Consumer Startup Script

Run the Huey task queue consumer:
    python -m backend.workers.consumer
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.utils.logging import setup_logging


def main() -> None:
    """Start the Huey consumer."""
    setup_logging()

    from backend.workers.tasks import huey
    from huey.consumer import Consumer

    consumer = Consumer(
        huey,
        workers=2,           # Number of worker threads
        periodic=True,       # Enable periodic tasks
        initial_delay=0.1,
        backoff=1.15,
        max_delay=10.0,
        scheduler_interval=1,
        # NOTE: 'verbose' was removed in Huey 2.5+ — drop it (verbosity is set
        # via loglevel/logging config on newer versions).
    )

    print("=" * 50)
    print("AIClipper Task Queue Consumer")
    print(f"Database: {huey.filename if hasattr(huey, 'filename') else 'N/A'}")
    print(f"Workers: 2")
    print("=" * 50)
    print("Waiting for tasks...\n")

    try:
        consumer.run()
    except KeyboardInterrupt:
        print("\nShutting down consumer...")
        consumer.stop()


if __name__ == "__main__":
    main()
