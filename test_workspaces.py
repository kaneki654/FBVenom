import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspaces import WorkspaceStore


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workspaces.json"
        self.store = WorkspaceStore(self.path)

    def test_missing_settings_start_empty_without_writing(self):
        self.store.load()
        self.assertEqual(self.store.workspaces, [])
        self.assertFalse(self.path.exists())

    def test_independent_workspaces_roundtrip_all_settings(self):
        first = self.store.create("  Café team  ", 2)
        second = self.store.create("Evening team", 1)
        first["links"] = [{"url": "https://www.facebook.com/reel/123",
                           "reaction": "love", "comment": "Hello\nKumusta!"}]
        first.update(screenshots=True, speed="turbo", devices=["phone-A"])
        self.assertEqual(second["links"],
                         [{"url": "", "reaction": "like", "comment": ""}])
        self.store.save()
        loaded = WorkspaceStore(self.path)
        loaded.load()
        self.assertEqual(loaded.workspaces, self.store.workspaces)
        self.assertEqual(loaded.get(first["id"])["name"], "Café team")
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)

    def test_names_are_required_and_case_insensitively_unique(self):
        for name in ("", "  \n", None, 3):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create(name, 1)
        self.store.create("Straße", 1)
        with self.assertRaises(ValueError):
            self.store.create(" STRASSE ", 1)
        self.assertEqual(len(self.store.workspaces), 1)

    def test_device_counts_are_positive_integers_without_an_arbitrary_cap(self):
        for count in (0, -1, True, False, 1.5, "2", None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.store.create("Team", count)
        self.assertEqual(self.store.create("Team", 1000)["device_count"], 1000)

    def test_update_is_transactional_and_retains_independent_content(self):
        first = self.store.create("First", 2)
        second = self.store.create("Second", 1)
        first["links"][0]["comment"] = "Only first"
        before = copy.deepcopy(first)
        for name, count in (("Second", 3), ("Renamed", 0)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.update(first["id"], name, count)
            self.assertEqual(first, before)
        self.store.update(first["id"], " FIRST ", 3)
        self.assertEqual(first["name"], "FIRST")
        self.assertEqual(first["device_count"], 3)
        self.assertEqual(first["links"], before["links"])
        self.assertEqual(second["links"][0]["comment"], "")

    def test_remove_and_unknown_ids(self):
        first = self.store.create("First", 1)
        second = self.store.create("Second", 1)
        self.store.remove(first["id"])
        self.assertIsNone(self.store.get(first["id"]))
        self.assertIs(self.store.get(second["id"]), second)
        with self.assertRaises(ValueError):
            self.store.update(first["id"], "Gone", 1)
        with self.assertRaises(ValueError):
            self.store.remove(first["id"])

    def test_corrupt_load_preserves_file_and_current_memory(self):
        workspace = self.store.create("Existing", 1)
        bad = b'{"version": 1, broken'
        self.path.write_bytes(bad)
        with self.assertRaises(ValueError):
            self.store.load()
        self.assertEqual(self.path.read_bytes(), bad)
        self.assertEqual(self.store.workspaces, [workspace])

    def test_invalid_schema_and_duplicate_ids_or_names_are_rejected(self):
        original = self.store.create("Existing", 1)
        valid = {"version": 1, "workspaces": [original]}
        invalid = [None, [], {}, {"version": True, "workspaces": []},
                   {"version": 2, "workspaces": []},
                   {"version": 1, "workspaces": {}},
                   {"version": 1, "workspaces": [None]}]
        changes = {"id": "bad", "name": " ", "device_count": True,
                   "links": [{"url": "", "reaction": "like", "comment": 5}],
                   "screenshots": 1, "speed": "unknown", "devices": [None]}
        for field, value in changes.items():
            document = copy.deepcopy(valid)
            document["workspaces"][0][field] = value
            invalid.append(document)
        duplicate_id = copy.deepcopy(original)
        duplicate_id["name"] = "Different"
        invalid.append({"version": 1, "workspaces": [original, duplicate_id]})
        duplicate_name = copy.deepcopy(original)
        duplicate_name.update(id="a" * 32, name=" EXISTING ")
        invalid.append({"version": 1, "workspaces": [original, duplicate_name]})
        for document in invalid:
            with self.subTest(document=document):
                raw = json.dumps(document)
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.load()
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)
                self.assertIs(self.store.workspaces[0], original)

    def test_failed_atomic_replace_keeps_previous_settings_and_cleans_tempfile(self):
        first = self.store.create("First", 1)
        self.store.save()
        previous = self.path.read_bytes()
        first["name"] = "Changed"
        with patch("workspaces.os.replace", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.store.save()
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_memory_cannot_overwrite_valid_settings(self):
        workspace = self.store.create("First", 1)
        self.store.save()
        previous = self.path.read_bytes()
        workspace["links"][0]["comment"] = None
        with self.assertRaises(ValueError):
            self.store.save()
        self.assertEqual(self.path.read_bytes(), previous)

    def test_allocation_is_disjoint_stable_and_discovery_order_independent(self):
        first = self.store.create("First", 2)
        second = self.store.create("Second", 2)
        assignments = self.store.allocate(["d", "a", "b", "c", "a"])
        self.assertEqual(assignments, {first["id"]: ["a", "b"],
                                      second["id"]: ["c", "d"]})
        self.assertEqual(self.store.allocate(["b", "c", "a", "d"]), assignments)
        assignments[first["id"]].append("external mutation")
        self.assertEqual(first["devices"], ["a", "b"])

    def test_expanding_first_workspace_preserves_later_workspace_devices(self):
        first = self.store.create("First", 1)
        second = self.store.create("Second", 1)
        self.store.allocate(["a", "b"])
        self.store.update(first["id"], "First", 3)
        assigned = self.store.allocate(["a", "b", "c", "d"])
        self.assertEqual(assigned[first["id"]], ["a", "c", "d"])
        self.assertEqual(assigned[second["id"]], ["b"])

    def test_reducing_count_releases_devices_to_next_workspace(self):
        first = self.store.create("First", 2)
        second = self.store.create("Second", 2)
        self.store.allocate(["a", "b", "c"])
        self.store.update(first["id"], "First", 1)
        self.store.allocate(["a", "b", "c"])
        self.assertEqual(first["devices"], ["a"])
        self.assertEqual(second["devices"], ["c", "b"])

    def test_disconnect_and_reconnect_preserve_requested_counts(self):
        first = self.store.create("First", 2)
        second = self.store.create("Second", 2)
        self.store.allocate(["a", "b", "c", "d"])
        self.store.allocate(["b", "d"])
        self.assertEqual(first["devices"], ["b"])
        self.assertEqual(second["devices"], ["d"])
        self.assertEqual([first["device_count"], second["device_count"]], [2, 2])
        self.store.allocate(["a", "b", "c", "d"])
        self.assertEqual(first["devices"], ["b", "a"])
        self.assertEqual(second["devices"], ["d", "c"])
        self.store.allocate([])
        self.assertEqual([first["devices"], second["devices"]], [[], []])
        self.assertEqual([first["device_count"], second["device_count"]], [2, 2])

    def test_prior_conflicting_assignments_never_duplicate_a_device(self):
        first = self.store.create("First", 2)
        second = self.store.create("Second", 2)
        first["devices"] = ["a", "a"]
        second["devices"] = ["a", "b"]
        assigned = self.store.allocate(["a", "b", "c"])
        self.assertEqual(assigned, {first["id"]: ["a", "c"],
                                    second["id"]: ["b"]})


if __name__ == "__main__":
    unittest.main()
