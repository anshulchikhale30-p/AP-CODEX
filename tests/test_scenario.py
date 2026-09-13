"""Integration tests for the ScenarioRunner (deterministic task decomposition)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unittest  # noqa: E402

from connectors import ConnectorManager, ShoppingClient, RideClient, EmailClient, TwitterClient  # noqa: E402
from connectors import MeetClient  # noqa: E402
from core import ScenarioRunner  # noqa: E402


class ScenarioTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = ConnectorManager()
        cls.manager.register(ShoppingClient())
        cls.manager.register(RideClient())
        cls.email_client = EmailClient()
        cls.manager.register(cls.email_client)
        cls.twitter_client = TwitterClient()
        cls.manager.register(cls.twitter_client)
        cls.meet_client = MeetClient(base_dir=tempfile.mkdtemp())
        cls.manager.register(cls.meet_client)
        cls.runner = ScenarioRunner(cls.manager)

    def setUp(self):
        self.email_client._outbox.clear()

    def test_tweet_intent(self):
        result = asyncio.run(self.runner.run(
            "tweet the q3 revenue numbers are up 12%"
        ))
        self.assertTrue(result.ok)
        tweets = [t for t in result.tool_calls if t.get("action") == "post_tweet"]
        self.assertEqual(len(tweets), 1)
        # twitter not configured -> clean tool-level error, no crash
        self.assertEqual(tweets[0]["status"], "error")

    def test_post_on_twitter_intent(self):
        result = asyncio.run(self.runner.run(
            "post on twitter that my new AI agent project is live!"
        ))
        self.assertTrue(result.ok)
        tweets = [t for t in result.tool_calls if t.get("action") == "post_tweet"]
        self.assertEqual(len(tweets), 1)
        self.assertEqual(tweets[0]["status"], "error")
        self.assertIn("access_token", tweets[0]["result"]["error"])
        # plan carries the cleaned tweet text
        plan = self.runner.parse("post on twitter that my new AI agent project is live!")[0]
        text = plan.steps[0].payload["text"]
        self.assertEqual(text, "My new AI agent project is live!")

    def test_tweet_with_no_text_ignored(self):
        result = asyncio.run(self.runner.run("can I tweet"))
        self.assertIsNone(result)

    def test_google_meet_intent(self):
        result = asyncio.run(self.runner.run(
            "create a google meet link for client call"
        ))
        self.assertTrue(result.ok)
        meets = [t for t in result.tool_calls if t.get("action") == "create_space"]
        self.assertEqual(len(meets), 1)
        self.assertEqual(meets[0]["status"], "error")
        self.assertIn("Meet", meets[0]["result"]["error"])
        plan = self.runner.parse("create a google meet link for the client call")[0]
        self.assertEqual(plan.steps[0].payload["title"], "the client call")

    def test_create_meeting_intent(self):
        result = asyncio.run(self.runner.run("create meeting"))
        self.assertTrue(result.ok)
        actions = [t.get("action") for t in result.tool_calls]
        self.assertIn("create_space", actions)
        self.assertIn("create_event", actions)  # meeting is scheduled on the calendar too
        plan = self.runner.parse("create a meeting for the client call")[0]
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].payload["title"], "the client call")
        self.assertEqual(plan.steps[0].connector, "meet")
        self.assertEqual(plan.steps[1].connector, "calendar")
        self.assertEqual(plan.steps[1].action, "create_event")
        plan = self.runner.parse("schedule a meeting with the team")[0]
        self.assertEqual(plan.steps[0].payload["title"], "the team")

    def test_plain_meeting_word_not_meet(self):
        # "meeting" alone must not create a Meet (avoids email/meeting collisions)
        self.assertEqual(self.runner.parse("about the meeting"), [])
        self.assertEqual(self.runner.parse("nice to meet you"), [])

    def test_calendar_intent(self):
        result = asyncio.run(self.runner.run(
            "add a team standup to my calendar tomorrow at 9 am"
        ))
        self.assertTrue(result.ok)
        events = [t for t in result.tool_calls if t.get("action") == "create_event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "error")  # not authorized in tests
        plan = self.runner.parse("add a team standup to my calendar tomorrow")[0]
        payload = plan.steps[0].payload
        self.assertEqual(payload["summary"], "team standup tomorrow")
        self.assertIn("+00:00", payload["start"])

    # ---------------------------------------------------------------- basics
    def test_no_match_returns_none(self):
        result = asyncio.run(self.runner.run("what is the weather today?"))
        self.assertIsNone(result)

    def test_single_intent_email(self):
        result = asyncio.run(self.runner.run(
            "email my boss that the build is broken"
        ))
        self.assertTrue(result.ok)
        self.assertTrue(any("email.send" in (t["name"] or "") for t in result.tool_calls))
        # simulated email landed in outbox
        self.assertEqual(len(self.email_client._outbox), 1)
        self.assertIn("build is broken", self.email_client._outbox[0]["body"])
        self.assertIn("build is broken", self.email_client._outbox[0]["subject"])

    # -------------------------------------------------------------- shopping
    def test_shopping_search_filters_by_budget(self):
        result = asyncio.run(self.runner.run(
            "buy me a shirt on Amazon under ₹1,000"
        ))
        self.assertTrue(result.ok)
        # the whole shopping chain ran end to end
        actions = [t["action"] for t in result.tool_calls]
        self.assertIn("search", actions)
        self.assertIn("add_to_cart", actions)
        self.assertIn("checkout", actions)
        self.assertIn("APX-", result.reply)
        # budget respected at the connector level too
        search = ShoppingClient().search("shirt", category="shirts", max_price=1000)
        items = search["data"]["items"]
        self.assertTrue(all(item["price"] <= 1000 for item in items))
        self.assertLessEqual(search["data"]["best"]["price"], 1000)

    def test_shopping_no_match_under_budget(self):
        result = asyncio.run(self.runner.run(
            "buy me headphones under ₹500"
        ))
        self.assertIn("No matching item", result.reply)
        actions = [t["action"] for t in result.tool_calls]
        self.assertIn("search", actions)
        self.assertNotIn("add_to_cart", actions)
        self.assertNotIn("checkout", actions)

    # -------------------------------------------------------------- rides
    def test_rides_with_time(self):
        result = asyncio.run(self.runner.run(
            "book an Uber cab for 8:00 PM"
        ))
        self.assertTrue(result.ok)
        self.assertTrue(any("rides." in (t["name"] or "") for t in result.tool_calls))
        self.assertIn("UBR-", result.reply)
        # the 8:00 PM request was normalized to 20:00 and reflected in the reply
        self.assertIn("20:00", result.reply)

    def test_rides_no_time(self):
        result = asyncio.run(self.runner.run("call me a taxi"))
        self.assertTrue(result.ok)
        self.assertIn("confirmed", result.reply.lower())

    # -------------------------------------------------------------- compound
    def test_full_compound_task(self):
        text = ("Buy me a shirt on Amazon under ₹1,000, "
                "book an Uber cab for 8:00 PM, "
                "email my manager that I'm late")
        result = asyncio.run(self.runner.run(text))
        self.assertTrue(result.ok)
        names = [t["name"] for t in result.tool_calls]
        self.assertIn("shopping.search", names)
        self.assertIn("shopping.add_to_cart", names)
        self.assertIn("shopping.checkout", names)
        self.assertIn("rides.estimate", names)
        self.assertIn("rides.book", names)
        self.assertIn("email.send", names)
        # order, booking, email all reflected in the final reply
        self.assertIn("APX-", result.reply)
        self.assertIn("UBR-", result.reply)
        # the 8:00 PM ride time survived clause parsing as 20:00
        self.assertIn("20:00", result.reply)
        # email was sent
        self.assertEqual(len(self.email_client._outbox), 1)
        self.assertIn("late", self.email_client._outbox[0]["body"].lower())

    def test_email_late_remapping(self):
        result = asyncio.run(self.runner.run(
            "please email my manager that i'm late"
        ))
        self.assertTrue(result.ok)
        self.assertEqual(self.email_client._outbox[-1]["subject"], "Late arrival")

    def test_typo_friendly_compound(self):
        # real-world phrasing with typos: "amzon", "gmail", "maneger", "8 o clock"
        text = ("buy me a shirt from amzon under 1000, book uber cab for me at 8 o clock, "
                "also send gmail to my maneger that i am late")
        result = asyncio.run(self.runner.run(text))
        self.assertTrue(result.ok)
        names = [t["name"] for t in result.tool_calls]
        self.assertIn("shopping.search", names)
        self.assertIn("shopping.add_to_cart", names)
        self.assertIn("shopping.checkout", names)
        self.assertIn("rides.estimate", names)
        self.assertIn("rides.book", names)
        self.assertIn("email.send", names)
        self.assertIn("APX-", result.reply)
        self.assertIn("UBR-", result.reply)
        # "8 o clock" normalized to 08:00
        self.assertIn("08:00", result.reply)
        self.assertEqual(self.email_client._outbox[-1]["subject"], "Late arrival")
        self.assertIn("late", self.email_client._outbox[-1]["body"].lower())

    # -------------------------------------------------------------- stream
    def test_stream_events(self):
        async def collect():
            kinds = []
            tool_statuses = []
            async for event in self.runner.stream("email the team that all clear"):
                kinds.append(event["kind"])
                if event["kind"] == "tool":
                    tool_statuses.append(event["status"])
            return kinds, tool_statuses

        kinds, tool_statuses = asyncio.run(collect())
        self.assertIn("status", kinds)
        self.assertIn("delta", kinds)
        self.assertIn("tool", kinds)
        self.assertIn("done", kinds)
        self.assertEqual(tool_statuses, ["started", "ok"])


if __name__ == "__main__":
    unittest.main()