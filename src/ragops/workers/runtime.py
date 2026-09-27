"""Redis worker entrypoint for deployment environments."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ragops.config.settings import Settings
from ragops.persistence.database import open_engine
from ragops.workers.queue import RedisIngestionQueue
from ragops.workers.worker import IngestionWorker


def main() -> None:
    settings = Settings.from_env()
    settings.validate_async_configuration()
    if not settings.redis_url:
        raise RuntimeError("RAGOPS_REDIS_URL is required for worker runtime")
    queue = RedisIngestionQueue(settings.redis_url, settings.queue_name)
    engine = open_engine()
    while True:
        message = queue.dequeue(timeout=5)
        if message is None:
            continue
        with Session(engine) as session:
            worker = IngestionWorker(session)
            worker.process(message.job_id, message.tenant_id)
            session.commit()


if __name__ == "__main__":
    main()
