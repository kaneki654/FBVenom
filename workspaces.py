"""Persistent, independent Comment to Comment workspace settings.

This module deliberately has no GUI, ADB, or commenting-engine dependencies.
Callers edit a workspace's links/settings and explicitly save when appropriate.
"""

import json
import os
from pathlib import Path
import tempfile
import uuid


class WorkspaceStore:
    """Manage named workspaces and allocate each connected device at most once."""

    def __init__(self, path):
        self.path = Path(path)
        self.workspaces = []

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Enter a workspace name.")
        return name.strip()

    @staticmethod
    def _count(device_count):
        if type(device_count) is not int or device_count < 1:
            raise ValueError("Device count must be a positive whole number.")
        return device_count

    @classmethod
    def _validated_workspaces(cls, workspaces):
        """Return a clean snapshot without changing the caller's objects."""
        if not isinstance(workspaces, list):
            raise ValueError("Workspaces must be a list.")
        result, ids, names = [], set(), set()
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                raise ValueError("Each workspace must be an object.")
            workspace_id = workspace.get("id")
            if (not isinstance(workspace_id, str)
                    or len(workspace_id) != 32
                    or any(c not in "0123456789abcdef" for c in workspace_id)):
                raise ValueError("Workspace ID must be a UUID hex string.")
            if workspace_id in ids:
                raise ValueError("Workspace IDs must be unique.")
            ids.add(workspace_id)
            name = cls._name(workspace.get("name"))
            if name.casefold() in names:
                raise ValueError("Workspace names must be unique.")
            names.add(name.casefold())
            count = cls._count(workspace.get("device_count"))
            links = workspace.get("links")
            if not isinstance(links, list):
                raise ValueError("Workspace links must be a list.")
            clean_links = []
            for link in links:
                if (not isinstance(link, dict)
                        or any(not isinstance(link.get(field), str)
                               for field in ("url", "reaction", "comment"))):
                    raise ValueError("Each link needs URL, reaction, and comment text.")
                clean_links.append({field: link[field]
                                    for field in ("url", "reaction", "comment")})
            screenshots = workspace.get("screenshots")
            if type(screenshots) is not bool:
                raise ValueError("Workspace screenshots must be true or false.")
            speed = workspace.get("speed")
            if not isinstance(speed, str) or speed not in ("safe", "fast", "turbo"):
                raise ValueError("Workspace speed must be safe, fast, or turbo.")
            devices = workspace.get("devices")
            if (not isinstance(devices, list)
                    or any(not isinstance(serial, str) or not serial.strip()
                           for serial in devices)
                    or len(set(devices)) != len(devices)):
                raise ValueError("Workspace devices must be unique device serials.")
            result.append({
                "id": workspace_id,
                "name": name,
                "device_count": count,
                "links": clean_links,
                "screenshots": screenshots,
                "speed": speed,
                "devices": list(devices),
            })
        return result

    def load(self):
        """Load settings; leave memory and disk untouched if validation fails."""
        try:
            with self.path.open("r", encoding="utf-8") as source:
                data = json.load(source)
        except FileNotFoundError:
            self.workspaces = []
            return
        if (not isinstance(data, dict)
                or type(data.get("version")) is not int
                or data["version"] != 1):
            raise ValueError("Unsupported workspace settings version.")
        validated = self._validated_workspaces(data.get("workspaces"))
        self.workspaces = validated

    def save(self):
        """Atomically replace the settings file after validating the full snapshot."""
        data = {"version": 1,
                "workspaces": self._validated_workspaces(self.workspaces)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as target:
                temporary_path = target.name
                json.dump(data, target, ensure_ascii=False, indent=2)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def get(self, workspace_id):
        """Return a workspace by ID, or None when it no longer exists."""
        return next((workspace for workspace in self.workspaces
                     if workspace["id"] == workspace_id), None)

    def _unique_name(self, name, except_id=None):
        name = self._name(name)
        if any(workspace["id"] != except_id
               and workspace["name"].casefold() == name.casefold()
               for workspace in self.workspaces):
            raise ValueError("A workspace with that name already exists.")
        return name

    def create(self, name, device_count):
        name = self._unique_name(name)
        device_count = self._count(device_count)
        workspace = {
            "id": uuid.uuid4().hex,
            "name": name,
            "device_count": device_count,
            "links": [{"url": "", "reaction": "like", "comment": ""}],
            "screenshots": False,
            "speed": "safe",
            "devices": [],
        }
        self.workspaces.append(workspace)
        return workspace

    def update(self, workspace_id, name, device_count):
        workspace = self.get(workspace_id)
        if workspace is None:
            raise ValueError("Workspace no longer exists.")
        name = self._unique_name(name, except_id=workspace_id)
        device_count = self._count(device_count)
        workspace.update(name=name, device_count=device_count)
        return workspace

    def remove(self, workspace_id):
        workspace = self.get(workspace_id)
        if workspace is None:
            raise ValueError("Workspace no longer exists.")
        self.workspaces.remove(workspace)

    def allocate(self, available_serials):
        """Keep available assignments, then fill vacancies in workspace order.

        Retention is a separate pass so expanding an earlier workspace cannot
        take devices already assigned to a later workspace. Sorting available
        serials makes allocation independent of device-discovery order.
        """
        available_serials = list(available_serials)
        if any(not isinstance(serial, str) or not serial.strip()
               for serial in available_serials):
            raise ValueError("Available devices must be nonempty serial strings.")
        available = set(available_serials)
        assigned, used = {}, set()
        for workspace in self.workspaces:
            count = self._count(workspace["device_count"])
            retained = []
            for serial in workspace["devices"]:
                if len(retained) >= count:
                    break
                if serial in available and serial not in used:
                    retained.append(serial)
                    used.add(serial)
            assigned[workspace["id"]] = retained
        remaining = iter(sorted(available - used))
        for workspace in self.workspaces:
            devices = assigned[workspace["id"]]
            while len(devices) < workspace["device_count"]:
                serial = next(remaining, None)
                if serial is None:
                    break
                devices.append(serial)
            workspace["devices"] = list(devices)
        return assigned
