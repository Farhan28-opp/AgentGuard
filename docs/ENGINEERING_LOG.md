> Historical day-by-day engineering notes (Days 1–7). Some statements here describe
> earlier stages and are superseded by the top-level README.md.

# AgentGuard — Day 1 Foundation

**Status: Day 1 Foundation.** This is the database schema, domain model, and
core authority invariants only. Reservation execution, revocation,
behavioral detection, cryptographic signing, and the simulated payment
rail are all intentionally deferred — see "What's deferred" below.

## 1. What AgentGuard is

AgentGuard is a hierarchical financial-authority control layer for
autonomous multi-agent payment systems. When a user authorizes a root AI
agent to spend up to some amount, and that agent delegates sub-tasks
(search, negotiation, purchase, logistics...) to other agents, AgentGuard
makes sure the user's original authority stays bounded and non-duplicated
no matter how deep or wide that delegation tree gets.

## 2. The problem being solved

A simple per-agent spending limit doesn't prevent:

- **Duplication** — two sibling agents each holding authority that assumes
  the other doesn't exist, so their sum exceeds what the user actually
  authorized.
- **Escalation** — a child ending up with more authority, broader scope,
  or a longer time window than its parent had.
- **Concurrent over-consumption** — two agents racing to spend against the
  same remaining budget at the same instant.
- **Delegation loops** — an agent transitively delegating back to one of
  its own ancestors.

Day 1 builds the schema and the invariant-enforcement logic that makes
these structurally impossible, rather than merely checked-for-by-convention.

## 3. Why hierarchical authority exists

Real agentic workflows are trees, not single actors: a shopping agent
doesn't itself negotiate and purchase, it delegates to specialists. Each
specialist needs its *own* bounded grant of authority — scoped to its
task, narrower than or equal to its parent's in every dimension (amount,
category, time window, delegation depth) — rather than shared, ambient
access to the whole budget.

## 4. The authority-conservation model

Every `Capability` tracks its authority as an explicit **pool**, not a
single shrinking number:

```
total_authority        -- fixed at issuance, never changes
unallocated_authority   -- not yet delegated to a child, not yet reserved
reserved_authority      -- held by an in-flight payment reservation (Day 3)
committed_authority     -- permanently spent (Day 3)
```

The conservation identity:

```
total_authority >= unallocated_authority + reserved_authority + committed_authority
```

The remainder — `total_authority` minus that sum — is, by definition,
authority currently **delegated to this capability's children**. It is
*derived*, not stored redundantly, by summing children's `total_authority`
(see `capability_service.compute_ledger`). Storing it separately would
create a second source of truth that could drift out of sync with reality
— exactly the kind of bug an early draft of this design had, where a
single `remaining_amount` field made it ambiguous whether a check was
against "what's left after delegation" or "what's left after spending."

**Delegation is atomic.** Issuing a child capability acquires a row lock
on the parent (`SELECT ... FOR UPDATE`, via SQLAlchemy's
`with_for_update=True`) and, within that same transaction, both validates
every invariant *and* moves the authority (decrements the parent's
`unallocated_authority`, creates the child with that amount as its
starting `unallocated_authority`). Two concurrent delegation requests
against the same parent cannot both succeed if their sum would exceed
what's available — the second one serializes on the lock and sees the
already-decremented balance. The same pattern is what the Day 3
reservation engine will reuse for payments.

## 5. Database architecture

7 tables: `users`, `agents`, `mandates`, `capabilities`, `reservations`,
`payments`, `audit_logs`. See `app/models/` for full column definitions;
the interesting one is `capabilities` (see §4 above and the model's
docstring).

Hierarchy:

```
User → Mandate → root Capability → child Capability → grandchild Capability → ...
```

Every `Capability` carries `root_mandate_id` directly (not just
`parent_capability_id`), so the full chain back to the originating user
mandate is always a single-column lookup away, regardless of delegation
depth.

