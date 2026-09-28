# AgentGuard

**AgentGuard lets autonomous software spend within financial authority that is explicitly
bounded, attributable, enforceable, and revocable.**

It is financial-authority infrastructure for autonomous agents. Authority is represented as
signed, bounded **capabilities** that can be issued, attenuated, delegated, reserved,
consumed (committed), released and revoked, while preserving authority conservation and
provenance. The user never gives an agent access to money. The agent receives bounded
authority — and **that authority is enforced on [Drunix](https://github.com/npci/drunix)**.

## Built on Drunix

AgentGuard decides whether a request is acceptable — agent identity (Ed25519), policy and
behavioural risk (IsolationForest). The **`agentauth` chaincode on Drunix independently decides
whether the authority exists**: every delegation, reserve and commit is endorsed by the Lite Peers
of both organisations, ordered, validated by the Committing Peers and written to Drunix's SQL state
database before AgentGuard records it. A payment is recorded only after Drunix commits it **VALID**.

Drunix refuses invalid operations on its own — over-limit reserves, double commits,
idempotency-key reuse, commits after revocation, concurrent overspend — even when they are sent
straight to the chaincode with AgentGuard's own ledger credentials (see the Security Lab on the
`/drunix` page). Details: [`docs/DRUNIX_INTEGRATION.md`](docs/DRUNIX_INTEGRATION.md).

```text
AI agent ─▶ Ed25519 verify ─▶ policy + authority checks ─▶ IsolationForest ─▶ ledger_sync
   ─▶ drunix-bridge (Fabric Gateway) ─▶ Lite Peers endorse (agentauth) ─▶ Orderer
   ─▶ Committing Peers + validation service (VSCC, MVCC) ─▶ SQL state DB ─▶ VALID ─▶ payment + receipt
```

Quick start (Drunix repository checked out, Go ≥ 1.26.1, Python 3.11+, PostgreSQL):

```bash
cp drunix/drunix.env.example drunix/drunix.env        # set DRUNIX_HOME
drunix/scripts/up.sh && drunix/scripts/deploy.sh && drunix/scripts/bridge.sh --background
# .env: DRUNIX_MODE=enforce and DRUNIX_BRIDGE_URL=http://127.0.0.1:8090
alembic upgrade head && uvicorn app.main:app --host 127.0.0.1 --port 8001
drunix/scripts/demo.sh --lab                          # the judges' demo, end to end
```

With `DRUNIX_MODE=off` (the default) AgentGuard runs exactly as before, on PostgreSQL alone.

On top of that sits a consumer fintech prototype: payments, a simulated marketplace
where software agents shop within your policy, an order and receipt lifecycle, an activity
feed, and a technical Security Center.

> **Hackathon prototype.** The payment rail is simulated. The merchant marketplace is a
> controlled simulated marketplace. Agent orchestration is controlled software-agent
> orchestration, not unrestricted LLM autonomy. Behavioural-anomaly data is a controlled
> security simulation. This is not a production banking or UPI implementation.

---

## Architecture

```text
Browser (same-origin; relative /product/... calls; no hardcoded hosts)
   │
FastAPI app (app/main.py) ── templates/ + static/ (paths resolved from the package)
   │
   ├── Product API  (app/api/product.py, app/api/commerce.py, app/api/payment_requests.py)
   │     tasks · cart · merchant comparison · checkout · authorization · orders · policy ·
   │     direct payments (recharge / bill / send money) · transaction history + receipts
   ├── Ledger API   (/capabilities, /reservations/*: public signed routes)
   ├── Security Center + Security Lab (/dashboard/*, /demo/*: DEMO_MODE only)
   │
   ├── Services: capability · reservation · payment (simulated rail) · revocation ·
   │             risk (IsolationForest) · signed_ops (Ed25519) · policy · commerce ·
   │             containment · audit
   │
   ├── Drunix layer (app/services/ledger_sync.py, drunix_client.py; /drunix page + API)
   │
PostgreSQL (DATABASE_URL, required) ── Alembic migrations 0001 → 0006
   │
drunix/bridge (Go, Fabric Gateway) ──▶ Drunix network ──▶ agentauth chaincode (drunix/chaincode-agentauth)
```

## Agent hierarchy

