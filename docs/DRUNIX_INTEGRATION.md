# AgentGuard × Drunix — integration guide

> **Claim this integration makes demonstrably true:** AgentGuard does not merely ask Drunix to *record* an AI agent's
> payment. The `agentauth` chaincode on Drunix independently enforces the financial-authority rules that decide whether
> that payment can be reserved and committed — and it refuses invalid operations even when they are sent to it directly,
> with AgentGuard's own ledger credentials, bypassing every AgentGuard check.

## 1. Problem

AI agents are starting to execute payments on their own. Payment systems were designed around a human pressing "pay":
they check *who* is paying, not *whether this particular agent is still inside the authority a human gave it*. Once an
agent can delegate to sub-agents, retry, run concurrently and be compromised, "the agent had the card" is no longer an
answer to "was this payment authorised?".

## 2. Solution in one picture

```
 AI agent ──signed request──▶ AgentGuard (FastAPI)                                   Drunix
                              ├─ Ed25519 identity, nonce, payload hash, binding
                              ├─ policy + deterministic authority checks (PostgreSQL)
                              ├─ IsolationForest behavioural risk  → ALLOW / REVIEW / CONTAIN
                              └─ ledger_sync ──HTTP──▶ drunix-bridge ──Fabric Gateway (gRPC/TLS, Org1)──▶
                                                                          Lite Peer org1 ┐ endorse: agentauth runs
                                                                          Lite Peer org2 ┘ (both orgs, majority policy)
                                                                          Orderer (Raft) → block
                                                                          Committing Peers + validation service
                                                                          (VSCC + MVCC) → SQL state DB
                              ◀─ VALID tx id + block ───────────────────────────────────────────┘
                              payment + order recorded ONLY after a VALID Drunix commit
```

| Layer | Responsibility | Where |
|---|---|---|
| **AgentGuard** | Who is asking (Ed25519 agent keys, anti-replay nonces, payload integrity, operation binding), what the user allows (agent policy: categories, merchants, approval mode), task/cart/order workflow, audit log, dashboard | `app/` |
| **AI** | Behavioural anomaly detection on the agent's history (IsolationForest): `ALLOW`, `REVIEW` (blocked), `CONTAIN` (revoke subtree + suspend agent) | `app/services/risk_engine.py`, `feature_extraction.py` |
| **Drunix** | **Whether the authority exists**: mandate → capability tree in integer paise; delegation conservation; reserve / commit / release state machine; idempotency; expiry; revocation of a node blocking everything beneath it; concurrency (MVCC) | `drunix/chaincode-agentauth/` |

The AI layer stays off-chain, but its consequences are enforced on-chain: a `CONTAIN` decision revokes the capability in
AgentGuard **and** on Drunix, after which Drunix refuses every reserve or commit beneath it.

## 3. What runs on Drunix (`agentauth` chaincode)

State (all values are JSON documents — Drunix's SQL state DB stores public state in a JSONB column; money is integer
paise; time is the transaction timestamp, so every endorsing peer computes the same result):

| Key | Document |
|---|---|
| `MANDATE~id` | owner, controller (AgentGuard system agent), currency, total, allocated, window, status |
| `CAP~id` | parent, mandate, holder, issuer, total / unallocated / reserved / committed, category, merchant allow/deny lists, window, depth, status, grant-signature hash |
| `RES~id` | capability, holder, amount, currency, merchant, category, status, idempotency key, **request hash computed on-chain**, created/expiry, reserve/commit/release tx ids, payment reference |
| `IDEM~cap~key` | reservation id + request hash |

Functions: `RegisterMandate`, `RevokeMandate`, `RegisterRootCapability`, `Delegate`, `Reserve`, `Commit`, `Release`,
`ReturnUnused`, `Revoke`; reads `Info`, `GetMandate`, `GetCapability`, `GetReservation`, `GetAuthorityChain`.

Rules the chaincode enforces on its own (unit-tested in `authority/contract_test.go`):

