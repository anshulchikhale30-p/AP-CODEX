"""Ride-hailing connector for AP-CODEX (Uber-style demo).

Provides fare estimates and confirmed bookings against an in-memory booking
list.  Deterministic fares; ``pickup_time`` can be a friendly string like
``"20:00"`` or ``"8:00 PM"`` — it is stored verbatim alongside the booking.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from typing import Any, Dict, List, Optional

_BASE_OUT = 149
_PER_KM = 11
_PROVIDER = "Uber"


class RideClient:
    """A deterministic taxi/cab simulator with estimate + book + status."""

    id = "rides"
    name = "Uber-style ride hailing"

    _counter = itertools.count(1)
    _DRIVERS = ["Arjun", "Sanjay", "Priya", "Vikram", "Meera"]

    def __init__(self):
        self._bookings: List[Dict[str, Any]] = []

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("estimate",
                       "Estimate the fare and ETA for a ride.",
                       {"pickup": {"type": "string", "description": "Pickup address (default 'Home')."},
                        "dropoff": {"type": "string", "description": "Destination (default 'Office')."},
                        "pickup_time": {"type": "string", "description": "Requested time, e.g. '20:00' or '8:00 PM'."},
                        "distance_km": {"type": "number", "description": "Trip distance to estimate with."}}),
            self._tool("book",
                       "Confirm a ride from an estimate id.",
                       {"estimate_id": {"type": "string", "description": "Id returned by estimate."},
                        "pickup_time": {"type": "string", "description": "Requested pickup time."}}),
            self._tool("status",
                       "Check the status of the most recent booking.",
                       {"booking_id": {"type": "string", "description": "Optional booking id."}}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "estimate":
            return self.estimate(
                pickup=payload.get("pickup") or "Home",
                dropoff=payload.get("dropoff") or "Office",
                pickup_time=payload.get("pickup_time"),
                distance_km=float(payload.get("distance_km") or 8.4),
            )
        if action == "book":
            return self.book(payload.get("estimate_id"), payload.get("pickup_time"))
        if action == "status":
            return self.status(payload.get("booking_id"))
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"rides has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def estimate(self, pickup: str = "Home", dropoff: str = "Office",
                 pickup_time: Optional[str] = None, distance_km: float = 8.4) -> Dict[str, Any]:
        fare = _BASE_OUT + max(0, int(distance_km)) * _PER_KM
        slug = "|".join([pickup, dropoff, str(distance_km)]).lower()
        estimate_id = "EST-" + hashlib.sha1(slug.encode()).hexdigest()[:8].upper()
        eta_min = max(5, int(distance_km * 3))
        return {"ok": True, "connector": self.id, "action": "estimate", "status": 200,
                "data": {
                    "estimate_id": estimate_id,
                    "provider": _PROVIDER,
                    "pickup": pickup,
                    "dropoff": dropoff,
                    "pickup_time": normalize_time(pickup_time),
                    "distance_km": round(distance_km, 1),
                    "duration_min": eta_min,
                    "fare": fare,
                    "currency": "INR",
                }}

    def book(self, estimate_id: Optional[str] = None, pickup_time: Optional[str] = None,
             pickup: str = "Home", dropoff: str = "Office") -> Dict[str, Any]:
        est = self.estimate(pickup, dropoff, pickup_time)["data"]
        driver = self._DRIVERS[(next(self._counter) - 1) % len(self._DRIVERS)]
        booking_id = f"UBR-{next(self._counter):04d}"
        booking = {
            "booking_id": booking_id,
            "status": "confirmed",
            "provider": _PROVIDER,
            "driver": driver,
            "vehicle": ["Toyota Camry", "Honda City", "Mahindra XUV700"][next(self._counter) % 3],
            "pickup": est["pickup"],
            "dropoff": est["dropoff"],
            "pickup_time": normalize_time(pickup_time) or est["pickup_time"],
            "fare": est["fare"],
            "currency": est["currency"],
        }
        self._bookings.append(booking)
        return {"ok": True, "connector": self.id, "action": "book", "status": 200,
                "data": booking}

    def status(self, booking_id: Optional[str] = None) -> Dict[str, Any]:
        if not self._bookings:
            return {"ok": False, "connector": self.id, "action": "status",
                    "status": 404, "error": "no bookings yet"}
        latest = self._bookings[-1]
        if booking_id and booking_id != latest["booking_id"]:
            return {"ok": False, "connector": self.id, "action": "status",
                    "status": 404, "error": f"booking {booking_id!r} not found"}
        return {"ok": True, "connector": self.id, "action": "status", "status": 200,
                "data": latest}

    @staticmethod
    def _tool(name: str, description: str, props: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": props,
                               "required": list(props)},
            },
        }


_TIME_RE = re.compile(r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<ampm>a\.?m|p\.?m)", re.I)


def normalize_time(raw: Optional[str]) -> Optional[str]:
    """Normalize times like '8:00 PM', '8 PM', '20:00' to 'HH:MM' (24h)."""
    if not raw:
        return None
    text = raw.strip()
    if text.isdigit() and len(text) <= 4:
        if len(text) == 4:
            return f"{text[:2]}:{text[2:]}"
        return f"{int(text):02d}:00"
    match = _TIME_RE.search(text)
    if match:
        hour = int(match.group("h1"))
        minute = int(match.group("m1") or 0)
        ampm = match.group("ampm")
        if ampm and ampm.lower().startswith("p") and hour < 12:
            hour += 12
        if ampm and ampm.lower().startswith("a") and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    if ":" in text:
        return text
    return text