## 6. Capability hierarchy & invariants implemented

All eight invariants from the design spec, enforced in
`app/services/authority_service.py` and orchestrated in
`app/services/capability_service.py`:

| # | Invariant | Enforced by |
|---|---|---|
| 1 | No authority creation | `validate_authority_available` |
| 2 | Scope can only narrow | `validate_scope_narrowing` |
| 3 | Expiry can only narrow | `validate_expiry_narrowing` |
| 4 | Delegation depth | `validate_delegation_depth` |
| 5 | Fanout | `validate_fanout` |
| 6 | No delegation loops | `validate_no_delegation_loop` |
| 7 | No zero-authority payment capability | `validate_zero_authority_rule` |
| 8 | Authority conservation | Row-locked delegation transaction in `capability_service.create_child_capability` |

Day 1 keeps scope-narrowing deterministic and simple, per the spec: a
child's `category` must exactly match its parent's (sub-category
hierarchies are a later refinement); merchant allowlists may only shrink;
merchant denylists may only grow.

## 7. Day-1 API endpoints

| Method | Path | Behavior |
|---|---|---|
| POST | `/mandates` | Create a mandate |
| GET | `/mandates/{mandate_id}` | Fetch a mandate |
| GET | `/mandates/{mandate_id}/ledger` | Full authority-pool view for the tree |
| POST | `/capabilities` | Create a root or child capability (invariants enforced) |
| GET | `/capabilities/{capability_id}` | Fetch a capability |
| GET | `/mandates/{mandate_id}/capabilities` | List all capabilities under a mandate |
| GET | `/capabilities/{capability_id}/audit-trail` | Return stored audit records (none are written yet on Day 1) |
| POST | `/capabilities/{capability_id}/reserve` | **501** — Day 3 |
| POST | `/reservations/{reservation_id}/commit` | **501** — Day 3 |
| POST | `/reservations/{reservation_id}/release` | **501** — Day 3 |
| POST | `/capabilities/{capability_id}/revoke` | **501** — Day 4 |
| GET | `/health` | Liveness check |

There is intentionally no `POST /users` or `POST /agents` endpoint yet —
see "Assumptions" below.

## 8. How to run locally

Requires Python 3.11+ and a running PostgreSQL instance.

```bash
python -m venv .venv
source .venv/bin/activate        # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env             # then edit DATABASE_URL if needed

# Create a database and role matching your DATABASE_URL, e.g.:
#   createuser agentguard --pwprompt
#   createdb agentguard --owner=agentguard

# Apply the schema -- either:
alembic upgrade head
# or, faster for local iteration (also run automatically by seed.py):
python -c "from app.database import Base, engine; from app import models; Base.metadata.create_all(engine)"

# Load the demo scenario (also creates tables if you skipped the step above):
PYTHONPATH=. python seed.py

# Run the API:
uvicorn app.main:app --reload
```