* **Reserve** rejects: unknown/inactive/revoked capability · any inactive or revoked **ancestor** · inactive or expired
  mandate · wrong holder · amount > on-chain unallocated · currency, category or merchant outside scope · outside any
  time window on the path · idempotency key reused with different parameters (same parameters → the original hold,
  `replayed: true`) · re-used reservation id.
* **Commit** rejects: unknown reservation · not `RESERVED` · expired · already committed (`ALREADY_COMMITTED`) · released
  · capability / ancestor / mandate no longer active · wrong holder.
* **Delegate** moves authority (never mints it): issuer must hold the parent; amount ≤ parent unallocated; category equal;
  allowlist only narrows, denylist only grows; window inside the parent's; no self-delegation.
* **Revoke** marks the capability (and listed descendants) `REVOKED`, releases listed holds; descendants it was not told
  about are still blocked because every operation walks the ancestor chain. A revoked root stops counting against the
  mandate total (mirrors AgentGuard).
* **Access control:** only the AgentGuard operator organisation (`Org1MSP`, configurable with
  `AGENTAUTH_WRITER_MSPS`) may change authority state; anyone on the channel may read it.
* **Conservation:** `total ≥ unallocated + reserved + committed ≥ 0` is checked on every write; a randomised test runs
  5,000 operations and checks that authority is never minted or lost.

## 4. How AgentGuard talks to Drunix

`drunix/bridge` is a small Go service built on `fabric-gateway` v1.10 — the same client library and connection pattern as
Drunix's `asset-transfer-basic/application-gateway-go` and `rest-api-go` samples. It connects to the **Lite Peer**
(`peer0.org1.example.com:7051`) as `User1@org1.example.com`, reading the certificate and key at runtime from the network's
generated `organizations/` tree.

```
POST /v1/submit   {"function":"Reserve","args":[...]}   → 200 {tx_id, block_number, status:"VALID", result, latency_ms}
POST /v1/evaluate {"function":"GetCapability","args":[...]} → 200 {result}
GET  /health                                             → 200 only if the chaincode answered a real query
GET  /v1/transactions                                    → the bridge's recent submissions
```

The bridge never reports success because `Submit()` returned: it waits for the commit status (the Lite Peer forwards it
to the Committing Peer — a Drunix feature) and answers 200 only for validation code `VALID`. Failures are classified:

| Category | HTTP | Meaning |
|---|---|---|
| `CHAINCODE_REJECTED` | 409 | agentauth refused it at endorsement; carries the rejection `code` (e.g. `INSUFFICIENT_AUTHORITY`); never ordered |
| `MVCC_READ_CONFLICT` | 409 | ordered into a block but invalidated: a concurrent transaction changed the state first |
| `DRUNIX_INVALID_COMMIT` | 502 | ordered but committed with another non-VALID code |
| `DRUNIX_UNAVAILABLE` | 503 | gateway / orderer unreachable |
| `DRUNIX_TIMEOUT` | 504 | no final answer in time — outcome unknown; tx id returned for reconciliation |

On the Python side, `app/services/drunix_client.py` maps these to typed exceptions (`DrunixRejectedError`,
`DrunixConflictError`, `DrunixInvalidCommitError`, `DrunixUnavailableError`, `DrunixTimeoutError`) and refuses to accept
any "success" that is not a VALID commit with a transaction id.

## 5. Transaction lifecycle (DRUNIX_MODE=enforce)

`app/services/ledger_sync.py` is called from AgentGuard's **service layer**, so the product flow, the public signed API
(`/capabilities/{id}/reserve`, `/reservations/{id}/pay|commit|release`), the demo endpoints and the risk simulation all
get the same enforcement.