```text
User  ── agent policy + standing mandate (overall authority, 30 days)
  ↓   signed root capability (grant signed by system-agent)
Main Agent                    ₹10,000  standing; delegates, and takes back unused authority
  ├── Search Agent                ₹0     searches the simulated marketplace (can look, cannot pay)
  ├── Merchant Optimization Agent ₹0     prices the cart at every simulated merchant and recommends
  │                                      one (deterministic optimization over listed prices — it
  │                                      does not negotiate and contacts no merchant; cannot pay)
  └── Purchase Agent              per purchase: a FRESH capability of min(budget, per-purchase
                                  limit, Main Agent's unallocated), scoped to allowed merchants

Wallet key (direct payments)      no standing authority; a single-use mandate for exactly the
                                  amount the user authorizes (see "Direct payments")
```

Each agent has its own Ed25519 identity. Only public keys are stored in the database.

Earlier versions gave the comparison agent (then called the Negotiation Agent) a fixed ₹300
"buffer" carved out of the Main Agent, which no flow ever spent and which capped the Main Agent at
₹9,700. It now holds ₹0 like the Search Agent; on an existing database the old buffer is handed back to
the Main Agent once, with the normal signed `attenuate` (Drunix `ReturnUnused`). The internal role key
and database column keep the name `negotiation`.

## Authority model

Each capability keeps `total ≥ unallocated + reserved + committed`, and the remainder
equals the sum of its children's totals. Delegation **transfers** authority; it never mints it.

| Operation | What happens | Who signs |
|---|---|---|
| issue / delegate | child capability carved from the parent's unallocated pool | grant signed by issuer (root: `system-agent`) |
| reserve | unallocated → reserved (TTL hold) | holder agent (`reserve`) |
| pay + commit | reserved → committed; Payment + Order written in **one** transaction | holder agent (`pay`) |
| release | reserved → unallocated | holder agent (`release`) |
| attenuate / return | unused authority handed back to the parent; grant re-signed | issuer (`attenuate`) |
| revoke | recursive subtree revocation; RESERVED holds released; committed stays committed | issuer / root agent (`revoke`) |

Authority held by a **contained** agent is not returned. It stays frozen in the revoked
capability.

## Agent policy (every setting is enforced)

| Setting | Default | Enforced by |
|---|---|---|
| Overall authority | ₹10,000 | ledger: mandate + Main Agent root capability (changing it re-issues authority) |
| Per-purchase limit | ₹3,000 | ledger: size of each Purchase capability, plus a pre-delegation policy check. The same limit caps each **direct payment** (checked before it can be authorized, and again at authorization) |
| Allowed categories | groceries, household, personal care | policy check before delegation (electronics is blocked by default) |
| Allowed merchants | QuickKart, FreshBasket, DailyMart | ledger: capability merchant allowlist, enforced by `reservation_service` |
| Approval mode | Always ask | task state machine: `always` / `above_threshold` / `autonomous`. The Purchase Agent still signs, and risk still runs |

## Security architecture

- **Identity:** Ed25519 per agent. The key backend is `file` (random keys in `dev_keys/`, git-ignored) or `derived` (`AGENT_KEY_SEED` set). In derived mode keys are HMAC-derived from an environment secret, nothing touches disk, and keys survive redeploys.
- **Signed requests:** `reserve`, `pay`, `release`, `revoke` and `attenuate` go through `verify_signed_request`. It checks the registered key, signature, ±300 s timestamp window, one-time nonce (replay protection), payload hash (integrity), and operation plus resource binding. A **suspended** agent's key is refused.
- **Deterministic authority layer (sovereign):** capability chain active, holder check, amount ≤ unallocated, scope and merchant allowlist, time window, conservation, row locks with `populate_existing`. This layer is never overridden by ML.
- **Behavioural layer:** a real trained IsolationForest scores requests that already passed the hard checks, using the agent's history across all its capabilities. `LOW→ALLOW`, `MEDIUM→REVIEW` (blocked), `HIGH→CONTAIN`.
- **Containment:** core subtree revocation releases holds. The agent is then suspended (key refused) and its other grants are revoked. Future transactions are rejected until the user explicitly replaces the agent with a new identity.
- **Honest limits:** containment and agent suspension are system control actions, attributed in the audit log, not agent-signed. `commit` happens inside the signed `pay`.

