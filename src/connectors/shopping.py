"""Marketplace connector for AP-CODEX (Amazon-style shopping demo).

Provides deterministic search (with a budget filter), a shopping cart, and
checkout against an in-memory catalogue in Indian Rupees.  No network needed;
state lives on the connector instance so the agent loop can chain
``search -> add_to_cart -> checkout`` and always see consistent results.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Union

_CURRENCY = "INR"


class ShoppingClient:
    """A toy marketplace with a fixed catalogue.

    Attributes:
        id: Connector id used by :class:`ConnectorManager`.
    """

    id = "shopping"
    name = "Amazon-style shopping"

    CATALOGUE: Dict[str, Dict[str, Any]] = {
        "shirt-classic-blue": {"name": "Classic Blue Check Shirt", "brand": "WRDX",
                               "category": "shirts", "price": 899},
        "shirt-casual-linen": {"name": "Casual Linen Shirt (White)", "brand": "Van Heusen",
                               "category": "shirts", "price": 1199},
        "shirt-denim": {"name": "Denim Shirt", "brand": "Levi's",
                        "category": "shirts", "price": 1599},
        "shirt-polo-navy": {"name": "Premium Polo T-Shirt (Navy)", "brand": "U.S. Polo",
                            "category": "shirts", "price": 749},
        "shirt-formal-peach": {"name": "Formal Slim Shirt (Peach)", "brand": "Allen Solly",
                               "category": "shirts", "price": 999},
        "tshirt-black-basic": {"name": "Basic Cotton T-Shirt (Black)", "brand": "H&M",
                               "category": "shirts", "price": 499},
        "sneaker-run": {"name": "Running Sneakers", "brand": "Nike",
                        "category": "shoes", "price": 2999},
        "headphone-anc": {"name": "Wireless ANC Headphones", "brand": "Sony",
                          "category": "electronics", "price": 19990},
    }

    _counter = itertools.count(1)

    def __init__(self):
        self._cart: List[Dict[str, Any]] = []

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("search", "Search the marketplace for a product.",
                       {"query": {"type": "string", "description": "Free-text product query, e.g. 'shirt'."},
                        "category": {"type": "string", "description": "Optional category to narrow, e.g. 'shirts'."},
                        "max_price": {"type": "integer",
                                      "description": "Budget ceiling in INR; only items at or below it match."}}),
            self._tool("add_to_cart", "Add a product from a search result to the cart.",
                       {"product_id": {"type": "string", "description": "Product id from a search result."},
                        "quantity": {"type": "integer", "description": "Quantity (default 1)."}}),
            self._tool("checkout", "Check out the current cart and place the order.",
                       {"billing_email": {"type": "string", "description": "Optional email for the receipt."},
                        "payment": {"type": "string", "description": "Payment method (default 'cod')."}}),
            self._tool("cart", "List what is currently in the cart.", {}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "search":
            return self.search(
                query=str(payload.get("query", "")),
                category=payload.get("category"),
                max_price=payload.get("max_price"),
            )
        if action == "add_to_cart":
            return self.add_to_cart(str(payload.get("product_id", "")), int(payload.get("quantity") or 1))
        if action == "checkout":
            return self.checkout(billing_email=payload.get("billing_email"), payment=payload.get("payment"))
        if action == "cart":
            return self.cart()
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"shopping has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def search(self, query: str = "", category: Optional[str] = None,
               max_price: Optional[Union[int, float]] = None) -> Dict[str, Any]:
        query = query.strip().lower()
        words = query.split()
        matches: List[Dict[str, Any]] = []
        for pid, item in self.CATALOGUE.items():
            haystack = " ".join([item["name"], item["brand"], item["category"]]).lower()
            if words and not all(w in haystack for w in words):
                continue
            if category and item["category"] != category.strip().lower():
                continue
            if max_price is not None and item["price"] > int(max_price):
                continue
            matches.append({**item, "id": pid, "currency": _CURRENCY,
                            "url": f"https://www.amazon.in/dp/{pid}"})

        matches.sort(key=lambda m: (m["price"], m["name"]))
        if not matches:
            return {"ok": True, "connector": self.id, "action": "search",
                    "data": {"count": 0, "items": [], "query": query,
                             "message": f"No items match '{query}'"
                                        + (f" under ₹{int(max_price)}" if max_price is not None else "")}}
        best = matches[0]
        return {"ok": True, "connector": self.id, "action": "search",
                "data": {"count": len(matches), "items": matches,
                         "best": best, "query": query}}

    def add_to_cart(self, product_id: str, quantity: int = 1) -> Dict[str, Any]:
        item = self.CATALOGUE.get(product_id)
        if item is None:
            return self._not_found(product_id)
        if quantity < 1:
            quantity = 1
        entry = dict(item, id=product_id, quantity=quantity)
        self._cart.append(entry)
        total = sum(e["price"] * e["quantity"] for e in self._cart)
        return {"ok": True, "connector": self.id, "action": "add_to_cart",
                "data": {"cart_size": len(self._cart), "total": round(total, 2),
                         "currency": _CURRENCY, "added": entry["name"]}}

    def checkout(self, billing_email: Optional[str] = None, payment: str = "cod") -> Dict[str, Any]:
        if not self._cart:
            return {"ok": False, "connector": self.id, "action": "checkout",
                    "status": 422, "error": "cart is empty — nothing to check out"}
        total = round(sum(e["price"] * e["quantity"] for e in self._cart), 2)
        lines = [f"{e['name']} x{e['quantity']} ({_CURRENCY} ₹{e['price']})" for e in self._cart]
        order_id = f"APX-{next(self._counter):04d}"
        order = {
            "order_id": order_id,
            "status": "placed",
            "total": total,
            "currency": _CURRENCY,
            "items": lines,
            "payment": payment,
            "billing_email": billing_email,
            "estimated_delivery": "tomorrow",
        }
        self._cart.clear()
        return {"ok": True, "connector": self.id, "action": "checkout", "status": 200,
                "data": order}

    def cart(self) -> Dict[str, Any]:
        total = round(sum(e["price"] * e["quantity"] for e in self._cart), 2)
        return {"ok": True, "connector": self.id, "action": "cart",
                "data": {"cart_size": len(self._cart), "total": total,
                         "items": [f"{e['name']} x{e['quantity']}" for e in self._cart]}}

    # ------------------------------------------------------------- helpers
    def _not_found(self, product_id: str) -> Dict[str, Any]:
        return {"ok": False, "connector": self.id, "action": "add_to_cart",
                "status": 404, "error": f"product {product_id!r} not found in catalogue"}

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