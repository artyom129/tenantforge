from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests processed",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ("method", "route"),
)
BACKGROUND_JOBS = Counter(
    "background_jobs_total",
    "Background jobs processed",
    ("job_type", "outcome"),
)
BACKGROUND_JOB_FAILURES = Counter(
    "background_job_failures_total",
    "Background job execution failures",
    ("job_type",),
)
WEBHOOK_EVENTS = Counter(
    "webhook_events_total",
    "Billing webhook events received",
    ("event_type", "outcome"),
)

