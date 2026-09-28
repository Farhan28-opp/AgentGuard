#!/usr/bin/env python3
"""Drive the judges' demo through AgentGuard's HTTP API and print what
AgentGuard and Drunix actually did (nothing here is simulated client-side).

  1. An AI agent buys groceries (~₹2,450): signature -> policy -> IsolationForest
     -> Drunix Reserve -> user authorization -> Drunix Commit -> payment -> receipt
  2. The agent asks for more than its authority: AgentGuard's policy blocks it
  3. The same attack sent straight to Drunix, bypassing AgentGuard: the
     agentauth chaincode refuses it on its own
  4. (--lab) every Security Lab ledger-bypass attack

usage: python drunix/scripts/demo_flow.py [--base http://127.0.0.1:8001] [--lab] [--no-reset]
Uses only the Python standard library.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# ≈ ₹2,450 at DailyMart with the seeded simulated catalogue.
BASKET = [("aashirvaad-atta-5kg", 3), ("fortune-sunflower-oil-1l", 4), ("amul-toned-milk-1l", 10),
          ("tata-salt-1kg", 2), ("potato-1kg", 3)]


def call(base, method, path, body=None, timeout=180):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local server: never via a proxy
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def hr(title):
    print("\n" + "═" * 78 + f"\n  {title}\n" + "═" * 78)


def steps(task, since=0):
    for s in task["steps"][since:]:
        mark = {"completed": "✓", "failed": "✗", "skipped": "·"}.get(s["status"], "…")
        extra = ""
        if s.get("drunix_tx"):
            extra = f"   [tx {s['drunix_tx'][:16]}… block {s.get('drunix_block')}]"
        if s.get("drunix_code"):
            extra += f"   [Drunix: {s['drunix_code']}]"
        print(f"  {mark} {s['name']}{extra}")
    return len(task["steps"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--lab", action="store_true", help="also run every Security Lab attack")
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()
    base = a.base.rstrip("/")

    code, st = call(base, "GET", "/drunix/status")
    hr("Drunix status")
    print(f"  mode={st['mode']} connected={st['connected']} bridge={st['bridge']} "
          f"chaincode={st['chaincode_status']} channel={st['channel']} peer={st['peer_endpoint']}")
    if st["mode"] != "enforce" or not st["connected"]:
        print("  Drunix enforcement is not active — start the network and set DRUNIX_MODE=enforce.")
        sys.exit(1)

    if not a.no_reset:
        t0 = time.monotonic()
        code, _ = call(base, "POST", "/demo/reset")
        print(f"  demo reset: HTTP {code} ({time.monotonic() - t0:.1f}s — mandate, root and standing grants registered on Drunix)")

    hr("1 · AI agent purchase, enforced by Drunix")
    code, t = call(base, "POST", "/product/agent/tasks", {"instruction": "Buy groceries for me under ₹3,000"})
    tid = t["task_id"]
    for pid, q in BASKET:
        call(base, "POST", f"/product/agent/tasks/{tid}/cart/items", {"product_id": pid, "quantity": q})
    call(base, "POST", f"/product/agent/tasks/{tid}/merchant", {"mode": "user", "merchant_id": "dailymart"})
    t0 = time.monotonic()
    code, body = call(base, "POST", f"/product/agent/tasks/{tid}/execute")
    prep_s = time.monotonic() - t0
    task = body.get("task", body)
    auth = task.get("authorization") or {}
    print(f"  Agent requests ₹{auth.get('total')} at {auth.get('merchant')} (prepared in {prep_s:.1f}s)")
    n = steps(task)
    if task["status"] != "AWAITING_AUTHORIZATION":
        print(f"  unexpected status {task['status']}: {task.get('error')}")
        sys.exit(1)
    print(f"  checks: {auth['checks']}")

    t0 = time.monotonic()
    code, done = call(base, "POST", f"/product/agent/tasks/{tid}/authorize-payment")
    auth_s = time.monotonic() - t0
    done = done.get("task", done)
    print(f"\n  User authorizes (settled in {auth_s:.1f}s):")
    steps(done, n)
    r = done["result"]
    code, receipt = call(base, "GET", f"/product/orders/{r['order_number']}")
    d = receipt["drunix"]
    print(f"\n  Receipt {r['order_number']}  ₹{r['amount']}  payment ref {r['utr_reference']}")
    for k in ("delegate", "reserve", "commit", "return_unused"):
        if d.get(k):
            print(f"    Drunix {k:<13} tx {d[k]['tx_id'][:24]}…  block {d[k]['block_number']}  {d[k]['status']}  {d[k]['latency_ms']} ms")

    hr("2 · Agent asks for more than its authority — AgentGuard blocks it")
    code, t = call(base, "POST", "/product/agent/tasks", {"instruction": "Buy groceries for me under ₹3,000"})
    tid2 = t["task_id"]
    for pid, q in [("aashirvaad-atta-5kg", 6), ("fortune-sunflower-oil-1l", 6), ("amul-toned-milk-1l", 10)]:
        call(base, "POST", f"/product/agent/tasks/{tid2}/cart/items", {"product_id": pid, "quantity": q})
    code, body = call(base, "POST", f"/product/agent/tasks/{tid2}/merchant", {"mode": "user", "merchant_id": "dailymart"})
    if code == 200:
        code, body = call(base, "POST", f"/product/agent/tasks/{tid2}/execute")
    print(f"  HTTP {code}: {body.get('error') or 'rejected'} — {body.get('detail')}")
    print("  Nothing was delegated, reserved or sent to Drunix.")

    hr("3 · Same attack sent straight to Drunix, bypassing AgentGuard")
    code, lab = call(base, "POST", "/drunix/lab/live_capability")
    for s in lab.get("steps", []):
        print(f"  {s['outcome']:<20} {s.get('code', ''):<24} {s['note']}")
    print(f"  {lab.get('verdict')}: {lab.get('explanation')}")

    if a.lab:
        hr("4 · Security Lab — ledger-bypass attacks")
        for sc in ("over_limit", "double_commit", "idempotency_reuse", "commit_after_revoke", "concurrent_race"):
            t0 = time.monotonic()
            code, res = call(base, "POST", f"/drunix/lab/{sc}")
            print(f"\n  [{res.get('verdict')}] {res.get('title')} ({time.monotonic() - t0:.1f}s)")
            for s in res.get("steps", []):
                print(f"     {s['outcome']:<20} {s.get('code', '') or '':<24} {s['note']}"
                      + (f"  (block {s['block_number']})" if s.get("block_number") else ""))

    code, st = call(base, "GET", "/drunix/status")
    hr("Ledger journal")
    print(f"  {st['journal']}  avg VALID latency {st['latency_24h']['avg_ms']} ms  sync pending {st['sync_pending']}")


if __name__ == "__main__":
    main()
