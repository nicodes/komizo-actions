import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "discord_notify", pathlib.Path(__file__).resolve().parents[1] / "notify-discord/send.py"
)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class DiscordNotifyTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "NOTIFY_APP": "example", "NOTIFY_ENVIRONMENT": "production",
            "NOTIFY_STATUS": "failure", "NOTIFY_REVISION": "a" * 40,
            "NOTIFY_DEPLOYMENT_URL": "https://example.com",
            "GITHUB_REPOSITORY": "owner/example", "GITHUB_RUN_ID": "123",
            "GITHUB_ACTOR": "nicodes",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/SECRET",
        }

    def test_preview_failures_are_silent(self):
        self.env["NOTIFY_ENVIRONMENT"] = "preview"
        self.assertIsNone(notify.payload(self.env))

    def test_preview_contains_navigation_and_disables_mentions(self):
        self.env.update(NOTIFY_ENVIRONMENT="preview", NOTIFY_STATUS="success",
                        NOTIFY_DEPLOYMENT_URL="", NOTIFY_PR="12", NOTIFY_PREVIEW_URL="https://pr-12.example.com")
        body = notify.payload(self.env)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        embed = body["embeds"][0]
        self.assertNotIn("title", embed)
        self.assertEqual(embed["description"], "🧪 **example** · [@nicodes](https://github.com/nicodes) · [View](https://pr-12.example.com) · [PR](https://github.com/owner/example/pull/12) · [Run](https://github.com/owner/example/actions/runs/123)")
        self.assertNotIn("timestamp", embed)
        self.assertNotIn("fields", embed)

    def test_production_message_types_and_site_link_without_details(self):
        self.env["NOTIFY_DETAILS"] = "old verbose job results"
        for status, emoji in [("success", "🚀"), ("failure", "❌")]:
            self.env["NOTIFY_STATUS"] = status
            embed = notify.payload(self.env)["embeds"][0]
            self.assertNotIn("title", embed)
            self.assertEqual(embed["description"], f"{emoji} **example** · [@nicodes](https://github.com/nicodes) · [View](https://example.com) · [Run](https://github.com/owner/example/actions/runs/123)")
            self.assertNotIn("timestamp", embed)
            self.assertNotIn("old verbose", str(embed))

    def test_missing_actor_preserves_notification(self):
        del self.env["GITHUB_ACTOR"]
        description = notify.payload(self.env)["embeds"][0]["description"]
        self.assertTrue(description.startswith("❌ **example** · [View]"))

    def test_bot_actor_is_linked_with_escaped_brackets(self):
        self.env["GITHUB_ACTOR"] = "dependabot[bot]"
        description = notify.payload(self.env)["embeds"][0]["description"]
        self.assertIn(r"[@dependabot\[bot\]](https://github.com/dependabot[bot])", description)

    def test_actor_cannot_inject_markdown(self):
        self.env["GITHUB_ACTOR"] = "user](https://example.com)"
        with self.assertRaises(ValueError):
            notify.payload(self.env)

    def test_missing_webhook_skips_without_network(self):
        self.env["DISCORD_WEBHOOK_URL"] = ""
        with contextlib.redirect_stdout(io.StringIO()) as output:
            notify.send(self.env, opener=object())
        self.assertIn("not configured", output.getvalue())

    def test_successful_delivery_sends_json_with_post(self):
        class Delivered:
            def open(inner, request, timeout):
                self.assertEqual(request.method, "POST")
                body = json.loads(request.data)
                self.assertTrue(body["embeds"][0]["description"].startswith("❌ **example** · [@nicodes](https://github.com/nicodes) · "))
                self.assertNotIn("SECRET", request.data.decode())
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.status = 204
                return response

        with contextlib.redirect_stdout(io.StringIO()) as output:
            notify.send(self.env, Delivered())
        self.assertIn("delivered", output.getvalue())

    def test_foreign_webhook_is_rejected_before_network(self):
        self.env["DISCORD_WEBHOOK_URL"] = "https://example.com/api/webhooks/123/SECRET"
        with self.assertRaises(ValueError):
            notify.send(self.env, opener=object())

    def test_rate_limit_retries_are_bounded(self):
        class Limited:
            calls = 0

            def open(inner, request, timeout):
                inner.calls += 1
                self.assertEqual(timeout, 10)
                raise urllib.error.HTTPError(request.full_url, 429, "limited",
                                             {"Retry-After": "9999"}, None)

        opener = Limited()
        delays = []
        with self.assertRaises(ValueError):
            notify.send(self.env, opener, delays.append)
        self.assertEqual(opener.calls, 3)
        self.assertEqual(delays, [5, 5])

    def test_delivery_errors_do_not_leak_or_fail_deployment(self):
        with patch.object(notify, "send", side_effect=RuntimeError("SECRET\n::error::injected")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                notify.main()
        self.assertIn("::warning::", output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("::error::", output.getvalue())


if __name__ == "__main__":
    unittest.main()