| AgentGuard operation | Drunix call | If Drunix does not confirm |
|---|---|---|
| root capability / delegation (`capability_service`) | `RegisterMandate` (once) + `RegisterRootCapability` / `Delegate` | **fail closed** — exception, PostgreSQL rolls back |
| reserve (`reservation_service.reserve_authority`, after signature + deterministic checks + IsolationForest `ALLOW`) | `Reserve` | **fail closed** — no hold exists anywhere |
| commit (`commit_reservation`, inside signed `pay`) | `Commit` (payment reference recorded on-chain) | **fail closed** — no payment, no order. Rejected → hold released, `PAYMENT_FAILED`. Unavailable / timeout / MVCC → hold kept, task stays payable, the user may retry. A retry after a timeout that Drunix answers `ALREADY_COMMITTED` is accepted only if the on-chain reservation shows exactly this payment reference (`RECOVERED`). |
| release (`release_reservation`, expiry sweeper) | `Release` | restrictive: AgentGuard's release stands; journal `SYNC_PENDING`, retried by the sweeper and `POST /drunix/sync/retry` |
| return unused (`return_unused_authority`) | `ReturnUnused` | restrictive → `SYNC_PENDING` |
| revoke / containment (`revocation_service`) | `Revoke` (subtree ids + released holds) | restrictive → `SYNC_PENDING` |
| mandate revoked on policy re-issue | `RevokeMandate` | restrictive → `SYNC_PENDING` |

**Direct payments** (recharge, bill, send money — `app/api/payment_requests.py`) go through exactly the same hooks. Preparing
a request submits nothing to Drunix (it only reads `/health` to show readiness on the review screen). Only after the user
authorizes: `RegisterMandate` + `RegisterRootCapability` for a single-use grant of exactly the amount → `Reserve` →
`Commit`. The unified receipt (`GET /product/transactions/{id}`) shows those transaction ids and blocks from the journal.

Every call is journalled in `ledger_transactions` (migration `0005`, additive) **in its own database transaction**, so a
Drunix rejection stays on record even though the business transaction that asked for it rolls back.

With `DRUNIX_MODE=off` every hook returns immediately: AgentGuard behaves exactly as before (the original 156 tests pass
unchanged).

Things intentionally **off-chain**: Ed25519 agent signatures and nonces, the risk model and its features, the agent
policy UI, marketplace / cart / orders / stock, the task state machine, the simulated payment rail, the audit log.
Labelled Behavioural-Risk-Simulation holds are PostgreSQL-only and are skipped by the ledger.

## 6. Setup

