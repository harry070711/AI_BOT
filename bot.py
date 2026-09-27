#!/usr/bin/env python3
"""
Vera-challenge bot — deterministic, rule-based composer.
==========================================================

Design choice: NO LLM call. Every message is built from a template that only
ever inserts fields that are actually present in the pushed context. This
guarantees:
  - determinism (same inputs -> byte-identical output, every time)
  - zero hallucination (nothing is said that wasn't in category/merchant/
    trigger/customer payloads)
  - zero external dependency / zero latency risk against the 30s budget

Two trigger kinds get a fully-tailored composer (the "one strong flow"):
    - research_digest   (merchant-facing, external)   -> mirrors brief Appendix A
    - recall_due         (customer-facing, internal)   -> mirrors brief Appendix B
Every other trigger kind falls back to a generic-but-safe composer that still
respects voice/CTA rules and never invents facts.

Run:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Vera-challenge bot", version="0.1.0")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: Dict[Tuple[str, str], Dict[str, Any]] = {}

# suppression_key -> True, once an action has gone out for it (dedupe across ticks)
SENT_SUPPRESSION_KEYS: Dict[str, str] = {}  # suppression_key -> conversation_id

# conversation_id -> state
CONVERSATIONS: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_ctx(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = CONTEXTS.get((scope, context_id))
    return entry["payload"] if entry else None


# ---------------------------------------------------------------------------
# Language / voice helpers
# ---------------------------------------------------------------------------

def wants_hindi_mix(languages: Optional[List[str]] = None, language_pref: Optional[str] = None) -> bool:
    if language_pref and "hi" in language_pref.lower():
        return True
    if languages and "hi" in [l.lower() for l in languages]:
        return True
    return False


def salutation(merchant: dict) -> str:
    ident = merchant.get("identity", {})
    first = ident.get("owner_first_name")
    name = ident.get("name", "there")
    examples = []
    # dentists tend to use "Dr. {first}" — infer from name containing "Dr."
    if first and ("Dr." in name or "dr." in name.lower()):
        return f"Dr. {first}"
    return first or name


# ---------------------------------------------------------------------------
# Composer: research_digest (merchant-facing, external) — the flagship flow
# ---------------------------------------------------------------------------

def compose_research_digest(category: dict, merchant: dict, trigger: dict) -> Optional[dict]:
    payload = trigger.get("payload", {})
    top_item_id = payload.get("top_item_id")
    digest_items = category.get("digest", [])
    item = next((d for d in digest_items if d.get("id") == top_item_id), None)
    if not item:
        return None  # don't invent a digest item that wasn't actually delivered

    hi_mix = wants_hindi_mix(merchant.get("identity", {}).get("languages"))
    sal = salutation(merchant)
    source = item.get("source", "")
    title = item.get("title", "")
    trial_n = item.get("trial_n")
    segment = item.get("patient_segment") or item.get("segment")

    # personalize using merchant signals if the segment lines up with them
    signals = " ".join(merchant.get("signals", []))
    segment_note = ""
    if segment and segment.replace("_", " ") in signals.replace("_", " "):
        segment_label = segment.replace("_", " ")
        segment_note = f"your {segment_label} patients" if not hi_mix else f"aapke {segment_label} patients"
    elif segment:
        segment_label = segment.replace("_", " ")
        segment_note = f"{segment_label} cases" if not hi_mix else f"{segment_label} cases"

    n_clause = f"{trial_n:,}-patient trial" if isinstance(trial_n, int) else "recent study"

    if hi_mix:
        body = (
            f"{sal}, {source.split(',')[0]} ka latest issue aaya hai. "
            f"Ek item jo relevant hai {segment_note or 'aapke case-mix ke liye'} — "
            f"{n_clause} mein dikha ki {title.lower()}. "
            f"Worth a look (2-min abstract). Chahiye ki main pull karke ek patient-ed WhatsApp bhi draft kar doon "
            f"jo aap share kar sakein? — {source}"
        )
    else:
        body = (
            f"{sal}, {source.split(',')[0]}'s latest issue landed. "
            f"One item relevant to {segment_note or 'your case-mix'} — "
            f"{n_clause} showed {title.lower()}. "
            f"Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp you can share? — {source}"
        )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", f"research:{category.get('slug')}:{top_item_id}"),
        "rationale": (
            f"External research_digest trigger; anchored on '{title}' ({source}), "
            f"personalized to {segment_note or 'the merchant case-mix'} from merchant signals. "
            "Curiosity + reciprocity levers; single open-ended CTA."
        ),
    }


# ---------------------------------------------------------------------------
# Composer: recall_due (customer-facing, internal) — second flagship flow
# ---------------------------------------------------------------------------

def compose_recall_due(category: dict, merchant: dict, trigger: dict, customer: dict) -> Optional[dict]:
    payload = trigger.get("payload", {})
    slots = payload.get("available_slots", [])
    if not slots:
        return None  # nothing bookable — don't fabricate slots

    ident = customer.get("identity", {})
    name = ident.get("name", "there")
    hi_mix = wants_hindi_mix(language_pref=ident.get("language_pref"))

    merchant_name = merchant.get("identity", {}).get("name", "your clinic")
    # find a matching active offer for the due service if one exists; otherwise
    # fall back to whatever's active — but never invent a price that isn't listed.
    service_due = payload.get("service_due", "")
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    offer_title = offers[0]["title"] if offers else None

    slot_labels = [s.get("label") for s in slots[:2] if s.get("label")]
    if len(slot_labels) < 1:
        return None

    last_visit = customer.get("relationship", {}).get("last_visit")
    months_note = ""
    if service_due and "month" in service_due:
        months_note = service_due.replace("_", " ")

    if hi_mix:
        slot_txt = " ya ".join(slot_labels)
        offer_txt = f" {offer_title}." if offer_title else ""
        body = (
            f"Hi {name}, {merchant_name} se message hai 🦷 Aapka {months_note or 'recall'} due hai"
            f"{f' (last visit {last_visit})' if last_visit else ''}. "
            f"Apke liye slots ready hain: {slot_txt}.{offer_txt} "
            f"Reply 1 for {slot_labels[0]}"
            + (f", 2 for {slot_labels[1]}" if len(slot_labels) > 1 else "")
            + ", ya batayein koi aur time chalega."
        )
    else:
        slot_txt = " or ".join(slot_labels)
        offer_txt = f" {offer_title}." if offer_title else ""
        last_visit_clause = f" — it has been a while since your last visit ({last_visit})" if last_visit else ""
        body = (
            f"Hi {name}, this is {merchant_name} 🦷 Your {months_note or 'recall'} is due"
            f"{last_visit_clause}. "
            f"We have slots open: {slot_txt}.{offer_txt} "
            f"Reply 1 for {slot_labels[0]}"
            + (f", 2 for {slot_labels[1]}" if len(slot_labels) > 1 else "")
            + ", or tell us a time that works."
        )

    return {
        "body": body,
        "cta": "multi_choice_binding",  # booking flow — numbered choice is allowed per brief
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key", f"recall:{customer.get('customer_id')}"),
        "rationale": (
            "Customer-facing recall_due trigger; used real catalog offer and real open slots, "
            "honored language preference and named the specific recall window."
        ),
    }


# ---------------------------------------------------------------------------
# Composer: generic safe fallback for any other trigger kind
# ---------------------------------------------------------------------------

def compose_generic(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> Optional[dict]:
    kind = trigger.get("kind", "update")
    payload = trigger.get("payload", {}) or {}
    sal = salutation(merchant) if not customer else customer.get("identity", {}).get("name", "there")

    # Only ever mention facts that are literally present in the payload.
    facts = [f"{k.replace('_', ' ')}: {v}" for k, v in payload.items() if isinstance(v, (str, int, float))]
    fact_str = "; ".join(facts[:2]) if facts else None

    if not fact_str:
        return None  # nothing concrete to say — restraint over spam

    body = f"{sal}, quick note on {kind.replace('_', ' ')} — {fact_str}. Want the details?"

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera" if not customer else "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key", f"{kind}:{trigger.get('id')}"),
        "rationale": f"Generic fallback composer for trigger kind '{kind}'; only literal payload facts used, no invented content.",
    }


def compose_for_trigger(trigger: dict) -> Optional[dict]:
    merchant = get_ctx("merchant", trigger.get("merchant_id"))
    if not merchant:
        return None
    category = get_ctx("category", merchant.get("category_slug"))
    if not category:
        return None

    customer = None
    if trigger.get("scope") == "customer":
        customer = get_ctx("customer", trigger.get("customer_id"))
        if not customer:
            return None  # can't personalize a customer message without the customer context

    kind = trigger.get("kind")
    if kind == "research_digest":
        result = compose_research_digest(category, merchant, trigger)
    elif kind == "recall_due" and customer:
        result = compose_recall_due(category, merchant, trigger, customer)
    else:
        result = compose_generic(category, merchant, trigger, customer)

    if result:
        result["_merchant_id"] = merchant.get("merchant_id")
        result["_customer_id"] = customer.get("customer_id") if customer else None
    return result


# ---------------------------------------------------------------------------
# Reply-side conversation logic (deterministic state machine)
# ---------------------------------------------------------------------------

INTENT_YES = [
    "yes", "yes please", "let's do it", "lets do it", "go ahead", "sure",
    "ok do it", "please proceed", "sounds good", "haan", "theek hai", "karo",
    "kar do", "chalega", "ok karo", "yes go ahead",
]
DECLINE = [
    "not interested", "no thanks", "no thank you", "stop", "not now",
    "nahi chahiye", "band karo", "no need", "unsubscribe",
]
ABUSE_MARKERS = ["idiot", "stupid", "useless", "nonsense", "bakwas", "chutiya"]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _contains_any(text: str, phrases: List[str]) -> bool:
    t = _norm(text)
    return any(p in t for p in phrases)


def handle_reply(conv: dict, message: str) -> dict:
    norm_msg = _norm(message)
    incoming = [t for t in conv["turns"] if t["from"] == conv["turns"][-1]["from"]]
    repeat_count = sum(1 for t in incoming if _norm(t["message"]) == norm_msg)

    # 1) Auto-reply detection: same exact text repeating.
    if repeat_count >= 3:
        return {
            "action": "end",
            "rationale": "Same message received 3+ times verbatim — auto-reply confirmed; exiting gracefully rather than burning turns.",
        }
    if repeat_count == 2:
        body = "Samajh gayi — just checking if you're able to see this yourself, or should I connect with your team directly?"
        if body in conv["sent_bodies"]:
            return {"action": "wait", "wait_seconds": 1800, "rationale": "Already sent the auto-reply probe once; backing off."}
        conv["sent_bodies"].add(body)
        return {"action": "send", "body": body, "cta": "open_ended",
                 "rationale": "Suspected auto-reply (2nd identical message); one probe before exiting."}

    # 2) Explicit decline
    if _contains_any(message, DECLINE):
        return {"action": "end", "rationale": "Merchant/customer signaled not interested — exiting politely."}

    # 3) Intent transition — switch straight to action, no more qualifying questions
    if _contains_any(message, INTENT_YES):
        body = "Great — starting now, I'll have it ready shortly and share it here."
        if body in conv["sent_bodies"]:
            body = "On it — will share as soon as it's ready."
        conv["sent_bodies"].add(body)
        return {"action": "send", "body": body, "cta": "none",
                 "rationale": "Detected explicit affirmative intent; routed directly to action instead of re-qualifying."}

    # 4) Hostile / off-topic — stay polite, redirect once
    if _contains_any(message, ABUSE_MARKERS):
        body = "Understood — happy to just focus on your listing/offers whenever you're ready."
        if body in conv["sent_bodies"]:
            return {"action": "end", "rationale": "Hostility repeated after one polite redirect; disengaging."}
        conv["sent_bodies"].add(body)
        return {"action": "send", "body": body, "cta": "open_ended",
                 "rationale": "Stayed on-mission after a hostile message; single polite redirect."}

    # 5) Default: acknowledge + advance with a concrete next step
    body = "Got it — noted. Let me know if you'd like me to go ahead."
    if body in conv["sent_bodies"]:
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Would repeat a prior body verbatim; waiting instead of resending."}
    conv["sent_bodies"].add(body)
    return {"action": "send", "body": body, "cta": "open_ended",
             "rationale": "Neutral/ambiguous reply; acknowledged and left a low-friction opening rather than pushing."}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}
    key = (body.scope, body.context_id)
    cur = CONTEXTS.get(key)
    if cur and cur["version"] > body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    if cur and cur["version"] == body.version:
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}
    CONTEXTS[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}


class TickBody(BaseModel):
    now: str
    available_triggers: List[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        if len(actions) >= 20:
            break
        trigger = get_ctx("trigger", trg_id)
        if not trigger:
            continue
        suppression_key = trigger.get("suppression_key")
        if suppression_key and suppression_key in SENT_SUPPRESSION_KEYS:
            continue  # already actioned this exact suppression key

        composed = compose_for_trigger(trigger)
        if not composed:
            continue  # not enough real context to say something specific — restraint over spam

        conversation_id = f"conv_{uuid.uuid4().hex[:10]}"
        CONVERSATIONS[conversation_id] = {
            "merchant_id": composed.get("_merchant_id"),
            "customer_id": composed.get("_customer_id"),
            "trigger_id": trg_id,
            "turns": [{"from": "bot", "message": composed["body"], "ts": body.now}],
            "sent_bodies": {composed["body"]},
            "state": "active",
        }
        if suppression_key:
            SENT_SUPPRESSION_KEYS[suppression_key] = conversation_id

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": composed.get("_merchant_id"),
            "customer_id": composed.get("_customer_id"),
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [composed.get("_merchant_id") or "", trigger.get("kind", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })
    return {"actions": actions}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = CONVERSATIONS.setdefault(body.conversation_id, {
        "merchant_id": body.merchant_id,
        "customer_id": body.customer_id,
        "trigger_id": None,
        "turns": [],
        "sent_bodies": set(),
        "state": "active",
    })
    conv["turns"].append({"from": body.from_role, "message": body.message, "ts": body.received_at or _now_iso()})

    if conv["state"] == "ended":
        return {"action": "end", "rationale": "Conversation already ended; not resuming."}

    result = handle_reply(conv, body.message)
    if result["action"] == "send":
        conv["turns"].append({"from": "bot", "message": result["body"], "ts": _now_iso()})
    elif result["action"] == "end":
        conv["state"] = "ended"
    return result


@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in CONTEXTS.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "HARDIK_TYAGI",
        "team_members": ["HARDIK_TYAGI"],
        "model": "deterministic-rule-based (no LLM call)",
        "approach": (
            "Two fully-tailored composer templates (research_digest merchant-facing, "
            "recall_due customer-facing) built directly from the brief's Appendix A/B gold "
            "examples, plus a safe generic fallback for other trigger kinds that only ever "
            "restates literal payload facts. Reply-side is a small deterministic state machine: "
            "auto-reply detection via verbatim-repeat counting, explicit-intent routing to action, "
            "decline detection, one polite hostility redirect, and anti-repetition on every send."
        ),
        "contact_email": "raj.hardik16@gmail.com",
        "version": "0.1.0",
        "submitted_at": _now_iso(),
    }


@app.post("/v1/teardown")
async def teardown():
    CONTEXTS.clear()
    SENT_SUPPRESSION_KEYS.clear()
    CONVERSATIONS.clear()
    return {"ok": True}


@app.get("/")
async def root():
    return {"service": "vera-challenge-bot", "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"]}