## Shopping flow ("Buy groceries for me under ₹3,000")

1. The instruction is parsed deterministically for category and budget. A task and an empty cart are created, and the Search Agent searches the marketplace.
2. The user searches and filters products, adds them to the cart, and changes quantities. **All totals come from the backend.**
3. **Compare platforms**: the Merchant Optimization Agent prices the cart at all three simulated merchants (items, stock, fees, delivery time, policy, the task budget and the authority limit) and recommends one with a reason that states the basket total against the budget. The budget is a ceiling, never a target.
4. **Choose Platform Myself** or **Let Agent Decide**. The agent picks the lowest total, unless another option is ≥15 min faster for ≤ max(₹30, 2 %) more.
5. **Send to Purchase Agent**: policy check → Main Agent delegates the Purchase capability → Purchase Agent signs `reserve` → verification → authority checks → IsolationForest → amount **RESERVED**.
6. **Agent payment request** review: items, quantities, subtotal, delivery fee, total, agent authority, remaining authority, and Identity / Signature / Policy / Risk / Reservation status.
7. **Authorize** (or auto-approve per policy): signed `pay` → simulated rail → commit → **Order** (stock decremented) → unused authority returned (signed `attenuate`) → **receipt** at `/orders/AG-…`.

A budget above the per-purchase limit (e.g. "under ₹10,000" with a ₹3,000 limit) is **never silently
lowered**: the task keeps the ₹10,000 budget, the Purchase Agent can be given at most ₹3,000, and the UI
says so and offers **Edit per-purchase limit** (the existing policy editor). After raising the limit to
₹10,000 the same request can use the full ₹10,000.

Task state machine: `CREATED → RUNNING → AWAITING_AUTHORIZATION → AUTHORIZED → COMPLETED`.
The failure states are `CANCELLED`, `EXPIRED`, `FAILED`, `REVIEW_REQUIRED`, `CONTAINED` and `PAYMENT_FAILED`. A
second authorization, or one after expiry, revocation, cancellation, completion or containment, is rejected.

## Direct payments (recharge, bills, send money)

A direct payment is authorized by the user, not delegated to an agent, but it uses the same
authorization infrastructure: the same persisted state machine (`agent_tasks`, `kind = "direct"`), the
same statuses, the same signed operations, risk engine, Drunix hooks and receipt.

```text
POST /product/payments/recharge|bill|send-money      prepare — nothing issued, held or paid
   policy (per-payment limit) → wallet key registered/active → IsolationForest pre-check (read-only)
   → Drunix readiness → AWAITING_AUTHORIZATION (review: type, payee, amount, actor, policy, authority,
     risk, Drunix, reservation status, what happens next)
POST /product/payments/requests/{id}/authorize       the user's explicit authorization
   single-use mandate + capability for exactly the amount (Drunix RegisterMandate, RegisterRootCapability)
   → wallet key signs reserve → authority checks + IsolationForest (authoritative) → Drunix Reserve VALID
   → wallet key signs pay → Drunix Commit VALID → simulated rail → receipt
POST /product/payments/requests/{id}/cancel          nothing was issued; nothing to undo
```

Every failure fails closed: a rejected Drunix Reserve or Commit, a risk decision, an expired hold or a
policy change after the review ends the request without a payment, and anything already issued is
withdrawn (hold released, single-use capability revoked; both mirrored on Drunix). If Drunix is only
unreachable at the commit, the hold is kept and authorizing again resumes at the commit.

**Why direct payments used to be rejected, and the risk-feature fix.** The single-use capability is
sized to exactly the payment, so `authority_consumption_rate` (projected spend ÷ capability total) was
always 100 %. The IsolationForest was trained with that feature between 5 % and 50 % (an agent spending
part of a delegated budget), so every direct payment started at the edge of "normal", and a first-time
biller plus an amount unlike the wallet's earlier payments pushed borderline cases to MEDIUM → REVIEW
(HTTP 409) before Drunix was ever called. Example: transfer ₹500 → bill ₹850 → recharge ₹149 scored
0.0391 (MEDIUM). For a *single-purpose* grant (root capability, no delegation, no prior spend) the
consumption rate is now measured against the user's **per-payment limit** — the same thing it measures
for an agent's Purchase capability, which is sized to that limit. The basis is set server-side only
(never from a request), can only widen the denominator, and the explanation says which basis was used.
The model, thresholds and the other five features are unchanged; the same recharge now scores 0.0193
(LOW) and still reports its −2.1σ amount deviation. Genuinely anomalous behaviour (burst velocity, far
larger amounts, new recipients, unusual hours) is still blocked — see `tests/test_final_pass.py`.