Prerequisites: Python 3.11+, Go ≥ the version in Drunix's `go.mod` (1.26.1), PostgreSQL for AgentGuard, the Drunix
repository checked out, PyYAML (`pip install -r drunix/scripts/native/requirements.txt`). For the native runtime also a
PostgreSQL *server* (or Docker), `redis-server` (or Docker) and `jq` (Drunix's channel scripts use it).

```bash
cp drunix/drunix.env.example drunix/drunix.env     # set DRUNIX_HOME=/path/to/drunix
```

### Runtimes

* **`DRUNIX_RUNTIME=native` (default).** Builds Drunix from source (`make peer orderer vscc configtxgen
  configtxlator cryptogen osnadmin` + Drunix's `ccaas_builder`) and runs the *same* topology as Drunix's
  `compose/compose-test-net.yaml` — orderer, a Lite Peer, a Committing Peer and a validation service per org —
  as local processes. Each node gets exactly the environment Drunix's compose file defines for it; only host paths,
  the state DB address (a local PostgreSQL stands in for YugabyteDB's PostgreSQL-compatible YSQL; Drunix uses gorm's
  postgres driver) and the KeyDB address (redis-server) are rewritten. Chaincode runs as a service (Fabric CCaaS via
  Drunix's ccaas_builder); channel creation and the chaincode lifecycle reuse Drunix's own `createChannel.sh`,
  `envVar.sh` and `ccutils.sh`. No container images are needed, so this is also the path that avoids amd64 emulation on
  Apple Silicon. Needs the node hostnames in `/etc/hosts`:
  `127.0.0.1 orderer.example.com peer0.org1.example.com peer1.org1.example.com peer2.org1.example.com peer0.org2.example.com peer1.org2.example.com peer2.org2.example.com`
  `DRUNIX_INFRA=docker` runs PostgreSQL and Redis as `agentguard-drunix-*` containers instead of local binaries.
* **`DRUNIX_RUNTIME=docker`.** Drunix's `./network.sh up createChannel` and `./network.sh deployCC` with the published
  `npcioss/drunix-*` images. On Apple Silicon these images are `linux/amd64` only (Rosetta emulation); Drunix's script also
  needs the `docker-compose` v1 command and bash ≥ 4.

### Local macOS setup (Apple Silicon) — verified

This exact sequence was run and verified end to end on macOS 26 / arm64 (Go 1.27, Docker Desktop, bash 3.2 — no
Homebrew bash needed): native Drunix nodes, `agentauth` deployed, bridge `READY`, real Reserve → VALID → Commit → VALID
purchases, chaincode rejections, reconciliation `MATCH` and the `/drunix` page showing those transactions.

One-time:

```bash
cd ~/Downloads/agentguard_2
cp drunix/drunix.env.example drunix/drunix.env
#   DRUNIX_HOME=/Users/<you>/Projects/drunix   DRUNIX_RUNTIME=native   DRUNIX_INFRA=docker
sudo sh -c 'echo "127.0.0.1 orderer.example.com peer0.org1.example.com peer1.org1.example.com peer2.org1.example.com peer0.org2.example.com peer1.org2.example.com peer2.org2.example.com" >> /etc/hosts'
brew install jq                                    # if missing
.venv/bin/pip install -r requirements.txt -r drunix/scripts/native/requirements.txt
```

`.env` (git-ignored) — AgentGuard's **own** database and the Drunix settings:

```bash
DATABASE_URL=postgresql+psycopg2://agentguard:<password>@127.0.0.1:5434/agentguard
TEST_DATABASE_URL=postgresql+psycopg2://agentguard:<password>@127.0.0.1:5434/agentguard_test
DRUNIX_MODE=enforce
DRUNIX_BRIDGE_URL=http://127.0.0.1:8090
```

Use a dedicated PostgreSQL for AgentGuard, never another project's server on `:5432`. `scripts/local_db.sh` starts one
as the container `agentguard-postgres` on `127.0.0.1:5434`, taking user, password and database names from `.env`
(Drunix's own state DB is a separate container, `agentguard-drunix-state`, on `:5433`).

Every day (all idempotent):

```bash
cd ~/Downloads/agentguard_2
export PATH="$PWD/.venv/bin:$PATH"
scripts/local_db.sh                               # agentguard-postgres on :5434
drunix/scripts/up.sh                              # 7 Drunix nodes + state DB/KV containers + mychannel
drunix/scripts/deploy.sh                          # first time only (a re-run upgrades to the next sequence)
drunix/scripts/bridge.sh --background             # bridge on 127.0.0.1:8090
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Verify:

```bash
drunix/scripts/status.sh                          # every node UP; bridge: ok | drunix: reachable | chaincode: agentauth ready
curl -s http://127.0.0.1:8090/health              # "status":"ok","chaincode_status":"ready","gateway_connection":"READY"
curl -s http://127.0.0.1:8001/health              # "drunix":{"mode":"enforce",...}
curl -s http://127.0.0.1:8001/drunix/status       # "connected":true
open http://127.0.0.1:8001/drunix                 # ENFORCE · Connected · agentauth 1.0.0 · real transactions
drunix/scripts/demo.sh --lab --no-reset           # a real purchase (Reserve + Commit) and every ledger-bypass attack
```

Start AgentGuard **after** the bridge is healthy: in enforce mode its startup bootstrap registers the standing authority
on Drunix and fails closed (the log shows `demo bootstrap failed … DrunixUnavailableError`) if Drunix is not reachable.
Restart it once Drunix is up.

Stop: `drunix/scripts/down.sh` (keeps ledgers; `--purge` deletes ledgers and generated crypto), then stop uvicorn.

### Commands, in order

```bash
drunix/scripts/up.sh                    # build (first run) + start network + create mychannel      ~25 s
drunix/scripts/deploy.sh                # test + deploy agentauth (re-run = upgrade to next sequence) ~20 s
drunix/scripts/bridge.sh --background   # Gateway bridge on 127.0.0.1:8090
drunix/scripts/status.sh                # nodes, bridge, chaincode

# AgentGuard (.env): DRUNIX_MODE=enforce, DRUNIX_BRIDGE_URL=http://127.0.0.1:8090
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8001

drunix/scripts/demo.sh --lab            # scripted judges' demo incl. every ledger-bypass attack
drunix/scripts/down.sh [--purge]        # stop (purge: delete ledgers + generated crypto)
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DRUNIX_MODE` | `off` | `enforce` turns on on-chain enforcement |
| `DRUNIX_BRIDGE_URL` | — | bridge base URL (required in enforce mode) |
| `DRUNIX_BRIDGE_TOKEN` | — | optional shared secret; set the same `BRIDGE_TOKEN` on the bridge |
| `DRUNIX_TIMEOUT_SECONDS` | `45` | client timeout for one Drunix transaction |
| `DRUNIX_HOME` | — | Drunix repository (scripts) |
| `DRUNIX_RUNTIME` / `DRUNIX_INFRA` | `native` / `local` | see above |
| bridge: `DRUNIX_ORG_PATH`, `DRUNIX_PEER_ENDPOINT`, `DRUNIX_GATEWAY_PEER`, `DRUNIX_MSP_ID`, `DRUNIX_CHANNEL`, `DRUNIX_CHAINCODE`, `BRIDGE_LISTEN`, `DRUNIX_*_TIMEOUT` | Org1 / `dns:///localhost:7051` / `mychannel` / `agentauth` | set by `bridge.sh` |

## 7. Demo (3–5 minutes)

1. **Drunix Ledger** page (`/drunix`): mode ENFORCE, network connected, chaincode version, channel, latency.
2. **AI Agent** (`/agent`): "Buy groceries for me under ₹3,000" → add items (~₹2,450) → choose DailyMart → *Send to
   Purchase Agent*. The agent log shows: policy → **Drunix Delegate VALID** → signature → authority → IsolationForest →
   **Drunix Reserve submitted → VALID (block n)** → payment authorization required. The review screen has a *Drunix*
   check.
3. **Authorize** → **Drunix Commit submitted → VALID** → simulated payment → order → receipt. The receipt lists the
   Delegate, Reserve, Commit and Return-unused transaction ids and blocks.
4. Failure the product prevents: a cart over ₹3,000 is refused by AgentGuard's policy before anything is delegated.
5. **The Drunix proof** (`/drunix` → Security Lab): *Attack the live Main Agent capability* sends an over-limit reserve
   straight to the chaincode with AgentGuard's own credentials → `INSUFFICIENT_AUTHORITY`. Then *Run all attacks*:
   over-limit, double commit, idempotency-key reuse, commit after revocation, concurrent race (one VALID, the others
   `MVCC_READ_CONFLICT` in the same block).
6. AI + blockchain: on a new pending payment, *Run simulation* (review screen) → IsolationForest HIGH → containment →
   Drunix `Revoke` → the hold is `RELEASED` on-chain and any commit is refused.
7. **Reconciliation** table: AgentGuard's PostgreSQL pools and the on-chain pools match.

## 8. Tests

```bash
pytest -q                                              # 156 original + 19 integration tests (fake bridge), 8 live skipped
AGENTGUARD_DRUNIX_LIVE_BRIDGE=http://127.0.0.1:8090 pytest tests/test_drunix_live.py -v   # real Drunix
(cd drunix/chaincode-agentauth && go test ./... -cover)  # 23 chaincode unit tests
(cd drunix/bridge && go test ./...)                     # 12 bridge tests (real fabric-gateway client vs fake gRPC Gateway)
```

## 9. Security, scale, impact

* **Security.** Two independent layers with different trust bases: AgentGuard's keys/policy/ML and Drunix's replicated,
  endorsed state. Compromising AgentGuard's server is not enough to overspend — the chaincode checks every hold against
  the on-chain tree (demonstrated live by the Security Lab). Every authority change must be endorsed by both
  organisations' Lite Peers (Drunix's default majority policy) and validated by the Committing Peers. Credentials are
  generated at runtime and never committed.
* **Concurrency.** Two holds racing for the same authority cannot both commit: Drunix's MVCC validation invalidates the
  loser (observed live: 1 VALID, 2 `MVCC_READ_CONFLICT` in one block).
* **Scale.** Drunix separates endorsement (stateless Lite Peers, horizontally scalable) from validation (stateless
  validation services) and stores state in a distributed SQL database (YugabyteDB) — the properties a national-scale
  payment network needs. Each agent payment is two ledger transactions (reserve, commit).
* **Latency (measured).** ≈ 2.0 s per transaction on the local network, dominated by the orderer's 2 s block-cut time
  (`ORDERER_GENERAL_BLOCKCUTTIME=2`); reserve + commit add ≈ 4 s per payment, well inside the 90 s authorization window.
  Endorsement-time rejections return in ≈ 25 ms.
* **Payment infrastructure.** The simulated UTR is written on-chain with the commit, linking ledger and rail; a real rail
  (UPI) would settle only against a VALID `Commit`.
* **Impact.** An enterprise can give an AI agent real spending authority with a mathematically bounded, independently
  auditable worst case: the agent can never spend more than the on-chain capability, never twice, and never after
  revocation — regardless of bugs or compromise in the orchestration layer.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `/drunix` shows *Not connected* | `drunix/scripts/status.sh`; restart the bridge with `bridge.sh --background` |
| bridge health `chaincode_status: not_ready` | `drunix/scripts/deploy.sh` |
| peers log TLS handshake errors to a proxy address | a host `HTTPS_PROXY` leaked into gRPC; the scripts strip it, check your shell |
| `hostnames must resolve to 127.0.0.1` | add the `/etc/hosts` line from §6 |
| committing peer exits: `mkdir /var/hyperledger: permission denied` | fixed in `drunix_native.py` (snapshot dir under `drunix/.runtime`); pull the current scripts and re-run `up.sh` |
| `configtxlator: command not found` while setting anchor peers | fixed in `build.sh`; re-run `up.sh` — it builds `configtxlator` and sets any missing anchor peer |
| `/drunix` shows mode OFF / *not_configured* | `.env` lacks `DRUNIX_MODE=enforce` / `DRUNIX_BRIDGE_URL`; restart uvicorn after editing |
| `docker-compose: command not found` (docker runtime) | enable the compose v1 shim or use the native runtime |
| AgentGuard checkout fails with `DrunixUnavailableError` | intended fail-closed behaviour; bring Drunix back and retry |
| `SYNC_PENDING` rows | `POST /drunix/sync/retry` or wait for the sweeper (every `EXPIRY_SWEEP_SECONDS`) |

## 11. Known limitations

* The native runtime substitutes a local PostgreSQL for YugabyteDB (wire-compatible YSQL, same Drunix code path) and
  redis-server for KeyDB. The Docker runtime uses the real YugabyteDB/KeyDB images.
* Both organisations are run by one operator in the test network; access control trusts the Org1 client identity.
  Agent Ed25519 signatures are verified by AgentGuard, not on-chain.
* Authority created while `DRUNIX_MODE=off` is adopted on-chain at its current size when first used in enforce mode.
* Restrictive operations can briefly be ahead in AgentGuard (`SYNC_PENDING`); in that window Drunix is *more*
  permissive only for authority AgentGuard itself already refuses.
* The payment rail and marketplace are simulated.

## 12. Future work

On-chain verification of the agents' Ed25519 signatures and nonces; per-organisation roles (user's bank, AgentGuard,
merchant acquirer) with endorsement policies that require each; private data collections for merchant details; chaincode
events streamed to the dashboard; a real rail adapter that settles only against a VALID commit.
