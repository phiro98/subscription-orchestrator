# Architecture: Distributed Subscription & Payment Orchestrator

## 1. High-Level System Overview

The Distributed Subscription & Payment Orchestrator serves as the mission-critical revenue engine for high-traffic digital platforms. In an environment processing millions of concurrent subscriber mutations and receiving asynchronous webhook callbacks from third-party payment gateways (e.g., Stripe, Adyen), race conditions or partial network failures immediately risk double-charging customers or causing revenue leakage.

```
                      +-----------------------------+
                      |       Client / Client Apps  |
                      +--------------+--------------+
                                     |
                         POST /v1/subscriptions/charge
                         (Header: Idempotency-Key)
                                     |
                                     v
+---------------------+      +-------+--------+      +---------------------+
| External Gateways   |      |  FastAPI App   | <--> |  Redis Cluster      |
| (Stripe, Adyen)     | ---> |  Orchestrator  |      |  - Distributed Lock |
+---------------------+      +-------+--------+      +---------------------+
   Webhooks (HMAC-SHA256)            |
                                     v
                             +-------+--------+
                             | PostgreSQL     |
                             | - ACID Storage |
                             | - Row Locks    |
                             | - Idempotency  |
                             +----------------+
```

---

## 2. Distributed Locking & Idempotency Strategy

### 2.1 The Two-Phase Idempotency Protocol

To provide strict exactly-once execution guarantees across distributed nodes without bottlenecking the database, the orchestrator employs a hybrid approach combining **atomic Redis distributed locks** with **PostgreSQL durable record storage**.

```
Client Request (Idempotency-Key: K, Payload: P)
  |
  +--> Compute SHA-256 Hash H = Hash(P)
  |
  +--> Check PostgreSQL idempotency_records WHERE idempotency_key = K
  |      |
  |      +--> Record exists AND record.hash != H
  |      |      --> Raise 422 Unprocessable Entity (Payload Mismatch)
  |      |
  |      +--> Record exists AND status == 'COMPLETED'
  |      |      --> Return Cached Response (HTTP 200/201, X-Cache-Lookup: HIT)
  |      |
  |      +--> Record exists AND status == 'STARTED' AND expires_at > NOW()
  |             --> Raise 409 Conflict (Request In Flight)
  |
  +--> Acquire Redis Distributed Lock:
  |      SET lock:idempotency:K <random_token> NX EX 15
  |      |
  |      +--> Lock Failed (Key exists in Redis)
  |             --> Raise 409 Conflict (Concurrent Mutation in Flight)
  |
  +--> Insert / Update DB IdempotencyRecord(status='STARTED', expires_at=NOW() + 15s)
  |
  +--> Execute Business Logic (Row lock on Subscription, FSM transition, Gateway charge)
  |
  +--> On Success:
  |      1. Update IdempotencyRecord(status='COMPLETED', response_code, response_body)
  |      2. Commit PostgreSQL Transaction
  |      3. Release Redis Lock via Lua Script (Matching Token)
  |
  +--> On Server Failure:
         1. Mark IdempotencyRecord(status='FAILED')
         2. Release Redis Lock
```

### 2.2 Redis Distributed Lock & TTL Trade-offs

1. **Atomic Acquisition**:
   The Redis lock uses `SET key token NX EX ttl`. The `token` is a cryptographically unique UUIDv4 per request worker. This ensures only one distributed worker holds the execution lease.

2. **Safe Atomic Release via Lua**:
   Releasing the lock is performed via a Lua script:
   ```lua
   if redis.call("get", KEYS[1]) == ARGV[1] then
       return redis.call("del", KEYS[1])
   else
       return 0
   end
   ```
   This prevents the **split-brain lock release problem**: if worker A experiences a GC pause or database network spike longer than the 15-second TTL, the lock naturally expires. When worker B acquires the lock, worker A cannot accidentally delete worker B's lock upon finally resuming.

3. **Lease Expiration (TTL) Sizing**:
   - **Too Short (< 5s)**: High risk of lock expiring while the payment gateway call is still settling, allowing concurrent retry to cause a double charge.
   - **Too Long (> 60s)**: If a worker process crashes mid-flight, subsequent client retries are blocked with 409 Conflict for an unnecessarily long period.
   - **Optimal Setting (15s)**: Balances payment gateway P99 latencies (typically 1.5s - 3s) with rapid self-healing if a worker terminates abruptly.

---

## 3. Webhook Delivery: Out-of-Order & Duplicate Protection

External payment gateways provide **at-least-once delivery**, which guarantees webhooks will arrive, but offers **no delivery order guarantees**. A network retry of `invoice.payment_failed` can arrive after a subsequent `invoice.payment_succeeded` was already processed. Blindly processing out-of-order webhooks causes catastrophic subscription state corruption (e.g. terminating paying subscribers).

### 3.1 Webhook Processing Lifecycle

