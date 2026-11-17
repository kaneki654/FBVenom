"""AI routing stays on the selected LAN profile and resets only connection state."""

from contextlib import ExitStack
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
from unittest.mock import patch

import core


LAN_CONFIG = {
    "server_mode": "lan", "provider": "custom",
    "url": "http://192.168.100.129:7499/v1/chat/completions",
    "api_key": "lan-test-token-which-is-long-enough", "model": "lan-model",
    "fallback_model": "lan-model", "temperature": 0.7, "timeout": 5,
    "pollinations_token": "", "tone": "neutral", "min_gap": 0,
}
PRIMARY_CONFIG = {**LAN_CONFIG, "server_mode": "primary", "provider": "groq",
                  "url": "https://provider.example/v1/chat/completions",
                  "api_key": "primary-test-key", "model": "primary-model",
                  "fallback_model": "primary-model"}


def completion(text="a fresh comment"):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


def fresh_breaker():
    return {"failures": 0, "open": False, "since": 0.0, "reason": "",
            "generation": 0, "probing": False, "said": set()}


class ServerModeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.config = self.patches.enter_context(patch.object(core, "ai_cfg", return_value=dict(LAN_CONFIG)))
        self.patches.enter_context(patch.object(core, "_ai_wait_slot", return_value=True))
        self.patches.enter_context(patch.object(core, "abort_sleep", return_value=True))
        self.patches.enter_context(patch.object(core, "sanitize_comment", side_effect=lambda raw, *a, **k: raw))
        self.patches.enter_context(patch.object(core, "_good_variation", return_value=True))
        self.network = self.patches.enter_context(patch.object(core.NOPROXY, "open", side_effect=AssertionError("Unexpected public request")))
        self.lan_network = self.patches.enter_context(patch.object(core.LAN_NOPROXY, "open", side_effect=OSError("LAN offline")))
        self.poll_get = self.patches.enter_context(patch.object(core, "_pollinations_get", side_effect=AssertionError("External fallback called")))
        self.poll_post = self.patches.enter_context(patch.object(core, "_pollinations_post", side_effect=AssertionError("External fallback called")))
        self.patches.enter_context(patch.object(core, "_ai_off", [False]))
        self.patches.enter_context(patch.object(core, "_ai_fatal_said", [False]))
        self.patches.enter_context(patch.object(core, "_ai_fails", [0]))
        self.patches.enter_context(patch.object(core, "_ai_cold_until", [0.0]))
        self.patches.enter_context(patch.object(core, "_poll_dead", [False]))
        self.patches.enter_context(patch.object(core, "_no_sampling", {}))
        self.patches.enter_context(patch.object(core, "_pool_by_link", {}))
        self.patches.enter_context(patch.object(core, "_ai_breaker", fresh_breaker()))
        self.probe = self.patches.enter_context(patch.object(core, "_start_ai_probe"))

    def assert_no_external_calls(self):
        self.network.assert_not_called()
        self.poll_get.assert_not_called()
        self.poll_post.assert_not_called()

    def test_failed_lan_comment_reply_and_startup_never_try_external_provider(self):
        example = "a lovely example"
        self.assertEqual(core.generate_comment(example, retries=1, tone="neutral"), example)
        self.assertEqual(core.ai_reply("hello", retries=1), "")
        self.assertFalse(core.start_llama())
        self.assertEqual(self.lan_network.call_count, 3)
        self.assert_no_external_calls()

    def test_successful_lan_routes_use_saved_key_model_and_url(self):
        self.lan_network.side_effect = lambda *a, **k: completion()
        self.assertEqual(core.generate_comment("an example", retries=1, tone="neutral"), "a fresh comment")
        self.assertEqual(core.ai_reply("hello", retries=1), "a fresh comment")
        self.assertTrue(core.start_llama())
        for call in self.lan_network.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, LAN_CONFIG["url"])
            self.assertEqual(request.get_header("Authorization"), "Bearer " + LAN_CONFIG["api_key"])
            self.assertEqual(json.loads(request.data)["model"], "lan-model")
        self.assert_no_external_calls()

    def test_lan_without_key_is_still_contacted_instead_of_external_fallback(self):
        self.config.return_value = {**LAN_CONFIG, "api_key": ""}
        # A refused start-up check opens the shared breaker, so the reply is
        # asked first: both must reach the LAN gateway and nothing external.
        self.assertEqual(core.ai_reply("hello", retries=1), "")
        self.assertFalse(core.start_llama())
        self.assertEqual(self.lan_network.call_count, 2)
        self.assert_no_external_calls()

    def test_unique_comment_offline_path_keeps_lan_only_routing(self):
        self.assertIsInstance(core.unique_comment("a lovely example", tone="annoyed"), str)
        self.assertGreater(self.lan_network.call_count, 0)
        self.assert_no_external_calls()

    def test_primary_comment_and_reply_keep_existing_external_fallback(self):
        self.config.return_value = dict(PRIMARY_CONFIG)
        self.poll_get.side_effect = None
        self.poll_get.return_value = "provider fallback reply"
        self.network.side_effect = OSError("primary provider offline")
        self.assertEqual(core.generate_comment("an example", retries=1, tone="neutral"), "provider fallback reply")
        self.assertEqual(core.ai_reply("hello", retries=1), "provider fallback reply")
        self.assertEqual(self.poll_get.call_count, 2)
        self.assertEqual(self.network.call_count, 2)
        self.lan_network.assert_not_called()

    def test_primary_startup_keeps_existing_plaintext_provider_probe(self):
        self.config.return_value = {**PRIMARY_CONFIG, "api_key": ""}
        self.network.side_effect = lambda *a, **k: io.BytesIO(b"ready")
        self.assertTrue(core.start_llama())
        request = self.network.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://text.pollinations.ai/"))
        self.lan_network.assert_not_called()

    def test_generation_captures_routing_config_for_the_whole_call(self):
        self.config.side_effect = [dict(LAN_CONFIG), dict(PRIMARY_CONFIG)]
        self.lan_network.side_effect = lambda *a, **k: completion()
        self.assertEqual(core.generate_comment("an example", retries=1, tone="neutral"), "a fresh comment")
        self.config.assert_called_once_with()
        self.assert_no_external_calls()


