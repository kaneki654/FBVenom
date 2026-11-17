"""Comment delivery stays fast when the AI endpoint is healthy, slow or down.

No phones, no provider requests: every network call goes to a fake opener or a
loopback HTTP server owned by the test.
"""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import core


LAN = {"server_mode": "lan", "provider": "lan",
       "url": "http://192.168.100.129:7499/v1/chat/completions",
       "api_key": "lan-test-token-which-is-long-enough", "model": "m",
       "fallback_model": "m", "temperature": 0.7, "timeout": 5,
       "pollinations_token": "", "tone": "neutral", "min_gap": 0}


def fresh_breaker():
    return {"failures": 0, "open": False, "since": 0.0, "reason": "",
            "generation": 0, "probing": False, "said": set()}


def completion(text="a fresh comment"):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.cfg = self.patches.enter_context(patch.object(core, "ai_cfg", return_value=dict(LAN)))
        self.breaker = fresh_breaker()
        self.patches.enter_context(patch.object(core, "_ai_breaker", self.breaker))
        self.probe = self.patches.enter_context(patch.object(core, "_start_ai_probe"))
        self.errors = self.patches.enter_context(patch.object(core, "report_error"))
        for name, value in {"_ai_off": [False], "_ai_fatal_said": [False], "_ai_fails": [0],
                            "_ai_cold_until": [0.0], "_no_sampling": {}, "_pool_by_link": {}}.items():
            self.patches.enter_context(patch.object(core, name, value))
        self.patches.enter_context(patch.object(core, "_ai_wait_slot", return_value=True))
        self.patches.enter_context(patch.object(core, "abort_sleep", return_value=True))
        self.network = self.patches.enter_context(
            patch.object(core.LAN_NOPROXY, "open", side_effect=TimeoutError("timed out")))
        self.public = self.patches.enter_context(
            patch.object(core.NOPROXY, "open", side_effect=AssertionError("public request")))

    def codes(self):
        return [call.args[0] for call in self.errors.call_args_list]


