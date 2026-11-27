"""Named prompt presets for the desktop client.

Stored in the desktop user-data directory as ``prompt-presets.json`` (file mode
0600, directory mode 0700). That directory is shared by every thread and is not
a project workspace, and it is not ``ROCKYCODE_HOME`` (model credentials live
there). A preset is only an id, a name, and a prompt body. Model choice,
permission mode, and credentials are rejected and never written.

Applying a preset is a renderer concern: it replaces the composer draft.
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

from .messages import HostError

FILENAME = "prompt-presets.json"
NAME_LIMIT = 80
BODY_LIMIT = 100_000
MAX_PRESETS = 100
ALLOWED_FIELDS = {"id", "name", "body", "updatedAt"}
SAVE_FIELDS = {"id", "name", "body"}
ID_RE = re.compile(r"^[a-f0-9]{32}$")


def normalize_name(name):
    return " ".join(name.split())


def _now():
    return int(time.time() * 1000)


def _entry(value):
    """Project one stored object onto the public fields, or drop it."""
    if not isinstance(value, dict):
        return None
    preset_id = value.get("id")
    name = value.get("name")
    body = value.get("body")
    updated = value.get("updatedAt")
    if not isinstance(preset_id, str) or not ID_RE.fullmatch(preset_id):
        return None
    if not isinstance(name, str) or not isinstance(body, str):
        return None
    name = normalize_name(name)
    if not name or len(name) > NAME_LIMIT or not body.strip() or len(body) > BODY_LIMIT:
        return None
    if type(updated) is not int:
        updated = 0
    return {"id": preset_id, "name": name, "body": body, "updatedAt": updated}


class PresetLibrary:
    def __init__(self, directory):
        self.path = Path(directory) / FILENAME
        self._items = []
        self._protected = False
        self._load()

    def list(self):
        return [dict(item) for item in self._ordered(self._items)]

    def save(self, params):
        if not isinstance(params, dict):
            raise HostError("diagnostic.invalidRequest")
        if set(params) - SAVE_FIELDS:
            raise HostError("diagnostic.presetFields")
        name = params.get("name")
        body = params.get("body")
        if not isinstance(name, str):
            raise HostError("diagnostic.presetName")
        name = normalize_name(name)
        if not name or len(name) > NAME_LIMIT:
            raise HostError("diagnostic.presetName")
        if not isinstance(body, str) or not body.strip() or len(body) > BODY_LIMIT:
            raise HostError("diagnostic.presetBody")
        items = [dict(item) for item in self._items]
        preset_id = params.get("id")
        if preset_id is None:
            if len(items) >= MAX_PRESETS:
                raise HostError("diagnostic.presetLimit")
            if any(item["name"].casefold() == name.casefold() for item in items):
                raise HostError("diagnostic.presetDuplicate")
            saved = {"id": uuid.uuid4().hex, "name": name, "body": body, "updatedAt": _now()}
            items.append(saved)
        else:
            if not isinstance(preset_id, str) or not ID_RE.fullmatch(preset_id):
                raise HostError("diagnostic.invalidRequest")
            saved = next((item for item in items if item["id"] == preset_id), None)
            if saved is None:
                raise HostError("diagnostic.presetMissing")
            if any(item["id"] != preset_id and item["name"].casefold() == name.casefold() for item in items):
                raise HostError("diagnostic.presetDuplicate")
            saved["name"] = name
            saved["body"] = body
            saved["updatedAt"] = _now()
        self._commit(items)
        return dict(saved)

    def delete(self, preset_id):
        if not isinstance(preset_id, str) or not ID_RE.fullmatch(preset_id):
            raise HostError("diagnostic.invalidRequest")
        items = [item for item in self._items if item["id"] != preset_id]
        if len(items) == len(self._items):
            raise HostError("diagnostic.presetMissing")
        self._commit(items)

    def _ordered(self, items):
        return sorted(items, key=lambda item: (item["name"].casefold(), item["id"]))

    def _load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            self._protected = True
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if data.get("version", 1) != 1:
            self._protected = True
            return
        presets = data.get("presets")
        if not isinstance(presets, list):
            return
        seen_ids = set()
        seen_names = set()
        items = []
        for entry in presets:
            item = _entry(entry)
            if item is None:
                continue
            folded = item["name"].casefold()
            if item["id"] in seen_ids or folded in seen_names:
                continue
            seen_ids.add(item["id"])
            seen_names.add(folded)
            items.append(item)
        self._items = items

    def _commit(self, items):
        if self._protected:
            raise HostError("diagnostic.presetFile")
        ordered = self._ordered(items)
        try:
            self._write(ordered)
        except OSError:
            raise HostError("diagnostic.presetFile") from None
        self._items = ordered

    def _write(self, items):
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        temporary = directory / f".prompt-presets.{uuid.uuid4().hex}.tmp"
        payload = {"version": 1, "presets": [{key: item[key] for key in ("id", "name", "body", "updatedAt")} for item in items]}
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
