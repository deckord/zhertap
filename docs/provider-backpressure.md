# Provider backpressure

Outbound enrichment is admitted through a Redis-backed provider policy before any HTTP
request starts. Production is fail-closed: if Redis cannot answer within the configured
1–3 second timeout, the request is deferred and no provider call is made. The in-process
backend is for tests and local development only.

## Jerler policy

- Provider key: `jerler`
- Rate: 1 request/second, burst 2
- Maximum concurrent calls: 2
- Lease TTL: 45 seconds
- Jerler HTTP hard timeout: 15 seconds
- Circuit failures: network errors, HTTP 429, and HTTP 5xx only
- Validation, unsafe URL, unsupported content, response bounds, and parsing errors do not
  count as upstream circuit failures

Every allowed permit is finalized once: successful/upstream-failed calls use
`record_result`, while cancellation and local data errors release the lease without
changing circuit health. Lease expiry is the recovery guard for worker loss.

`JerlerEnrichmentDeferred.retry_after_seconds` is the Celery-facing reschedule contract.
Callers must use a bounded task countdown; they must not sleep or busy-wait in a worker.
Queue depth alarms must receive broker-observed depth rather than inferred counts.

## Environment and operational gate

`REDIS_URL` selects the shared Redis database and `APP_ENV=production` (or `prod`)
enables fail-closed behavior. Redis socket and connect timeouts are both 1.5 seconds by
default and must remain between 1 and 3 seconds.

The real-Lua integration test is opt-in:

```powershell
$env:PROVIDER_BACKPRESSURE_REDIS_URL = "redis://localhost:6379/15"
.\.venv\Scripts\python.exe -m pytest tests/test_provider_backpressure_redis_integration.py -q
```

It uses namespaced temporary keys and verifies rate/concurrency admission, circuit
open/half-open/close, lease expiry, and Redis metrics. Until this test passes against the
deployment Redis version, the integration must not be described as production-rolled-out.
