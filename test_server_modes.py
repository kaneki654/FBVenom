"""Isolated server routing tests: no saved user credentials or external services."""
from copy import deepcopy
import http.server
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import aiconfig

LAN_KEY = "lan-test-key-" + "a" * 40
PRIMARY_KEY = "credential-primary-" + "b" * 40
PRIMARY = {"provider": "custom", "url": "https://primary.example/v1/chat/completions",
           "api_key": PRIMARY_KEY, "model": "primary-model", "fallback_model": "primary-fallback",
           "min_gap": 2.5, "timeout": 90, "tone": "happy", "temperature": 0.7}


class ServerModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"
        self.patches = [patch.object(aiconfig, "CONFIG_PATH", str(self.path)),
                        patch.object(aiconfig, "_cache", None),
                        patch.object(aiconfig, "get_local_lan_key", return_value=""),
                        patch.dict(os.environ, {"VENOM_API_KEY": "", "GROQ_API_KEY": ""})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def profile(**changes):
        arguments = {"url": "http://192.168.100.129:7499", "key": LAN_KEY, "model": "gemma-sea-lion-9b"}
        arguments.update(changes)
        return aiconfig.build_lan_profile(**arguments)

    def test_private_url_forms_normalize_to_one_chat_endpoint(self):
        for path in ("", "/", "/v1", "/v1/", "/chat/completions", "/v1/chat/completions/"):
            with self.subTest(path=path):
                profile = self.profile(url="http://192.168.100.129:7499" + path)
                self.assertEqual(profile["url"], aiconfig.LAN_DEFAULT_URL)
                self.assertEqual(profile["provider"], "lan")
                self.assertEqual(profile["fallback_model"], profile["model"])
                self.assertEqual(profile["timeout"], 300)
                self.assertEqual(profile["min_gap"], 1.0)
        self.assertEqual(self.profile(url="https://127.0.0.1:7499/v1")["url"],
                         "https://127.0.0.1:7499/v1/chat/completions")

    def test_unsafe_addresses_and_credential_urls_are_rejected(self):
        addresses = ("http://0.0.0.0:7499", "http://8.8.8.8:7499", "http://169.254.1.1:7499",
                     "http://gateway.local:7499", "http://[::1]:7499", "ftp://192.168.1.2:7499",
                     "http://secret@192.168.1.2:7499", "http://192.168.1.2:7499?key=secret",
                     "http://192.168.1.2:7499#secret", "http://192.168.1.2:7499/admin",
                     "http://192.168.1.2:0", "http://192.168.1.2:99999")
        for address in addresses:
            with self.subTest(address=address), self.assertRaises(ValueError) as error:
                self.profile(url=address)
            self.assertNotIn("secret", str(error.exception))

    def test_lan_key_and_model_validation_does_not_echo_credentials(self):
        for key in ("", "too-short", "a" * 31, "a" * 32 + "\x7f", "é" * 32, "a b" * 20):
            with self.subTest(key_length=len(key)), self.assertRaises(ValueError):
                self.profile(key=key)
        for model in ("", None, [], "model\ninvalid"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.profile(model=model)

    def test_switch_roundtrip_restores_every_primary_connection_field(self):
        original = aiconfig.save(PRIMARY)
        profile = self.profile()
        switched = aiconfig.set_server_mode("lan", profile)
        self.assertEqual(aiconfig.get_server_mode(switched), "lan")
        for name in aiconfig._CONNECTION_FIELDS:
            self.assertEqual(switched[name], profile[name])
            self.assertEqual(switched["primary_profile"][name], original[name])
        self.assertEqual(switched["tone"], "happy")
        self.assertEqual(switched["temperature"], 0.7)
        aiconfig._cache = None
        restored = aiconfig.set_server_mode("primary")
        self.assertEqual(aiconfig.get_server_mode(restored), "primary")
        for name in aiconfig._CONNECTION_FIELDS:
            self.assertEqual(restored[name], original[name])
        self.assertEqual(aiconfig.get_lan_profile(restored), profile)
        self.assertEqual(aiconfig.set_server_mode("lan")["api_key"], LAN_KEY)

    def test_initial_lan_profile_uses_current_model_and_local_key_without_saving(self):
        cfg = {**PRIMARY, "model": "gemma-sea-lion-9b"}
        original = deepcopy(cfg)
        with patch.object(aiconfig, "get_local_lan_key", return_value=LAN_KEY) as discover:
            profile = aiconfig.get_lan_profile(cfg)
        self.assertEqual(profile["api_key"], LAN_KEY)
        self.assertEqual(profile["model"], "gemma-sea-lion-9b")
        self.assertEqual(profile["fallback_model"], "gemma-sea-lion-9b")
        discover.assert_called_once_with(aiconfig.LAN_DEFAULT_URL)
        self.assertEqual(cfg, original)
        self.assertFalse(self.path.exists())
        self.assertIsNone(aiconfig._cache)

    def test_saved_lan_profile_and_explicit_key_win_over_discovery(self):
        saved = self.profile(model="saved-lan-model")
        cfg = {**PRIMARY, "lan_profile": saved}
        with patch.object(aiconfig, "get_local_lan_key", return_value="different-local-key") as discover:
            result = aiconfig.get_lan_profile(cfg)
        self.assertEqual(result, saved)
        discover.assert_not_called()
        result["api_key"] = "changed"
        self.assertEqual(cfg["lan_profile"]["api_key"], LAN_KEY)

    def test_empty_saved_lan_key_checks_its_address_without_using_primary_key(self):
        saved = self.profile(url="http://192.168.100.130:7499")
        saved["api_key"] = ""
        cfg = {**PRIMARY, "lan_profile": saved}
        with patch.object(aiconfig, "get_local_lan_key", return_value="") as discover:
            result = aiconfig.get_lan_profile(cfg)
        self.assertEqual(result["api_key"], "")
        self.assertEqual(result["model"], saved["model"])
        discover.assert_called_once_with(saved["url"])

    def test_hosted_credentials_are_never_reused_for_lan(self):
        aiconfig.save(PRIMARY)
        old = self.path.read_bytes()
        with self.assertRaises(ValueError):
            aiconfig.set_server_mode("lan", self.profile(key=PRIMARY_KEY))
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(aiconfig.get_server_mode(), "primary")
        self.assertEqual(aiconfig.get_lan_profile()["api_key"], "")

    def test_lan_missing_key_never_uses_hosted_environment_fallback(self):
        profile = self.profile()
        profile["api_key"] = ""
        stored = {**profile, "server_mode": "lan", "lan_profile": deepcopy(profile), "primary_profile": PRIMARY}
        self.path.write_text(json.dumps(stored))
        with patch.dict(os.environ, {"VENOM_API_KEY": PRIMARY_KEY}):
            loaded = aiconfig.load(force=True)
            self.assertEqual(loaded["api_key"], "")
            self.assertFalse(aiconfig.is_ready())

    def test_ai_settings_provider_change_updates_primary_and_preserves_lan(self):
        aiconfig.save(PRIMARY)
        aiconfig.set_server_mode("lan", self.profile())
        draft = aiconfig.load()
        draft.update(PRIMARY)
        draft["model"] = "new-primary-model"
        changed = aiconfig.save(draft)
        self.assertEqual(aiconfig.get_server_mode(changed), "primary")
        self.assertEqual(changed["primary_profile"]["model"], "new-primary-model")
        self.assertEqual(changed["lan_profile"]["api_key"], LAN_KEY)
        self.assertEqual(aiconfig.set_server_mode("lan")["model"], "gemma-sea-lion-9b")
        self.assertEqual(aiconfig.set_server_mode("primary")["model"], "new-primary-model")

    def test_primary_transition_cannot_leak_active_lan_key(self):
        aiconfig.save(PRIMARY)
        aiconfig.set_server_mode("lan", self.profile())
        draft = aiconfig.load()
        draft.update(provider="groq", url="https://api.groq.com/openai/v1/chat/completions")
        previous = self.path.read_bytes()
        with self.assertRaises(ValueError):
            aiconfig.save(draft)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(aiconfig.get_server_mode(), "lan")

    def test_provider_presets_use_stored_primary_key_or_clear_it(self):
        primary = {**PRIMARY, "provider": "groq"}
        aiconfig.save(primary)
        aiconfig.set_server_mode("lan", self.profile())
        self.assertEqual(aiconfig.apply_preset("groq")["api_key"], PRIMARY_KEY)
        aiconfig.set_server_mode("lan")
        self.assertEqual(aiconfig.apply_preset("openrouter")["api_key"], "")
        self.assertEqual(aiconfig.get_server_mode(), "primary")
        self.assertEqual(aiconfig.apply_preset("lan")["api_key"], LAN_KEY)

    def test_model_listing_uses_private_lan_validator(self):
        with patch.object(aiconfig, "validate_lan_connection", return_value=["lan-model"]) as validate:
            profile = self.profile()
            self.assertEqual(aiconfig.list_models(profile), ["lan-model"])
            validate.assert_called_once_with(profile)

    def test_load_and_get_profile_return_deep_copies(self):
        aiconfig.save(PRIMARY)
        aiconfig.set_server_mode("lan", self.profile())
        loaded = aiconfig.load()
        loaded["primary_profile"]["api_key"] = "mutated"
        loaded["lan_profile"]["api_key"] = "mutated"
        self.assertEqual(aiconfig.load()["primary_profile"]["api_key"], PRIMARY_KEY)
        profile = aiconfig.get_lan_profile()
        profile["api_key"] = "mutated"
        self.assertEqual(aiconfig.load()["lan_profile"]["api_key"], LAN_KEY)

    def test_atomic_save_is_private_and_failure_preserves_disk_and_cache(self):
        original = aiconfig.save(PRIMARY)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        old = self.path.read_bytes()
        with patch.object(aiconfig.os, "replace", side_effect=OSError("private details")):
            with self.assertRaises(RuntimeError) as error:
                aiconfig.set_server_mode("lan", self.profile())
        self.assertNotIn("private details", str(error.exception))
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(aiconfig.load(), original)
        self.assertEqual(list(self.path.parent.glob(".venom-ai-*")), [])

    def test_mode_description_does_not_expose_any_key_fragment(self):
        aiconfig.save(PRIMARY)
        description = aiconfig.describe()
        self.assertIn("Primary", description)
        self.assertNotIn(PRIMARY_KEY[:6], description)
        aiconfig.set_server_mode("lan", self.profile())
        description = aiconfig.describe()
        self.assertIn("LAN", description)
        self.assertNotIn(LAN_KEY[:6], description)
        self.assertTrue(aiconfig.is_ready())

    def test_validation_requires_healthy_gateway_and_authenticates_models(self):
        profile = self.profile()
        with patch.object(aiconfig, "_lan_json_get", side_effect=[{"status": "ok"}, {"data": [
                {"id": "model-a"}, {"id": "model-a"}, {"id": "model-b"}]}]) as get:
            self.assertEqual(aiconfig.validate_lan_connection(profile), ["model-a", "model-b"])
        self.assertEqual(get.call_args_list[0].args[:2], ("http://192.168.100.129:7499/health", ""))
        self.assertEqual(get.call_args_list[1].args[:2], ("http://192.168.100.129:7499/v1/models", LAN_KEY))
        self.assertEqual(get.call_args_list[0].args[2], get.call_args_list[1].args[2])
        with patch.object(aiconfig, "_lan_json_get", return_value={"status": "unavailable"}):
            with self.assertRaises(RuntimeError):
                aiconfig.validate_lan_connection(profile)

    def test_validation_network_errors_are_sanitized(self):
        with patch.object(aiconfig, "_lan_json_get", side_effect=OSError("private " + LAN_KEY)):
            with self.assertRaises(RuntimeError) as error:
                aiconfig.validate_lan_connection(self.profile())
        self.assertNotIn(LAN_KEY, str(error.exception))
        self.assertNotIn("private", str(error.exception))

    def server(self, mode="ok"):
        calls = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/steal")
                    self.end_headers()
                    return
                data = ({"status": "ok"} if self.path == "/health" else
                        {"data": [{"id": "local-model"}]})
                raw = json.dumps(data).encode()
                if mode == "large" and self.path == "/v1/models":
                    raw = b" " * (1024 * 1024 + 1)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        profile = self.profile(url=f"http://127.0.0.1:{server.server_port}")
        return profile, calls

    def test_direct_validation_ignores_proxy_environment_and_sends_only_lan_key(self):
        profile, calls = self.server()
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
            self.assertEqual(aiconfig.validate_lan_connection(profile), ["local-model"])
        self.assertEqual(calls, [("/health", None), ("/v1/models", "Bearer " + LAN_KEY)])

    def test_validation_never_follows_redirects(self):
        profile, calls = self.server(mode="redirect")
        with self.assertRaises(RuntimeError) as error:
            aiconfig.validate_lan_connection(profile)
        self.assertIn("302", str(error.exception))
        self.assertEqual(calls, [("/health", None)])

    def test_validation_bounds_upstream_response_size(self):
        profile, _ = self.server(mode="large")
        with self.assertRaises(RuntimeError) as error:
            aiconfig.validate_lan_connection(profile)
        self.assertIn("oversized", str(error.exception))


@unittest.skipUnless(all(hasattr(os, name) for name in ("getuid", "O_NOFOLLOW", "O_NONBLOCK")),
                     "Requires POSIX private-file checks")
class LocalLANKeyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "lan.key"
        self.path.write_text(LAN_KEY + "\n", encoding="ascii")
        self.path.chmod(0o600)
        path_patch = patch.object(aiconfig, "LOCAL_LAN_KEY_FILE", str(self.path))
        path_patch.start()
        self.addCleanup(path_patch.stop)
        socket_patch = patch.object(aiconfig.socket, "socket")
        self.socket = socket_patch.start()
        self.addCleanup(socket_patch.stop)
        self.probe = self.socket.return_value.__enter__.return_value
        self.url = "http://192.168.100.129:7499/v1"

    def test_assigned_address_imports_private_key_without_outbound_connection_or_writes(self):
        original = self.path.read_bytes()
        self.assertEqual(aiconfig.get_local_lan_key(self.url), LAN_KEY)
        self.probe.bind.assert_called_once_with(("192.168.100.129", 0))
        self.probe.connect.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_remote_address_never_opens_the_local_secret(self):
        self.probe.bind.side_effect = OSError("address not assigned locally")
        with patch.object(aiconfig.os, "open", wraps=os.open) as opened:
            self.assertEqual(aiconfig.get_local_lan_key(self.url), "")
        opened.assert_not_called()

    def test_invalid_or_public_url_never_probes_address_or_opens_secret(self):
        for url in ("http://8.8.8.8:7499", "http://gateway.local:7499", "http://0.0.0.0:7499",
                    "http://user:secret@192.168.100.129:7499", "http://192.168.100.129:7499/admin"):
            with self.subTest(url=url), patch.object(aiconfig.os, "open", wraps=os.open) as opened:
                self.assertEqual(aiconfig.get_local_lan_key(url), "")
                opened.assert_not_called()
        self.socket.assert_not_called()

    def test_missing_or_world_readable_key_is_not_imported(self):
        self.path.chmod(0o644)
        self.assertEqual(aiconfig.get_local_lan_key(self.url), "")
        self.path.unlink()
        self.assertEqual(aiconfig.get_local_lan_key(self.url), "")

    def test_wrong_owner_key_is_not_imported(self):
        current_uid = os.getuid()
        with patch.object(aiconfig.os, "getuid", return_value=current_uid + 1):
            self.assertEqual(aiconfig.get_local_lan_key(self.url), "")

    def test_symlink_hardlink_and_fifo_are_rejected(self):
        target = self.path.with_name("target.key")
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(aiconfig.get_local_lan_key(self.url), "")
        self.path.unlink()
        self.path.hardlink_to(target)
        self.assertEqual(aiconfig.get_local_lan_key(self.url), "")
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assertEqual(aiconfig.get_local_lan_key(self.url), "")

    def test_invalid_and_oversized_file_contents_are_not_imported(self):
        for text in ("x" * 31, "é" * 32, "x" * 32 + "\x7f", "x" * 32 + " internal space", "x" * 4097):
            with self.subTest(length=len(text)):
                self.path.write_text(text, encoding="utf-8")
                self.assertEqual(aiconfig.get_local_lan_key(self.url), "")


if __name__ == "__main__":
    unittest.main()
