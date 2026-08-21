import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

DEFAULT_QUEUE = "tenantforge:jobs"
DEFAULT_DEAD_LETTER_QUEUE = "tenantforge:jobs:dead"
MAX_ATTEMPTS = 3
RETRY_DELAYS = (1, 2, 4)
SENSITIVE_PAYLOAD_KEYS = {
    "api_key",
    "authorization",
    "password",
    "refresh_token",
    "token",
}


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if str(key).lower() in SENSITIVE_PAYLOAD_KEYS
            else _redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    type: str
    payload: dict[str, Any]
    attempt: int
    created_at: str

    @classmethod
    def create(cls, job_type: str, payload: dict[str, Any]) -> "Job":
        if not job_type:
            raise ValueError("job type is required")
        return cls(
            id=str(uuid.uuid4()),
            type=job_type,
            payload=payload,
            attempt=0,
            created_at=datetime.now(UTC).isoformat(),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Job":
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            raise ValueError("invalid job payload")
        return cls(
            id=str(data["id"]),
            type=str(data["type"]),
            payload=data["payload"],
            attempt=int(data["attempt"]),
            created_at=str(data["created_at"]),
        )

    def next_attempt(self) -> "Job":
        return replace(self, attempt=self.attempt + 1)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), default=str)


class RedisJobQueue:
    def __init__(
        self,
        redis: Redis,
        *,
        queue_name: str = DEFAULT_QUEUE,
        dead_letter_queue: str = DEFAULT_DEAD_LETTER_QUEUE,
        processing_queue: str | None = None,
    ) -> None:
        self.redis = redis
        self.queue_name = queue_name
        self.dead_letter_queue = dead_letter_queue
        self.processing_queue = processing_queue or f"{queue_name}:processing"

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        job: Job | None = None,
    ) -> Job:
        queued_job = job or Job.create(job_type, payload)
        await self.redis.lpush(self.queue_name, queued_job.to_json())
        return queued_job

    async def dequeue(self, *, timeout: int = 5) -> Job | None:
        raw_job = await self.redis.brpoplpush(
            self.queue_name,
            self.processing_queue,
            timeout=timeout,
        )
        if raw_job is None:
            return None
        try:
            return Job.from_json(raw_job)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            envelope = {
                "failed_at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
                "raw_sha256": hashlib.sha256(str(raw_job).encode()).hexdigest(),
            }
            async with self.redis.pipeline(transaction=True) as pipeline:
                pipeline.lrem(self.processing_queue, 1, raw_job)
                pipeline.lpush(
                    self.dead_letter_queue,
                    json.dumps(envelope, separators=(",", ":")),
                )
                await pipeline.execute()
            raise

    async def acknowledge(self, job: Job) -> None:
        await self.redis.lrem(self.processing_queue, 1, job.to_json())

    async def retry(self, job: Job) -> Job:
        retry_job = job.next_attempt()
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.lrem(self.processing_queue, 1, job.to_json())
            pipeline.lpush(self.queue_name, retry_job.to_json())
            await pipeline.execute()
        return retry_job

    async def move_to_dead_letter(self, job: Job, error: BaseException) -> None:
        safe_job = asdict(job)
        safe_job["payload"] = _redact_payload(job.payload)
        envelope = {
            "job": safe_job,
            "failed_at": datetime.now(UTC).isoformat(),
            "error_type": type(error).__name__,
        }
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.lrem(self.processing_queue, 1, job.to_json())
            pipeline.lpush(
                self.dead_letter_queue,
                json.dumps(envelope, separators=(",", ":"), default=str),
            )
            await pipeline.execute()

    async def recover_in_flight(self) -> int:
        recovered = 0
        while await self.redis.rpoplpush(self.processing_queue, self.queue_name) is not None:
            recovered += 1
        return recovered