class DirectFallbackAndStateTests(unittest.TestCase):
    def test_direct_pollinations_calls_are_blocked_before_network_in_lan_mode(self):
        with patch.object(core, "ai_cfg", return_value=dict(LAN_CONFIG)), \
                patch.object(core.NOPROXY, "open") as network:
            for function in (core._pollinations_get, core._pollinations_post):
                with self.subTest(function=function.__name__):
                    with self.assertRaisesRegex(RuntimeError, "disabled in LAN mode"):
                        function("private comment")
            network.assert_not_called()

    def test_reset_connection_state_preserves_dedupe_history_and_does_no_work(self):
        history = {"post": {"comments": {"already posted"}, "words": {"used": 2}}}
        before = deepcopy(history)
        with ExitStack() as patches:
            for name, value in {
                "_ai_off": [True], "_ai_fatal_said": [True], "_ai_fails": [14],
                "_ai_cold_until": [9999999999.0], "_poll_dead": [True],
                "_poll_last_call": [100.0], "_ai_next_slot": [200.0],
                "_no_sampling": {"lan-model": True}, "_pool_by_link": history,
                "_example_turn": [7],
            }.items():
                patches.enter_context(patch.object(core, name, value))
            warm = patches.enter_context(patch.object(core, "warm_ai_endpoint"))
            thread = patches.enter_context(patch.object(core.threading, "Thread"))
            network = patches.enter_context(patch.object(core.NOPROXY, "open"))
            lan_network = patches.enter_context(patch.object(core.LAN_NOPROXY, "open"))
            core.reset_ai_connection_state()
            self.assertFalse(core._ai_is_off())
            self.assertEqual(core._ai_fails, [0])
            self.assertEqual(core._ai_fatal_said, [False])
            self.assertEqual(core._poll_dead, [False])
            self.assertEqual(core._no_sampling, {})
            self.assertEqual(core._poll_last_call, [0.0])
            self.assertEqual(core._ai_next_slot, [0.0])
            self.assertEqual(core._pool_by_link, before)
            self.assertEqual(core._example_turn, [7])
            for mock in (warm, thread, network, lan_network):
                mock.assert_not_called()

    def test_idle_settings_reset_does_not_wait_for_background_http_request(self):
        held_http_lock = threading.Lock()
        finished = threading.Event()
        history = {"post": {"comments": {"already posted"}}}
        before = deepcopy(history)
        with ExitStack() as patches:
            patches.enter_context(patch.object(core, "_ai_http_lock", held_http_lock))
            patches.enter_context(patch.object(core, "_pool_by_link", history))
            patches.enter_context(patch.object(core, "_ai_off", [True]))
            warm = patches.enter_context(patch.object(core, "warm_ai_endpoint"))
            network = patches.enter_context(patch.object(core.NOPROXY, "open"))
            lan_network = patches.enter_context(patch.object(core.LAN_NOPROXY, "open"))
            def reset():
                core.reset_ai_connection_state()
                finished.set()
            held_http_lock.acquire()
            worker = threading.Thread(target=reset, daemon=True)
            try:
                worker.start()
                self.assertTrue(finished.wait(0.5),
                                "An idle settings reset waited for the HTTP request lock")
                self.assertTrue(held_http_lock.locked())
                self.assertFalse(core._ai_is_off())
                self.assertEqual(core._pool_by_link, before)
            finally:
                held_http_lock.release()
                worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            for mock in (warm, network, lan_network):
                mock.assert_not_called()

    def test_lan_endpoint_redirect_is_not_followed(self):
        visited = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                visited.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/should-not-receive-comment")
                self.end_headers()
            def do_GET(self):
                visited.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "redirected"}}]}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = {**LAN_CONFIG, "url": f"http://127.0.0.1:{server.server_port}/redirect"}
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                core._groq_chat("private comment", cfg=cfg)
            self.assertEqual(visited, ["/redirect"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