class BreakerTests(_Base):
    def test_two_timeouts_open_it_and_later_calls_send_nothing(self):
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                core._groq_chat("hi")
        self.assertTrue(core.ai_endpoint_open())
        self.assertTrue(core._ai_is_off())
        self.probe.assert_called_once_with(0)
        with self.assertRaises(core.AIEndpointCooling):
            core._groq_chat("hi")
        self.assertEqual(self.network.call_count, 2)
        self.assertEqual(self.codes(), ["AI_TIMEOUT"])     # once, not per phone

    def test_one_timeout_alone_does_not_open_it(self):
        with self.assertRaises(TimeoutError):
            core._groq_chat("hi")
        self.assertFalse(core.ai_endpoint_open())
        self.network.side_effect = lambda *a, **k: completion()
        self.assertEqual(core._groq_chat("hi"), "a fresh comment")
        self.assertEqual(self.breaker["failures"], 0)

    def test_refused_startup_check_opens_at_once_but_startup_timeout_does_not(self):
        self.assertFalse(core.start_llama())          # a timeout: maybe a cold model
        self.assertFalse(core.ai_endpoint_open())
        self.network.side_effect = ConnectionRefusedError("refused")
        self.assertFalse(core.start_llama())          # refused: definitely down
        self.assertTrue(core.ai_endpoint_open())
        self.assertEqual(self.codes(), ["AI_UNREACHABLE"])

    def test_server_errors_count_but_client_errors_do_not(self):
        def http_error(code):
            def raise_it(*args, **kwargs):
                raise urllib.error.HTTPError(LAN["url"], code, "x", {}, io.BytesIO(b"{}"))
            return raise_it
        self.network.side_effect = http_error(404)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                core._groq_chat("hi")
        self.assertFalse(core.ai_endpoint_open())
        self.assertEqual(self.codes(), ["AI_REQUEST_REJECTED"])
        self.network.side_effect = http_error(503)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                core._groq_chat("hi")
        self.assertTrue(core.ai_endpoint_open())
        self.assertEqual(self.codes(), ["AI_REQUEST_REJECTED", "AI_SERVER_ERROR"])

    def test_rejected_key_turns_ai_off_and_says_why(self):
        def denied(*args, **kwargs):
            raise urllib.error.HTTPError(LAN["url"], 401, "x", {},
                                         io.BytesIO(b'{"error":{"message":"bad key"}}'))
        self.network.side_effect = denied
        with self.assertRaises(RuntimeError):
            core._groq_chat("hi")
        self.assertTrue(core._ai_is_off())
        self.assertEqual(self.codes(), ["AI_AUTH_FAILED"])

    def test_success_closes_it_and_reports_recovery(self):
        self.breaker.update(open=True, since=time.time() - 30, failures=2, reason="x")
        self.breaker["said"].add("outage")
        self.network.side_effect = lambda *a, **k: completion()
        self.assertEqual(core._groq_chat("hi", _probe="recheck"), "a fresh comment")
        self.assertFalse(core.ai_endpoint_open())
        self.assertEqual(self.codes(), ["AI_RECOVERED"])
        self.assertNotIn("outage", self.breaker["said"])

    def test_slow_reply_is_reported_once_but_warmup_never(self):
        clock = iter([100.0, 140.0, 200.0, 240.0, 300.0, 340.0])
        self.network.side_effect = lambda *a, **k: completion()
        with patch.object(core.time, "time", side_effect=lambda: next(clock)):
            core._groq_chat("hi", _probe="warmup")
            self.assertEqual(self.codes(), [])
            core._groq_chat("hi")
            core._groq_chat("hi")
        self.assertEqual(self.codes(), ["AI_SLOW"])

    def test_open_breaker_skips_every_generation_path_quickly(self):
        self.breaker.update(open=True, since=time.time(), reason="x")
        example = "kawawang pilipinas talaga"
        started = time.monotonic()
        self.assertEqual(core.generate_comment(example, ctx=example, tone="neutral"), example)
        text = core.unique_comment(example, ctx=example)
        self.assertTrue(text)
        self.assertLess(time.monotonic() - started, 2.0)
        self.network.assert_not_called()
        self.public.assert_not_called()

    def test_reset_closes_breaker_and_retires_old_probe(self):
        self.breaker.update(open=True, failures=5, probing=True, generation=3)
        self.breaker["said"].add("outage")
        with patch.object(core, "warm_ai_endpoint"):
            core.reset_ai_connection_state()
        self.assertFalse(self.breaker["open"])
        self.assertEqual(self.breaker["failures"], 0)
        self.assertFalse(self.breaker["probing"])
        self.assertEqual(self.breaker["generation"], 4)
        self.assertEqual(self.breaker["said"], set())


