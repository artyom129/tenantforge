import asyncio
from collections.abc import Awaitable, Callable

import structlog

from app.core.logging import configure_logging
from app.core.metrics import BACKGROUND_JOB_FAILURES, BACKGROUND_JOBS
from app.infrastructure.queue import MAX_ATTEMPTS, RETRY_DELAYS, RedisJobQueue
from app.infrastructure.redis import close_redis, get_redis
from app.workers.jobs import JobHandler, build_handlers

logger = structlog.get_logger()
Sleep = Callable[[float], Awaitable[None]]


class Worker:
    def __init__(
        self,
        queue: RedisJobQueue,
        handlers: dict[str, JobHandler],
        *,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        retry_delays: tuple[int, ...] = RETRY_DELAYS,
    ) -> None:
        if max_attempts < 1 or not retry_delays:
            raise ValueError("worker retry settings must be positive")
        self.queue = queue
        self.handlers = handlers
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.retry_delays = retry_delays

    async def run_once(self, *, timeout: int = 5) -> bool:
        job = await self.queue.dequeue(timeout=timeout)
        if job is None:
            return False

        handler = self.handlers.get(job.type)
        try:
            if handler is None:
                raise ValueError(f"unknown job type: {job.type}")
            await handler(job.payload)
        except Exception as exc:
            BACKGROUND_JOB_FAILURES.labels(job_type=job.type).inc()
            if job.attempt >= self.max_attempts:
                await self.queue.move_to_dead_letter(job, exc)
                BACKGROUND_JOBS.labels(job_type=job.type, outcome="dead_letter").inc()
                logger.error(
                    "background_job_dead_lettered",
                    job_id=job.id,
                    job_type=job.type,
                    attempt=job.attempt,
                    error_type=type(exc).__name__,
                )
                return True

            delay_index = min(job.attempt, len(self.retry_delays) - 1)
            delay = self.retry_delays[delay_index]
            logger.warning(
                "background_job_retrying",
                job_id=job.id,
                job_type=job.type,
                attempt=job.attempt,
                retry_in=delay,
                error_type=type(exc).__name__,
            )
            await self.sleep(delay)
            await self.queue.retry(job)
            BACKGROUND_JOBS.labels(job_type=job.type, outcome="retry").inc()
            return True

        BACKGROUND_JOBS.labels(job_type=job.type, outcome="success").inc()
        await self.queue.acknowledge(job)
        logger.info(
            "background_job_completed",
            job_id=job.id,
            job_type=job.type,
            attempt=job.attempt,
        )
        return True

    async def run_forever(self) -> None:
        recovered = await self.queue.recover_in_flight()
        logger.info("worker_started", queue=self.queue.queue_name, recovered_jobs=recovered)
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("worker_poll_failed", error_type=type(exc).__name__)
                await self.sleep(1)


async def main() -> None:
    configure_logging()
    worker = Worker(RedisJobQueue(get_redis()), build_handlers())
    try:
        await worker.run_forever()
    finally:
        await close_redis()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("worker_stopped")