## Activity and receipts

`/activity` lists every payment request (agent purchases and direct payments) with row selection and a
detail panel. Each transaction has a unified receipt (`/transactions/{id}`, API
`GET /product/transactions/{id}`): type, merchant, amount, timestamp, payment ID, UTR, simulated rail,
actor and signature result, risk, policy, capability and reservation IDs, and the Drunix
Register/Delegate/Reserve/Commit transaction IDs and blocks read from the ledger journal — a field that
does not exist is shown as "Not available", never invented. **Repeat & Edit** / **Edit & Retry** start a
*new* request pre-filled from the old one. **Hide from Activity** only sets `agent_tasks.hidden_at`;
payments, orders, the Drunix journal and audit events are never deleted.

## Containment flow (Controlled Security Simulation)

On a pending payment request, **Run simulation** injects 20 labelled synthetic rapid holds on the
Purchase Agent's capability. They are not real history. The agent then sends one genuine
signed request for 90 % of its remaining authority at an *allowed* merchant it has never used:

```text
valid agent → valid signature → valid authority → abnormal behaviour → IsolationForest HIGH
→ CONTAIN → recursive capability revocation → all holds (incl. the pending payment) released
→ agent suspended → follow-up request blocked → next purchase rejected until the agent is replaced
```

The Security Lab in the Security Center runs the same scenario on separate `lab-*` agents,
plus a tampered request, an over-limit request (a hard rule, where the model is never consulted), and a
concurrent race.

## Technology stack

Python 3.11 · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL 14+ · cryptography (Ed25519) ·
scikit-learn IsolationForest · Jinja2 templates + vanilla JS · pytest · Railway.

## Local setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # set DATABASE_URL (required)
createuser agentguard --pwprompt
createdb agentguard --owner=agentguard
alembic upgrade head                 # head: 0006_hidden_at
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open <http://127.0.0.1:8001>. On startup the app upserts the catalogue and provisions the demo
user's standing authority. It **never deletes data on startup**.

Submission package (no secrets, no local state): `python scripts/package_submission.py` writes
`dist/agentguard_submission.zip` and fails if it finds a private key, a credential or a key seed in it.

To clear a local database full of old test debris (local development only):
`python scripts/reset_demo_data.py --wipe-all --yes-delete-everything`.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | **yes** | PostgreSQL URL (`postgres://`, `postgresql://` or `postgresql+psycopg2://`). There is no default, and SQLite is rejected |
| `PORT` | Railway sets it | port used by `python -m app.serve` (host `0.0.0.0`) |
| `AGENT_KEY_SEED` | recommended in prod | derive agent keys from this secret (nothing written to disk) |
| `DEV_KEYS_DIR` | no | file key backend location (default `./dev_keys`, git-ignored) |
| `DEMO_MODE` | no (`true`) | enables `/demo/*` (scoped reset + Security Lab) |
| `BOOTSTRAP_ON_STARTUP` | no (`true`) | upsert catalogue + demo standing authority |
| `RESERVATION_TTL_SECONDS` | no (`90`) | authorization window |
| `EXPIRY_SWEEP_SECONDS` | no (`30`) | background release of expired holds |
| `RISK_TIMEZONE`, `RISK_NORMAL_HOURS_START/END` | no (`Asia/Kolkata`, 6, 23) | "unusual time" feature |
| `TEST_DATABASE_URL` | tests | throwaway database for `pytest` |
| `DRUNIX_MODE` | no (`off`) | `enforce`: delegation, reserve and commit require a VALID Drunix transaction |
| `DRUNIX_BRIDGE_URL` | in enforce mode | the Drunix bridge, e.g. `http://127.0.0.1:8090` |
| `DRUNIX_BRIDGE_TOKEN` | no | shared secret with the bridge's `BRIDGE_TOKEN` |
| `DRUNIX_TIMEOUT_SECONDS` | no (`45`) | client timeout for one Drunix transaction |