class ProbeLoopTests(_Base):
    def test_probe_closes_breaker_when_endpoint_answers(self):
        self.breaker.update(open=True, since=time.time(), probing=True)
        calls = []

        def answer(*args, **kwargs):
            calls.append(kwargs.get("_probe"))
            core._ai_breaker_success(0.5)
            return "ok"
        with patch.object(core, "AI_PROBE_DELAYS", (0.0,)), \
                patch.object(core, "_groq_chat", side_effect=answer):
            core._ai_probe_loop(0)
        self.assertEqual(calls, ["recheck"])
        self.assertFalse(self.breaker["open"])
        self.assertFalse(self.breaker["probing"])

    def test_probe_from_an_older_session_exits_without_requests(self):
        self.breaker.update(open=True, generation=2, probing=True)
        with patch.object(core, "AI_PROBE_DELAYS", (0.0,)), \
                patch.object(core, "_groq_chat") as chat:
            core._ai_probe_loop(1)
        chat.assert_not_called()
        self.assertTrue(self.breaker["probing"])   # owned by the newer session

    def test_probe_keeps_trying_until_success(self):
        self.breaker.update(open=True, since=time.time(), probing=True)
        outcomes = [TimeoutError("timed out"), TimeoutError("timed out"), "ok"]

        def chat(*args, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            core._ai_breaker_success(1.0)
            return outcome
        with patch.object(core, "AI_PROBE_DELAYS", (0.0,)), \
                patch.object(core, "_groq_chat", side_effect=chat):
            core._ai_probe_loop(0)
        self.assertEqual(outcomes, [])
        self.assertFalse(self.breaker["open"])


class KeepAliveTests(unittest.TestCase):
    """The real handler against a loopback HTTP/1.1 server."""

    def setUp(self):
        self.connections = []
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                owner.connections.append(self.client_address)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.requests.append((self.path, self.headers.get("Authorization"), body))
                payload = json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if getattr(owner, "close_after", False):
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                payload = b"plain"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"
        self.handler = core._KeepAliveAIHandler()
        self.opener = core.urllib.request.build_opener(
            core.urllib.request.ProxyHandler({}), core._NoLANRedirect(), self.handler)
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(core, "LAN_NOPROXY", self.opener))
        patches.enter_context(patch.object(core, "_ai_breaker", fresh_breaker()))
        patches.enter_context(patch.object(core, "_start_ai_probe"))
        patches.enter_context(patch.object(core, "report_error"))
        self.cfg = {**LAN, "url": self.url}

    def tearDown(self):
        self.handler.reset()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_consecutive_comments_reuse_one_connection(self):
        for _ in range(3):
            self.assertEqual(core._groq_chat("hi", cfg=self.cfg), "hello")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.connections), 1)
        path, auth, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(auth, "Bearer " + LAN["api_key"])
        self.assertEqual(json.loads(body)["model"], "m")

    def test_server_closing_idle_socket_is_retried_on_a_fresh_one(self):
        self.assertEqual(core._groq_chat("hi", cfg=self.cfg), "hello")
        self.assertEqual(self.handler.idle_count(), 1)
        # Simulate the server dropping the idle keep-alive connection.
        idle = self.handler._idle[next(iter(self.handler._idle))][0]
        idle.sock.shutdown(socket.SHUT_RDWR)
        self.assertEqual(core._groq_chat("hi", cfg=self.cfg), "hello")
        self.assertEqual(len(self.requests), 2)

    def test_connection_close_is_not_pooled(self):
        self.close_after = True
        self.assertEqual(core._groq_chat("hi", cfg=self.cfg), "hello")
        self.assertEqual(self.handler.idle_count(), 0)

    def test_requests_without_the_marker_use_the_stock_handler(self):
        request = core.urllib.request.Request(self.url.replace("/v1/chat/completions", "/plain"))
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.read(), b"plain")
        self.assertEqual(self.handler.idle_count(), 0)

    def test_reset_drops_idle_connections(self):
        core._groq_chat("hi", cfg=self.cfg)
        self.assertEqual(self.handler.idle_count(), 1)
        self.handler.reset()
        self.assertEqual(self.handler.idle_count(), 0)
        core._groq_chat("hi", cfg=self.cfg)
        self.assertEqual(len(self.connections), 2)


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        core._tls.prefetch = None
        self.addCleanup(setattr, core._tls, "prefetch", None)
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.release = threading.Event()
        self.seen = []

        def compose(example, ctx=""):
            self.seen.append((core.current_post_url(), example, ctx))
            self.release.wait(5)
            return f"comment for {core.current_post_url()}"
        self.compose = patches.enter_context(patch.object(core, "unique_comment", side_effect=compose))
        self.stop = patches.enter_context(patch.object(core, "stop_requested", return_value=False))

    def test_comment_is_composed_in_the_background_for_that_link(self):
        target = {"url": "https://www.facebook.com/reel/1", "comment": "a | b", "do_comment": True}
        future = core.prefetch_comment(target)
        self.assertIsNotNone(future)
        self.release.set()
        self.assertEqual(core.take_prefetched_comment(target["url"], "a | b"),
                         "comment for https://www.facebook.com/reel/1")
        self.assertEqual(self.seen, [(target["url"], "a | b", "a b")])
        self.assertIsNone(core.take_prefetched_comment(target["url"], "a | b"))

    def test_retrying_the_same_open_reuses_one_pending_comment(self):
        target = {"url": "https://www.facebook.com/reel/2", "comment": "x", "do_comment": True}
        first = core.prefetch_comment(target)
        second = core.prefetch_comment(target)
        self.assertIs(first, second)
        self.release.set()
        first.result(timeout=5)
        self.assertEqual(self.compose.call_count, 1)

    def test_other_link_or_no_comment_is_not_served(self):
        self.assertIsNone(core.prefetch_comment({"url": "u", "comment": "x", "do_comment": False}))
        core.prefetch_comment({"url": "https://a", "comment": "x", "do_comment": True})
        self.release.set()
        self.assertIsNone(core.take_prefetched_comment("https://b", "x"))

    def test_abort_while_waiting_returns_promptly(self):
        core.prefetch_comment({"url": "https://a", "comment": "x", "do_comment": True})
        self.stop.return_value = True
        started = time.monotonic()
        self.assertEqual(core.take_prefetched_comment("https://a", "x"), "")
        self.assertLess(time.monotonic() - started, 2.0)
        self.release.set()

    def test_failed_prefetch_falls_back_to_inline_composition(self):
        self.compose.side_effect = RuntimeError("boom")
        core.prefetch_comment({"url": "https://a", "comment": "x", "do_comment": True})
        self.assertIsNone(core.take_prefetched_comment("https://a", "x"))


