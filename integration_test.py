#!/usr/bin/env python3
"""Full local integration test: push real dataset, tick, and drive replies."""
import json
import time
from pathlib import Path
from urllib import request as rq

BASE = "http://127.0.0.1:8080"
DATA = Path(__file__).parent / "expanded"


def post(path, body):
    req = rq.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with rq.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get(path):
    with rq.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def push_all():
    n = 0
    for f in (DATA / "categories").glob("*.json"):
        d = json.load(open(f))
        r = post("/v1/context", {"scope": "category", "context_id": d["slug"], "version": 1, "payload": d, "delivered_at": "2026-04-26T10:00:00Z"})
        assert r["accepted"], r
        n += 1
    for f in (DATA / "merchants").glob("*.json"):
        d = json.load(open(f))
        r = post("/v1/context", {"scope": "merchant", "context_id": d["merchant_id"], "version": 1, "payload": d, "delivered_at": "2026-04-26T10:00:00Z"})
        assert r["accepted"], r
        n += 1
    for f in (DATA / "customers").glob("*.json"):
        d = json.load(open(f))
        r = post("/v1/context", {"scope": "customer", "context_id": d["customer_id"], "version": 1, "payload": d, "delivered_at": "2026-04-26T10:00:00Z"})
        assert r["accepted"], r
        n += 1
    for f in (DATA / "triggers").glob("*.json"):
        d = json.load(open(f))
        r = post("/v1/context", {"scope": "trigger", "context_id": d["id"], "version": 1, "payload": d, "delivered_at": "2026-04-26T10:00:00Z"})
        assert r["accepted"], r
        n += 1
    print(f"pushed {n} contexts")


def main():
    print("--- healthz before push ---")
    print(get("/v1/healthz"))

    push_all()

    print("--- healthz after push (should show 5/50/200/100) ---")
    print(get("/v1/healthz"))

    # Idempotency check: re-push same version -> should still be accepted (no-op), not error
    dentist = json.load(open(DATA / "categories" / "dentists.json"))
    r = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 1, "payload": dentist, "delivered_at": "x"})
    print("--- idempotent re-push same version ---")
    print(r)

    # Stale version check
    r = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 0, "payload": dentist, "delivered_at": "x"})
    print("--- stale version push (v0 after v1) ---")
    print(r)

    # ---- Flagship flow 1: research_digest for Dr. Meera ----
    print("\n=== TICK: research_digest (Dr. Meera) ===")
    r = post("/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_001_research_digest_dentists"]})
    print(json.dumps(r, indent=2, ensure_ascii=False))
    conv_id_research = r["actions"][0]["conversation_id"]

    # Re-tick with the SAME trigger -> must be suppressed (no duplicate action)
    print("\n=== TICK AGAIN: same trigger should be suppressed ===")
    r2 = post("/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_001_research_digest_dentists"]})
    print(json.dumps(r2, indent=2))
    assert r2["actions"] == [], "suppression_key dedupe failed!"

    # ---- Flagship flow 2: recall_due for Priya (customer-facing) ----
    print("\n=== TICK: recall_due (Priya, customer-facing) ===")
    r = post("/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_003_recall_due_priya"]})
    print(json.dumps(r, indent=2, ensure_ascii=False))
    conv_id_recall = r["actions"][0]["conversation_id"] if r["actions"] else None

    # ---- Generic fallback: some other trigger kind ----
    print("\n=== TICK: unknown/other trigger kinds (generic fallback + unseen id) ===")
    r = post("/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_002_compliance_dci_radiograph", "trg_does_not_exist"]})
    print(json.dumps(r, indent=2, ensure_ascii=False))

    # ---- Reply flow: intent transition ----
    print("\n=== REPLY: merchant says 'yes' -> should route to action, not re-ask ===")
    r = post("/v1/reply", {"conversation_id": conv_id_research, "merchant_id": "m_001_drmeera_dentist_delhi",
                            "customer_id": None, "from_role": "merchant", "message": "Yes please, send it",
                            "received_at": "2026-04-26T10:40:00Z", "turn_number": 2})
    print(r)
    assert r["action"] == "send" and "?" not in r["body"], "should route to action, not ask another question"

    # ---- Reply flow: auto-reply detection (same text 3x) ----
    print("\n=== REPLY: same canned auto-reply text sent 3 times ===")
    conv2 = "conv_autoreply_test"
    canned = "Thank you for contacting us. We will get back to you shortly."
    for i in range(1, 4):
        r = post("/v1/reply", {"conversation_id": conv2, "merchant_id": "m_002_studio11_salon_hyderabad",
                                "customer_id": None, "from_role": "merchant", "message": canned,
                                "received_at": "2026-04-26T10:4{}:00Z".format(i), "turn_number": i})
        print(f"  turn {i}: {r}")
    assert r["action"] == "end", "should have detected auto-reply and ended by 3rd repeat"

    # ---- Reply flow: decline ----
    print("\n=== REPLY: explicit decline ===")
    r = post("/v1/reply", {"conversation_id": "conv_decline_test", "merchant_id": "m_003_pizzajunction_restaurant_delhi",
                            "customer_id": None, "from_role": "merchant", "message": "Not interested, please stop",
                            "received_at": "2026-04-26T10:50:00Z", "turn_number": 1})
    print(r)
    assert r["action"] == "end"

    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