```
Webhook Callback Received (Header: Stripe-Signature / X-Signature)
  |
  +--> Step 1: Cryptographic HMAC-SHA256 Signature Verification
  |      - Extract timestamp `t` and signature `v1`
  |      - Reject if abs(now - t) > 300s (Replay Attack Prevention)
  |      - Verify HMAC(secret, "${t}.${body}") == v1 via constant-time compare
  |
  +--> Step 2: Ingestion Deduplication Check
  |      - Query webhook_events WHERE gateway = G AND event_id = E
  |      - If found: Log duplicate, return 200 OK immediately (idempotent ACK)
  |
  +--> Step 3: Transactional Row-Level Locking
  |      - BEGIN DB Transaction
  |      - SELECT * FROM subscriptions WHERE id = :sub_id FOR UPDATE
  |
  +--> Step 4: Deterministic Event Timestamp Sequencing
  |      - Compare incoming event.created against subscription.last_event_timestamp
  |      |
  |      +--> event.created < subscription.last_event_timestamp:
  |      |      - Stale / out-of-order event detected!
  |      |      - Persist WebhookEvent with status 'IGNORED_OUT_OF_ORDER'
  |      |      - COMMIT Transaction
  |      |      - Return 200 OK (Gateway won't retry; state remains pristine)
  |      |
  |      +--> event.created >= subscription.last_event_timestamp:
  |             - Advance state machine: SubscriptionFSM.transition(...)
  |             - Update subscription.last_event_timestamp = event.created
  |             - Insert Transaction ledger entry
  |             - Persist WebhookEvent with status 'PROCESSED'
  |             - COMMIT Transaction (Releases row lock)
```

### 3.2 Monotonic Clock Ordering vs Version Vectors

Each gateway event carries a signed UNIX epoch timestamp `created`. The `subscriptions` table tracks `last_event_timestamp`. Because writes to a given subscription are guarded by `SELECT ... FOR UPDATE`, evaluation of `event.created < subscription.last_event_timestamp` is completely serialized and race-free.

---

## 4. Subscription Finite State Machine (FSM)

Subscriptions follow a deterministic lifecycle. Any jump outside the permitted transition graph raises an `IllegalStateTransitionError` (409 Conflict):

```
       +---------------------------------------------+
       |                                             |
       v                                             |
  [ PENDING ] ------------> [ ACTIVE ] <----------+  |
       |                         |    ^           |  |
       |                         |    |           |  |
       |                         v    |           |  |
       |                    [ PAST_DUE ]          |  |
       |                         |                |  |
       v                         v                |  |
  +-----------------------------------------------+  |
  |                [ CANCELED ]                      |
  |                 (Terminal)                       |
  +--------------------------------------------------+
```

### Transition Matrix:

| Current State | Target State | Permitted? | Business Trigger |
| :--- | :--- | :--- | :--- |
| `PENDING` | `ACTIVE` | **YES** | Initial invoice payment succeeded |
| `PENDING` | `CANCELED` | **YES** | User aborts checkout or initial payment abandoned |
| `PENDING` | `PAST_DUE` | **NO** | Cannot be past due without prior active period |
| `ACTIVE` | `PAST_DUE` | **YES** | Recurring billing charge failed |
| `ACTIVE` | `CANCELED` | **YES** | Subscriber cancellation or terminal charge failure |
| `ACTIVE` | `ACTIVE` | **YES (Idempotent)** | Duplicate payment confirmation |
| `PAST_DUE` | `ACTIVE` | **YES** | Dunning recovery payment succeeded |
| `PAST_DUE` | `CANCELED` | **YES** | Dunning period expired or subscriber canceled |
| `CANCELED` | *Any State* | **NO (STRICT)** | **Terminal state.** Reactivation requires new subscription ID |

---

## 5. Database Isolation Levels & Concurrency Control

### 5.1 Concurrency Strategy: Pessimistic Row Locking (`SELECT FOR UPDATE`)

For high-throughput payment mutations, relying purely on optimistic concurrency control (`version` checks with retry loops) creates high abort rates under bursts of concurrent events (e.g. user clicking "Charge" while an automated renewal webhook arrives simultaneously).

Instead, we employ **pessimistic row-level locking (`SELECT ... FOR UPDATE`)**:
- When modifying a subscription or recording transactions against it, the session acquires an exclusive row lock on `subscriptions`.
- Concurrent transactions attempting to modify the same subscription queue up until the active transaction completes `COMMIT` or `ROLLBACK`.
- Locks are released immediately upon transaction termination.

### 5.2 Transaction Isolation Level: `READ COMMITTED`

In PostgreSQL:
- The default isolation level is `READ COMMITTED`.
- In `READ COMMITTED`, `SELECT ... FOR UPDATE` acquires an exclusive tuple lock. If another concurrent transaction is already updating that row, the waiting transaction will block until the first commits, then re-read the updated row with the latest committed values.
- This provides the perfect balance between high throughput (no phantom serialization failures typical of `SERIALIZABLE`) and absolute correctness (no dirty reads, no lost updates, guaranteed deterministic FSM evaluation).

---

## 6. RFC 7807 Error Handling Standard

All business, concurrency, and validation exceptions emit structured JSON conforming to **RFC 7807 (Problem Details for HTTP APIs)** with the `Content-Type: application/problem+json` header:

```json
{
  "type": "https://api.platform.internal/errors/illegal-state-transition",
  "title": "Illegal State Transition",
  "status": 409,
  "detail": "Illegal state transition from 'CANCELED' to 'ACTIVE'. Reason: Subscription is in terminal 'CANCELED' state and cannot be reactivated or altered.",
  "instance": "/v1/subscriptions/charge",
  "current_status": "CANCELED",
  "target_status": "ACTIVE",
  "subscription_id": "018e47d1-0000-7000-8000-000000000001"
}
```