## Database and migrations

`alembic upgrade head` is the only schema setup step, locally and in production.
- 0001–0003: the core ledger, including a fix for the enum labels.
- 0004: the marketplace, agent policy, persistent tasks, and the demo-scoping columns.
- 0005: `ledger_transactions`, the journal of every Drunix transaction (additive).
- 0006: `agent_tasks.hidden_at` — "Hide from Activity" (additive, nullable; nothing is deleted).

`alembic check` reports no drift.

## Railway deployment

The repository includes `railway.json` (Railpack builder, `preDeployCommand: alembic upgrade head`,
start command `python -m app.serve`, health check `/health`), `.python-version` (3.11) and
`.railwayignore`.

```bash
npm i -g @railway/cli            # or: brew install railway
railway login
railway init                     # create a project (or `railway link` to an existing one)
railway add --database postgres  # Railway PostgreSQL service ("Postgres")
railway add --service agentguard # empty service for the app
railway variable set -s agentguard 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
railway variable set -s agentguard "AGENT_KEY_SEED=$(openssl rand -hex 32)"
railway variable set -s agentguard DEMO_MODE=true
railway up -s agentguard         # upload + build + pre-deploy migration + start
railway domain -s agentguard     # generate the public *.up.railway.app URL
curl https://<generated-domain>/health
```

Older CLI versions use `railway variables --set "KEY=value"` instead of `railway variable set`.
You can also set the same variables in the service's **Variables** tab in the dashboard.
The `${{Postgres.DATABASE_URL}}` reference variable assumes the database service is named
`Postgres`, which is Railway's default. If not, check `railway variables --service Postgres`.

## Health check

`GET /health` runs a real `SELECT 1` against the database. It returns `200 {"status":"ok","database":"ok", ...}`
with the dialect, server version, database name, Alembic revision, model status and key backend.
It returns `503 {"database":"unreachable"}` when the database is down.

## Demo instructions

1. Security Center → **Reset demo data**. This is a scoped reset that deletes only demo-user rows. It restores catalogue stock and leaves one demo user, a Main Agent, and the standing Search and Negotiation grants.
2. AI Agent → "Buy groceries for me under ₹3,000" → search, add and edit items → **Compare platforms** → choose one or **Let Agent Decide** → **Send to Purchase Agent** → review → **Authorize** → receipt → Activity → Security Center (use "View Security Trail" to see provenance).
3. Start another task, prepare it, then use **Run simulation** on the review screen to see containment. Then try another purchase (blocked) → **Replace Purchase Agent**.
4. Under Agent policy, set the approval mode to *Autonomous within policy* and watch a purchase settle without a prompt. Or untick a merchant and watch it disappear from eligible options (the ledger allowlist).

## Tests

```bash
createdb agentguard_test --owner=agentguard
pytest -q            # uses TEST_DATABASE_URL for everything; never touches your dev DB
AGENTGUARD_DRUNIX_LIVE_BRIDGE=http://127.0.0.1:8090 pytest tests/test_drunix_live.py -v   # against real Drunix
(cd drunix/chaincode-agentauth && go test ./...)   # chaincode unit tests
(cd drunix/bridge && go test ./...)                # bridge tests
```

## Known limitations

- The payment rail is simulated (`SIM…` UTRs). The marketplace (QuickKart, FreshBasket, DailyMart; brand-name products, invented prices and stock) is a controlled simulation.
- Orchestration is deterministic software agents. Instruction parsing, comparison and selection are rules, not an LLM.
- The anomaly demo seeds labelled synthetic holds, while the model itself is real. The IsolationForest was trained on synthetic "normal" data. It weights merchant novelty and delegation bursts more than raw velocity, and first purchases made late at night (outside 06:00–23:00 IST) by an agent with no history score HIGH.
- There is no user authentication: every visitor acts as the single demo user, so a public deployment is shared.
- Agent runtimes run in the server process, and derived keys are a stand-in for a KMS/HSM.
- This is not production banking or UPI: there is no KYC, PCI/NPCI compliance or real merchant settlement.

Historical day-by-day notes: [`docs/ENGINEERING_LOG.md`](docs/ENGINEERING_LOG.md).