class RetryFeedbackTests(_Base):
    """A rejected styled attempt tells the next one which words sank it."""

    EXAMPLES = ["grabe ang ganda ng view dito sana makabalik ulit kami",
                "sobrang sulit ng bakasyon namin"]
    LEANING = "view dito kinilig ako sana makabalik din kami"
    CLEAN = "ang saya ng tanawin gusto ko pumunta dyan"

    def setUp(self):
        super().setUp()
        core.set_current_post_url("https://www.facebook.com/reel/feedback-test")
        self.addCleanup(core.set_current_post_url, None)

    def test_gate_names_the_echoed_words_in_typed_order_without_changing_the_verdict(self):
        ctx = " ".join(self.EXAMPLES)
        feedback = {}
        self.assertFalse(core._good_context_comment(self.LEANING, self.EXAMPLES, ctx, feedback=feedback))
        self.assertEqual(feedback["avoid"], ["view", "sana", "makabalik"])
        self.assertFalse(core._good_context_comment(self.LEANING, self.EXAMPLES, ctx))
        self.assertTrue(core._good_context_comment(self.CLEAN, self.EXAMPLES, ctx, feedback={}))

    def test_words_used_up_under_the_post_are_named_too(self):
        feedback = {}
        with patch.object(core, "_spent_words", return_value={"tanawin"}):
            self.assertFalse(core._good_context_comment(
                self.CLEAN, self.EXAMPLES, " ".join(self.EXAMPLES), feedback=feedback))
        self.assertEqual(feedback["avoid"], ["tanawin"])

    def test_styled_gate_names_a_name_that_is_not_in_the_post(self):
        feedback = {}
        with patch.object(core, "_foreign_entity", return_value="Batman"):
            self.assertFalse(core._good_styled_comment(
                "grabe si batman dito", "grabe si batman dito".split(), {"length": "any"},
                "a post about a beach", self.EXAMPLES, feedback=feedback))
        self.assertEqual(feedback["avoid"], ["batman"])

    def test_avoid_line_is_empty_until_something_was_rejected_and_stays_bounded(self):
        self.assertEqual(core._retry_avoid_line([]), "")
        line = core._retry_avoid_line(["a", "b", "a"] + [f"w{i}" for i in range(20)])
        self.assertIn('"a", "b", "w0"', line)
        self.assertEqual(line.count('"'), 20)          # ten words, deduplicated
        self.assertIn("NONE of them", line)

    def test_retry_prompt_names_every_word_rejected_so_far(self):
        prompts = []
        replies = [self.LEANING, "sobrang sulit grabe talaga", self.CLEAN]

        def chat(prompt_text, *args, **kwargs):
            prompts.append(prompt_text)
            return replies[len(prompts) - 1]
        with patch.object(core, "_groq_chat", side_effect=chat), \
                patch.object(core, "sanitize_comment", side_effect=lambda raw, *a, **k: raw):
            out = core._generate_context_comment(self.EXAMPLES, " ".join(self.EXAMPLES),
                                                 retries=3, tone="neutral")
        self.assertEqual(out, self.CLEAN)
        self.assertEqual(len(prompts), 3)
        self.assertNotIn("YOUR LAST TRY WAS REJECTED", prompts[0])
        self.assertIn('reusing these words: "view", "sana", "makabalik"', prompts[1])
        # The list only grows: attempt 3 still hears attempt 1's words.
        self.assertIn('"view", "sana", "makabalik", "sobrang", "sulit", "grabe"', prompts[2])
        for prompt in prompts:
            self.assertTrue(prompt.startswith(prompts[0][:200]))


if __name__ == "__main__":
    unittest.main()