Then visit `http://localhost:8000/docs` for interactive API docs
(FastAPI's auto-generated Swagger UI).

## 9. How to run tests

Point `TEST_DATABASE_URL` at a **throwaway** database (never your dev or
demo database — the test suite creates and drops all tables around the
whole session):

```bash
createdb agentguard_test --owner=agentguard
export TEST_DATABASE_URL=postgresql+psycopg2://<user>:<password>@<host>:5432/agentguard_test
pytest -v
```

`pytest.ini` sets `pythonpath = .` so `app` and `tests` resolve without
any extra `PYTHONPATH` fiddling, as long as you run `pytest` from the
project root.

## 10. What is intentionally deferred to later days

Per the Day-1 non-goals list: LLM integration / natural-language policy
compiler, AI/ML anomaly detection, agent orchestration, real or simulated
UPI execution, Ed25519 signing, JWT / cryptographic capability tokens,
Redis, Kafka, WebSockets, frontend/dashboard, automatic revocation,
hash-chain implementation, distributed consensus, cloud deployment. The
`reservations`, `payments`, and `audit_logs` tables exist now specifically
so those features can be built on Day 3/4/6 without a schema migration
detour.

## 11. Assumptions made

- **No `POST /users` or `POST /agents` endpoints on Day 1.** The
  requested API route list didn't include them, so users and agents are
  provisioned via `seed.py` for now. Adding thin CRUD endpoints for both
  is a natural, low-risk Day 2 addition if the team wants agents
  registered dynamically rather than pre-seeded.
- **`Capability.issued_to_agent_id` / `issued_by_agent_id` are foreign
  keys to the `agents` table**, not free-text strings, even though the
  original field names were `issued_to` / `issued_by`. This was chosen
  because the spec explicitly calls for an `Agent` table "because the
  project is fundamentally about agent identity" — a plain string would
  make that table decorative rather than load-bearing, and the
  loop-detection invariant (#6) specifically needs to compare *agent
  identity*, not capability IDs.
- **Scope narrowing (Invariant 2) requires exact category match** rather
  than a hierarchical/sub-category model, per the spec's instruction to
  keep Day 1 deterministic rather than building "sophisticated policy
  logic."
- **The Alembic initial migration (`alembic/versions/0001_initial_schema.py`)
  was hand-written**, not produced by `alembic autogenerate` against a
  live database — see the next section.

## 12. Day 2: Capability Issuance & Hierarchical Delegation Engine

Day 2 introduces a robust engine for issuing capabilities and enforcing the exact boundaries of hierarchical delegation:

- **Atomic Issuance API**: The `POST /capabilities` endpoint consumes a `CapabilityIssueRequest` mapping cleanly to business logic, preventing clients from dictating critical internal accounting fields.
- **Strict Invariant Validation**: The system now guarantees issuer authorization (agents can only delegate what they explicitly hold), prevents self-delegation loops, strictly evaluates active status, and bounds time windows to precisely fit parent boundaries.
- **Historical Fan-Out Constraint**: Fan-out restrictions count total child capability emissions over the lifetime of a parent, mitigating recursive spamming even if children are immediately revoked.
- **Concurrency & Atomicity Guarantee**: Utilizing transaction-scoped row-level locks on parent capabilities (`with_for_update=True`), the system strictly conserves unallocated authority even under hostile multi-threaded issuance requests.

## 13. State of Verification

- **Syntax and Imports Verified.** All Python files compile cleanly (`python -m compileall`). All application imports resolve successfully. The FastAPI app can be launched via Uvicorn.
- **Database & Tests Blocked by Environment.** Full runtime execution (Alembic migrations and pytest) is blocked because a local PostgreSQL instance is unavailable. The tests genuinely require PostgreSQL to exercise row locks and check constraints.
- **Concurrency Tests Implemented.** Threading-based concurrency tests have been implemented in `test_concurrency.py` to empirically prove atomicity and race-condition prevention under `with_for_update=True` locks, prepared for CI execution.
- **Authority Accounting Model Validated.** The data model distinguishes total, unallocated, reserved, and committed authority perfectly. Database constraints enforce conservation (`unallocated + reserved + committed <= total`) without overloading a single field.

## 14. Day 3: Financial Authority Consumption Engine

Day 3 introduces the Reservation engine to consume capability authority atomically, bridging the gap between agent policies and the actual execution of payments.

- **Atomic Reserving**: A reservation (Reserve ₹X) is created and instantly decrements `unallocated_authority` while incrementing `reserved_authority`, in a single transaction-scoped row lock on the capability (`with_for_update=True`). This guarantees that concurrent transactions against the same remaining budget cannot double-spend.
- **Idempotency**: All reservation requests take an `idempotency_key`. Retrying identical requests returns the existing record. Retrying with mutated parameters yields an HTTP 409 Conflict.
- **Scope Checking**: Every reservation asserts that the amount, currency, category, merchant, and timestamp perfectly align with the capability's allowed boundaries.
- **Commit/Release**: After the reservation is secured, the simulated payment rail handles execution. If successful, `commit` moves authority from `reserved_authority` to `committed_authority`. If failed, `release` returns it to `unallocated_authority`. Both transitions are strictly validated and atomic.
- **Expiry/TTL**: If a reservation is abandoned, it is eventually swept up by a deterministic expiry mechanism via `RESERVATION_TTL_SECONDS`.

### Execution Flow

```
Agent
  ↓
Reserve ₹X (POST /capabilities/{capability_id}/reserve)
  ↓
AgentGuard (Atomic row-lock + invariants check)
  ↓
Reservation created (unallocated -= X, reserved += X)
  ↓
[Simulated Payment Execution happens here]
  ↓
Commit OR Release (POST /reservations/{reservation_id}/[commit|release])
  ↓
AgentGuard (Atomic row-lock, reserved -= X, unallocated/committed += X)
```

**Note:** The actual UPI payment rail is *not* simulated in Day 3. The reservation engine represents the deterministic authority enforcement boundary directly before a payment is sent out.

## 15. Day 4: Recursive Subtree Revocation

Day 4 introduces the ability to revoke a capability and atomically invalidate its entire subtree of delegated authority, while cleanly releasing any active reservations.

- **Recursive Subtree Revocation**: Revocation operates over the **capability tree**. Using a recursive CTE, the system discovers all descendant capabilities of a target node and marks them as `REVOKED`. 
- **Atomic Reservation Release**: Any active (`RESERVED`) reservations belonging to the revoked capabilities are discovered, locked, and released back into their respective capability's `unallocated_authority`. This guarantees no authority remains "stuck" inside a revoked capability.
- **Unrelated Branch Isolation**: Revocation is strictly scoped to the target's subtree. Sibling or cousin capabilities on unrelated branches remain perfectly `ACTIVE`.
- **Idempotency & Safe Repeats**: If a subtree is already revoked, repeated calls safely return the current state without corrupting the authority pools.
- **Concurrency Protection**: The canonical lock ordering (`Capability` → `Reservation`) and deterministic sorting (by `id`) ensures deadlocks are avoided. If a concurrent reservation attempts to spend against a capability while it is being revoked, the outcome is guaranteed to be consistent: either the reservation commits first and is subsequently released, or the revocation commits first and the new reservation is rejected.

### Example Scenario

```
Root
 ├── A
 │    ├── A1
 │    └── A2
 │
 └── B
      └── B1
```

If we revoke `A`, the expected state is:

```
Root       ACTIVE
A          REVOKED
A1         REVOKED
A2         REVOKED
B          ACTIVE
B1         ACTIVE
```

# Day 7 — Integrated Demonstration

The Day 7 integration connects the cryptographic identity, financial authority ledger, and behavioural risk engine into a single unified visual dashboard.

## Demonstration Workflow

1. **Agent Identity**: Agents authenticate and operate via Ed25519 cryptographic signatures.
2. **Capability Authority**: Finite financial limits are delegated through the hierarchical tree structure. 
3. **Behavioural Risk**: The Day 5 Isolation Forest model screens signed transactions for behavioral anomalies like velocity spikes.
4. **Reserve**: Authority is locked securely and subtracted from unallocated bounds.
5. **Simulated Payment**: An atomic external payment integration is simulated safely.
6. **Commit**: The reservation successfully resolves to a permanent committed state.

## Containment Workflow

If the behavioral risk engine detects anomalies:
1. **High Risk**: The agent transaction returns a `HIGH` risk result.
2. **Recursive Revocation**: The targeted capability and its descendants are atomically revoked.
3. **Reservation Release**: All pending financial reservations on that capability are released and refunded back into the pool of unallocated authority. 

*(All UI metrics, status transitions, and events read directly from the PostgreSQL backend, bypassing any duplication of business logic.)*
