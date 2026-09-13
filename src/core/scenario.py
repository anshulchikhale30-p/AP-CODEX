"""Deterministic scenario engine for compound, real-world tasks.

Manus-style demos need actions to actually happen.  When optionality is low —
"buy a shirt under ₹1,000, book an Uber for 8 PM, email my manager that I'm
late" — a scripted planner is faster, cheaper, and more reliable than an LLM.
This runner parses the intents out of a user prompt, builds ordered tool plans
against the built-in lifestyle connectors, and emits the exact same
``status / delta / tool / done / error`` event stream as :class:`AgentLoop`,
so the CLI-API-frontend pipeline is interchangeable.

If no known intent is detected the runner returns ``None`` and the caller
falls through to the LLM agent.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

from connectors import ConnectorManager
from connectors.rides import normalize_time

from .types import AgentResult, ToolExecution

_SKIP = object()  # sentinel: derived payload not applicable (e.g. no product)

_STOPWORDS = {"me", "a", "an", "the", "please", "and", "for", "of", "to", "on", "online", "at"}
_RETAILERS = {"amazon", "flipkart", "amazon.in", "myntra", "ajio", "online"}
_CATEGORY_HINTS = {
    "shirt": "shirts", "tshirt": "shirts", "shirts": "shirts", "tee": "shirts",
    "shoes": "shoes", "shoe": "shoes", "sneaker": "shoes", "sneakers": "shoes",
    "headphone": "electronics", "headphones": "electronics", "earbud": "electronics",
}


@dataclass
class Step:
    """One connector call in a plan.  ``payload`` may derive from prior results."""

    id: str
    connector: str
    action: str
    payload: Union[Dict[str, Any], Callable[[Dict[str, Any]], Any]]
    narrative: str
    label: str


@dataclass
class Plan:
    """An ordered group of steps + how to summarize the outcome."""

    kind: str
    steps: List[Step]

    def summarize(self, results: Dict[str, Any]) -> str:
        return _summarize(self.kind, results)


_MONEY = r"(?:under|below|less\s+than|within|upto|max)\s*(?:₹|rs\.?|inr\b)?\s*([\d][\d,]*(?:\.\d+)?)"
_TIME = r"(?:for|at|around|by)?\s*((?:\d{1,2}:\d{2}\s*(?:a\.?m|p\.?m)|\d{1,2}\s*(?:a\.?m|p\.?m)|\d{1,4}))"

_INTENTS = {
    "shopping": re.compile(r"\b(buy|order|purchase|shop\s+for|grab)\b", re.I),
    "rides": re.compile(r"\b(uber|cab|taxi|lyft|ride)\b", re.I),
    "email": re.compile(r"\b(e-?mail|mail|gmail|message)\b", re.I),
    "twitter": re.compile(r"\b(tweet|twitter)\b", re.I),
    "meet": re.compile(r"\b(google\s*meet|gmeet|(?:create|start|schedule|make)\s+(?:a\s+)?meet(?:ing)?)\b", re.I),
    "calendar": re.compile(r"\b(gcal|calendar|google\s*calendar|add\s+an?\s+event)\b", re.I),
}

_MANAGER_WORDS = {
    "manager", "manag", "maneger", "manger", "mgr", "mngr",
    "boss", "supervisor", "super", "team", "leader", "lead", "hr",
}


class ScenarioRunner:
    """Parse compound prompts and execute deterministic lifestyle plans."""

    def __init__(self, manager: ConnectorManager, outbox_path: Optional[str] = None):
        self._manager = manager
        self._outbox_path = outbox_path

    # -------------------------------------------------------------- public
    def matches(self, text: str) -> bool:
        return bool(self.parse(text))

    def parse(self, text: str) -> List[Plan]:
        plans: List[Plan] = []
        trigger = _INTENTS["shopping"].search(text)
        if trigger:
            plan = self._shopping_plan(text[trigger.end():])
            if plan:
                plans.append(plan)
        trigger = _INTENTS["rides"].search(text)
        if trigger:
            plan = self._rides_plan(text)
            if plan:
                plans.append(plan)
        trigger = _INTENTS["email"].search(text)
        if trigger:
            plan = self._email_plan(text[trigger.end():], self._email_transport())
            if plan:
                plans.append(plan)
        trigger = _INTENTS["twitter"].search(text)
        if trigger:
            plan = self._twitter_plan(text[trigger.end():])
            if plan:
                plans.append(plan)
        trigger = _INTENTS["meet"].search(text)
        if trigger:
            plan = self._meet_plan(text[trigger.end():])
            if plan:
                plans.append(plan)
        trigger = _INTENTS["calendar"].search(text)
        if trigger:
            plan = self._calendar_plan(text)
            if plan:
                plans.append(plan)
        return plans

    def _email_transport(self) -> str:
        """Prefer a signed-in Gmail connector, else the simulated/SMTP one."""
        try:
            gmail = self._manager._connectors.get("gmail")
        except AttributeError:
            return "email"
        return "gmail" if gmail is not None and getattr(gmail, "authorized", False) else "email"

    async def run(self, text: str) -> Optional[AgentResult]:
        """Execute plans to completion; returns None when nothing matched."""
        if not self.matches(text):
            return None
        reply: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        iterations = 0
        ok = True
        error = None
        async for ev in self.stream(text):
            kind = ev["kind"]
            if kind == "delta":
                reply.append(ev["text"])
            elif kind == "tool" and ev.get("status") in ("ok", "error"):
                tool_calls.append(ev)
            elif kind == "done":
                iterations = ev["iterations"]
                ok = ev["ok"]
                if ev["reply"]:
                    reply = [ev["reply"]]
            elif kind == "error":
                ok = False
                error = ev["message"]
        return AgentResult(
            reply="\n".join(reply).strip(),
            tool_calls=tool_calls,
            iteration_count=iterations,
            ok=ok,
            error=error,
        )

    async def stream(self, text: str) -> AsyncIterator[Dict[str, Any]]:
        """Yield the same event protocol as :class:`AgentLoop`."""
        plans = self.parse(text)
        if not plans:
            yield {"kind": "status", "state": "done"}
            yield _done("No actionable tasks found in your request.", [], 0)
            return

        yield {"kind": "status", "state": "parsing"}
        yield {"kind": "delta", "text": "I heard you. Breaking your request into tasks and executing them now."}

        results: Dict[str, Any] = {}
        executed: List[Dict[str, Any]] = []
        counter = 0
        try:
            for plan in plans:
                yield {"kind": "status", "state": "running"}
                for step in plan.steps:
                    counter += 1
                    payload = step.payload(results) if callable(step.payload) else step.payload
                    tool_id = f"scn-{plan.kind}-{counter}"

                    if payload is _SKIP:
                        yield {"kind": "delta", "text": step.narrative + " — nothing to do, skipped."}
                        continue

                    yield {"kind": "delta", "text": step.narrative}
                    yield {
                        "kind": "tool", "status": "started",
                        "name": f"{step.connector}.{step.action}",
                        "connector": step.connector, "action": step.action,
                        "tool_call_id": tool_id,
                    }

                    result = await asyncio.to_thread(
                        self._manager.execute_connector_action,
                        step.connector, step.action, payload,
                    )
                    results[step.id] = result
                    execution = ToolExecution(tool_id, f"{step.connector}.{step.action}",
                                              payload if payload is not _SKIP else {}, result)
                    if result.get("ok"):
                        executed.append(execution.summary())
                        yield {
                            "kind": "tool", "status": "ok",
                            "name": execution.name, "connector": step.connector,
                            "action": step.action, "tool_call_id": tool_id,
                            "result": execution.summary(),
                        }
                    else:
                        executed.append(execution.summary())
                        yield {
                            "kind": "tool", "status": "error",
                            "name": execution.name, "connector": step.connector,
                            "action": step.action, "tool_call_id": tool_id,
                            "result": execution.summary(),
                        }

            summary_lines = [plan.summarize(results) for plan in plans if plan.summarize(results)]
            all_ok = bool(executed) and all(e.get("ok") for e in executed)
            head = "All done." if all_ok else "Some tasks couldn't complete."
            final_reply = head + " " + " ".join(summary_lines)
            yield {"kind": "status", "state": "done"}
            yield _done(final_reply, executed, counter)
        except Exception as exc:  # noqa: BLE001
            yield {"kind": "error", "message": str(exc)}

    # ------------------------------------------------------------ planners
    @staticmethod
    def _shopping_plan(rest: str) -> Optional[Plan]:
        rest = _take_shopping_clause(rest)
        max_price, stripped = _extract_budget(rest)
        query = _clean_query(stripped)
        if not query:
            return None
        category = _category_for(query)
        step_search = Step("search", "shopping", "search",
                           {"query": query, "category": category, "max_price": max_price},
                           f"Searching for '{query}' on Amazon"
                           + (f" under ₹{max_price}" if max_price is not None else "") + "…",
                           label=f"shop:{query}")
        step_cart = Step("add_to_cart", "shopping", "add_to_cart",
                         lambda r: _best_product(r),
                         "Adding the best match to the cart…",
                         label="cart")
        step_checkout = Step("checkout", "shopping", "checkout",
                             lambda r: {} if (r.get("add_to_cart") or {}).get("ok") else _SKIP,
                             "Checking out your order…", label="checkout")
        return Plan("shopping", [step_search, step_cart, step_checkout])

    @staticmethod
    def _rides_plan(text: str) -> Optional[Plan]:
        trigger = _INTENTS["rides"].search(text)
        if not trigger:
            return None
        clause = text[trigger.start():]
        for pattern in (r"\b(?:e-?mail|mail|message)\b", r"\b(?:then|also)\b",
                        r"\s*,\s*", r"\s+and\s+"):
            match = re.search(pattern, clause, re.I)
            if match:
                clause = clause[:match.start()]
        time_match = re.search(_TIME, clause, re.I)
        pickup_time = normalize_time(time_match.group(1)) if time_match else None
        pickup, dropoff = _extract_route(clause)
        if pickup_time is None and pickup == "Home" and dropoff == "Office":
            pass  # still a valid ride request; defaults apply
        step_est = Step("estimate", "rides", "estimate",
                        {"pickup": pickup, "dropoff": dropoff, "pickup_time": pickup_time},
                        f"Booking a ride from {pickup} to {dropoff}"
                        + (f" for {pickup_time}" if pickup_time else "") + "…",
                        label="rides")
        step_book = Step("book", "rides", "book",
                         lambda r: _from_estimate(r, "estimate"),
                         "Confirming the ride with your driver…",
                         label="book")
        return Plan("rides", [step_est, step_book])

    @staticmethod
    def _twitter_plan(rest: str) -> Optional[Plan]:
        tail = re.sub(r"^\s*please\s+(?:post|tweet)\s+(?:on|to)\s+(?:twitter|x)\b",
                      "", rest, flags=re.I)
        tail = re.sub(r"^\s*(?:that|saying)\s+", "", tail, flags=re.I)
        text = tail.strip().strip(":,; \t\n\"'").strip()
        if not text:
            return None
        if text and text[:1].islower():
            text = text[:1].upper() + text[1:]
        if len(text) > 280:
            text = text[:277] + "..."
        step_tweet = Step("post_tweet", "twitter", "post_tweet",
                          {"text": text},
                          f"Posting your tweet…",
                          label="twitter")
        return Plan("twitter", [step_tweet])

    @staticmethod
    def _meet_plan(rest: str) -> Optional[Plan]:
        title = rest.strip().strip(":,; \t\n\"'").strip()
        title = re.sub(r"^\s*(link\s+for|link\s+to|for|to|about|with)\b", "", title, flags=re.I).strip()
        if not title:
            title = "Quick meeting"
        if len(title) > 120:
            title = title[:117] + "..."
        start = _parse_start(rest)
        step_meet = Step("create_space", "meet", "create_space",
                         {"title": title},
                         f"Creating a Google Meet for \"{title}\"…",
                         label="meet")
        step_schedule = Step("create_event", "calendar", "create_event",
                             lambda r: _meet_schedule_payload(title, start, r),
                             "Scheduling it on your Google Calendar…",
                             label="calendar")
        return Plan("meet", [step_meet, step_schedule])

    @staticmethod
    def _calendar_plan(text: str) -> Optional[Plan]:
        rest = re.sub(_INTENTS["calendar"], " ", text)
        rest = re.sub(r"\badd\s+an?\s+event\b", "", rest, flags=re.I)
        rest = re.sub(r"\b(on|to|in)\s+my\s+calendar\b", "", rest, flags=re.I)
        rest = re.sub(r"^\s*add\s+(?:a|an)\s+", "", rest, flags=re.I)
        rest = re.sub(r"\bto\s+my\b", "", rest, flags=re.I)
        rest = " ".join(rest.split()).strip(" ,:;")
        if not rest:
            return None
        lowered = rest.lower()
        now = datetime.now(timezone.utc)
        if "tomorrow" in lowered:
            start = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            start = (now.replace(second=0, microsecond=0)) + timedelta(hours=1)
        step_event = Step("create_event", "calendar", "create_event",
                          {"summary": rest, "start": start.isoformat(), "duration_min": 30},
                          f"Adding \"{rest}\" to your calendar…",
                          label="calendar")
        return Plan("calendar", [step_event])

    @staticmethod
    def _email_plan(rest: str, transport: str = "email") -> Optional[Plan]:
        to, subject_text = _extract_recipient(rest)
        body = _extract_reason(rest) or "I'm running late today."
        subject = _make_subject(body)
        step_send = Step("send", transport, "send",
                         {"to": to, "subject": subject, "body": body},
                         f"Emailing {to} — \"{subject}\"…",
                         label="email")
        return Plan("email", [step_send])


# --------------------------------------------------------------- helpers
def _take_shopping_clause(text: str) -> str:
    """Trim everything from the next ride/email intent or an 'and' onwards."""
    for pattern in (r"\b(?:uber|cab|taxi|lyft|ride)\b",
                    r"\b(?:e-?mail|mail|message)\b",
                    r"\b(?:also|then)\b"):
        match = re.search(pattern, text, re.I)
        if match:
            text = text[:match.start()]
    # drop a trailing action clause like ", book an Uber..." / ", and email..."
    text = re.sub(r"\s*,\s*(?:and\s+)?(?:book|reserve|schedule|arrange|hail|order|email|mail)\b.*$",
                  " ", text, flags=re.I)
    text = re.sub(r"\s*,\s*(?:and|then|also)?\s*$", " ", text)
    text = re.split(r"\s+\band\b\s+", text, maxsplit=1, flags=re.I)[0]
    return text


def _extract_budget(text: str) -> tuple:
    match = re.search(_MONEY, text, re.I)
    if not match:
        return None, text
    amount = int(match.group(1).replace(",", ""))
    return amount, text[:match.start()] + " " + text[match.end():]


def _clean_query(text: str) -> str:
    lowered = text.lower()
    for retailer in _RETAILERS:
        lowered = re.sub(rf"\bfrom\s+{re.escape(retailer)}\b", " ", lowered)
        lowered = re.sub(rf"\b{re.escape(retailer)}\b", " ", lowered)
    lowered = re.sub(r"\bfrom\s+[a-z0-9.'\-]+", " ", lowered)
    lowered = re.sub(r"\bunder\b|\bbelow\b|\ble[cs]s\s+than\b", " ", lowered)
    lowered = re.sub(r"[₹,;:.\-]+", " ", lowered)
    tokens = [w for w in lowered.split() if w and w not in _STOPWORDS]
    return " ".join(tokens)


def _category_for(query: str) -> Optional[str]:
    first = query.split()[0] if query.split() else ""
    return _CATEGORY_HINTS.get(first.strip().lower())


def _best_product(results: Dict[str, Any]):
    data = (results.get("search") or {}).get("data") or {}
    best = data.get("best") or {}
    if best.get("id"):
        return {"product_id": best["id"], "quantity": 1}
    return _SKIP


def _from_estimate(results: Dict[str, Any], step_id: str):
    data = (results.get(step_id) or {}).get("data") or {}
    return {"estimate_id": data.get("estimate_id"), "pickup_time": data.get("pickup_time")}


def _extract_route(text: str) -> tuple:
    pickup, dropoff = "Home", "Office"
    fm = re.search(r"\bfrom\s+([A-Za-z][\w\s'.-]{0,24}?)(?=,\s|\s+(?:to|for|at|and)\b|$)", text, re.I)
    if fm:
        pickup = fm.group(1).strip()
    to = re.search(r"\bto\s+([A-Za-z][\w\s'.-]{0,24}?)(?=,\s|\s+(?:for|at|and)\b|$)", text, re.I)
    if to:
        dropoff = to.group(1).strip()
    return pickup or "Home", dropoff or "Office"


def _extract_recipient(rest: str) -> tuple:
    rest = rest.strip().lstrip(", ")
    role = re.search(r"\b(my|the|his|her|their)\s+(\w+)\b", rest, re.I)
    if role:
        word = role.group(2).lower().rstrip("s") or role.group(2).lower()
        if _is_manager_word(word):
            return "manager@example.com", rest
    addr = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", rest)
    if addr:
        return addr.group(0), rest.replace(addr.group(0), " ")
    head = re.split(r"\b(?:that|about|saying|re:)\b", rest, maxsplit=1, flags=re.I)[0].strip()
    recipient = head.strip(" ,") or "manager@example.com"
    if recipient and recipient.lower().startswith("my "):
        recipient = recipient[3:].strip()
    first = recipient.split()[0] if recipient.split() else "manager@example.com"
    if _is_manager_word(first.lower().rstrip("s")):
        first = "manager@example.com"
    return first, rest


def _is_manager_word(word: str) -> bool:
    lowered = (word or "").lower().rstrip("s")
    return lowered in _MANAGER_WORDS or (
        lowered.startswith("manag") and len(lowered) >= 5
    )


def _extract_reason(rest: str) -> Optional[str]:
    match = re.search(r"\b(?:that|saying\s+that|about|saying)\s+(.+)$", rest, re.I)
    if match:
        return match.group(1).strip(" .")
    return None


def _make_subject(body: str) -> str:
    lowered = body.strip().lower()
    if lowered.startswith(("i'm late", "i am late", "running late", "will be late")):
        return "Late arrival"
    words = body.strip().split()
    if not words:
        return "Update from AP-CODEX"
    if len(words) <= 5:
        return " ".join(words)[:60]
    return " ".join(words[:5]).rstrip(",") + "\u2026"


def _parse_start(text: str) -> str:
    """Best-effort schedule time from free text -> ISO 8601 string (UTC)."""
    lowered = text.lower()
    base = datetime.now(timezone.utc)
    if "tomorrow" in lowered:
        base = (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "today" in lowered or re.search(r"\b(at|by|around)\s+\d", text, re.I):
        base = base.replace(minute=0, second=0, microsecond=0)
    else:
        base = base.replace(second=0, microsecond=0) + timedelta(hours=1)
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?\s*m|p\.?\s*m)?\b", text, re.I)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower().replace(".", "").replace(" ", "")
        if meridiem:
            if meridiem.startswith("p") and hour < 12:
                hour += 12
            elif meridiem.startswith("a") and hour == 12:
                hour = 0
        base = base.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    return base.isoformat()


def _meet_schedule_payload(title: str, start: str, results: Dict[str, Any]) -> Dict[str, Any]:
    """Calendar event payload that attaches the Meet space created earlier in the plan."""
    space = (results.get("create_space") or {}).get("data") or {}
    uri = space.get("meeting_uri")
    payload: Dict[str, Any] = {
        "summary": title,
        "start": start,
        "duration_min": 30,
        "description": f"Google Meet: {uri or 'created via AP-CODEX'}",
    }
    if uri:
        payload["conference_url"] = uri
        payload["conference_code"] = space.get("meeting_code")
    return payload


def _summarize(kind: str, results: Dict[str, Any]) -> str:
    if kind == "shopping":
        checkout = results.get("checkout") or {}
        data = checkout.get("data") or {}
        if data.get("order_id"):
            return (f"Shopping order {data['order_id']} placed for "
                    f"₹{data['total']} ({len(data.get('items') or [])} item(s)).")
        search = results.get("search") or {}
        if not (search.get("data") or {}).get("best"):
            return "No matching item was found within budget."
        return "Shopping complete."
    if kind == "rides":
        book = results.get("book") or {}
        data = book.get("data") or {}
        if data.get("booking_id"):
            return (f"Ride {data['booking_id']} confirmed with {data.get('driver')} "
                    f"for {data.get('pickup_time') or 'the requested time'}.")
        return "Ride couldn't be booked."
    if kind == "email":
        send = results.get("send") or {}
        data = send.get("data") or {}
        if data.get("message_id"):
            return f"Email sent to {data.get('to')} — done."
        return "Email couldn't be sent."
    if kind == "twitter":
        post = results.get("post_tweet") or {}
        data = post.get("data") or {}
        if data.get("tweet_id"):
            text = str(data.get("text") or "")
            return f"Tweet posted (id {data['tweet_id']}): \"{text[:60]}\" — done."
        return "Tweet couldn't be posted."
    if kind == "meet":
        event = results.get("create_event") or {}
        edata = event.get("data") or {}
        space = results.get("create_space") or {}
        sdata = space.get("data") or {}
        if edata.get("event_id"):
            return (f"Google Meet created & scheduled — done: {sdata.get('meeting_uri')} "
                    f"(attached to your calendar, event {edata.get('event_id')}).")
        if sdata.get("meeting_uri"):
            return (f"Google Meet created — done: {sdata.get('meeting_uri')} "
                    f"(calendar not attached).")
        return "Meet couldn't be created."
    if kind == "calendar":
        event = results.get("create_event") or {}
        data = event.get("data") or {}
        if data.get("event_id"):
            return f"Added to your calendar — done: {data.get('summary')} at {data.get('start')}."
        return "Calendar event couldn't be created."
    return "Task complete."


def _done(reply: str, executed: List[dict], iterations: int) -> dict:
    return {
        "kind": "done",
        "reply": reply or "",
        "tool_calls": [e for e in executed if e.get("status") in ("ok", "error")],
        "iterations": iterations,
        "ok": True,
    }