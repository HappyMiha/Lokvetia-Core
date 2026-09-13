"""Project-neutral asset provenance, safe import and hardware budgets.

Three separate questions are kept separate here. May this file be used at all
(type and archive safety)? Do we have the rights to ship it (provenance)? Will
it fit the target machine (budget)? An asset with unknown rights can still be
imported and used locally; it is the operations that need rights - export,
share, publish, sell - that it blocks.

Nothing in this module executes an asset, hands one to a plugin, or trusts a
file extension. Types are decided by content, and an archive is inspected and
reported before a single member is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

UNKNOWN = "unknown"
ASSET_MANIFEST_PATH = ".lokvetia/assets.json"
ASSET_BACKUP_DIR = ".lokvetia/asset-backups"
RIGHTS_OPERATIONS = (
    "import", "edit", "internal_build", "export", "share", "publish", "sell",
)
LOCAL_OPERATIONS = frozenset({"import", "edit", "internal_build"})
MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_RATIO = 120
MAX_MEMBER_NAME = 200


class AssetRefused(PermissionError):
    """Raised when an asset or archive member may not be imported."""


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Licence:
    licence_id: str
    name: str
    redistribute: bool
    modify: bool
    commercial: bool
    attribution_required: bool


LICENCES: Mapping[str, Licence] = {
    licence.licence_id: licence
    for licence in (
        Licence(UNKNOWN, "Unknown or unrecorded", False, False, False, False),
        Licence("CC0-1.0", "Creative Commons Zero", True, True, True, False),
        Licence("CC-BY-4.0", "Creative Commons Attribution", True, True, True, True),
        Licence("CC-BY-SA-4.0", "Creative Commons Attribution-ShareAlike", True, True, True, True),
        Licence("OFL-1.1", "SIL Open Font License", True, True, True, True),
        Licence("MIT", "MIT License", True, True, True, True),
        Licence("Apache-2.0", "Apache License 2.0", True, True, True, True),
        Licence("owned", "Created by the project owner", True, True, True, False),
        Licence("licensed-noncommercial", "Licensed for non-commercial use", True, True, False, True),
        Licence("licensed-internal", "Licensed for internal use only", False, True, False, True),
    )
}


@dataclass(frozen=True)
class AssetProvenance:
    """Where an asset came from and what may be done with it."""

    source: str
    licence_id: str
    attribution: str = ""
    acquired_at: str = ""
    note: str = ""

    @classmethod
    def create(
        cls,
        *,
        source: str,
        licence_id: str,
        attribution: str = "",
        acquired_at: str = "",
        note: str = "",
    ) -> "AssetProvenance":
        licence = str(licence_id).strip()
        if licence not in LICENCES:
            raise ValueError(f"Unknown licence identifier: {licence_id!r}")
        origin = str(source).strip()
        if licence != UNKNOWN and not origin:
            raise ValueError("A licensed asset must record where it came from")
        return cls(
            origin[:500], licence, str(attribution).strip()[:300],
            str(acquired_at).strip()[:64] or _stamp(), str(note).strip()[:500],
        )

    @classmethod
    def unrecorded(cls, *, note: str = "") -> "AssetProvenance":
        """An honest 'we do not know' marker, never a silent default."""
        return cls("", UNKNOWN, "", _stamp(), str(note).strip()[:500])

    @property
    def licence(self) -> Licence:
        return LICENCES[self.licence_id]

    @property
    def known(self) -> bool:
        return self.licence_id != UNKNOWN

    @property
    def attribution_satisfied(self) -> bool:
        return not self.licence.attribution_required or bool(self.attribution)

    def permits(self, operation: str) -> tuple[bool, str]:
        if operation not in RIGHTS_OPERATIONS:
            raise ValueError(f"Unknown rights operation: {operation!r}")
        if operation in LOCAL_OPERATIONS:
            return True, "local use does not require recorded rights"
        if not self.known:
            return False, "rights are unknown; record the source and licence first"
        if not self.attribution_satisfied:
            return False, f"{self.licence_id} requires attribution and none is recorded"
        if not self.licence.redistribute:
            return False, f"{self.licence_id} does not permit redistribution"
        if operation == "sell" and not self.licence.commercial:
            return False, f"{self.licence_id} does not permit commercial use"
        return True, f"{self.licence_id} permits {operation}"

    @property
    def record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "licence_id": self.licence_id,
            "attribution": self.attribution,
            "acquired_at": self.acquired_at,
            "note": self.note,
            "known": self.known,
            "attribution_satisfied": self.attribution_satisfied,
        }


# --------------------------------------------------------------- type sniffing

@dataclass(frozen=True)
class DetectedType:
    kind: str
    format: str
    supported: bool
    reason: str
    declared_mismatch: bool = False


_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image", "png"),
    (b"\xff\xd8\xff", "image", "jpeg"),
    (b"GIF87a", "image", "gif"),
    (b"GIF89a", "image", "gif"),
    (b"OggS", "audio", "ogg"),
    (b"fLaC", "audio", "flac"),
    (b"glTF", "model", "glb"),
    (b"\x00\x01\x00\x00", "font", "ttf"),
    (b"OTTO", "font", "otf"),
    (b"true", "font", "ttf"),
)
_EXTENSION_KIND = {
    ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp",
    ".ogg": "ogg", ".flac": "flac", ".wav": "wav", ".glb": "glb", ".gltf": "gltf",
    ".ttf": "ttf", ".otf": "otf",
}
EXECUTABLE_SIGNATURES = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"#!")


def detect_type(payload: bytes, *, declared_name: str = "") -> DetectedType:
    """Decide the type from content. The file name is a claim, not evidence."""
    if any(payload.startswith(marker) for marker in EXECUTABLE_SIGNATURES):
        return DetectedType("unsupported", "executable", False, "executable content is refused")
    detected: tuple[str, str] | None = None
    if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        detected = ("audio", "wav")
    elif payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        detected = ("image", "webp")
    else:
        for signature, kind, fmt in _SIGNATURES:
            if payload.startswith(signature):
                detected = (kind, fmt)
                break
    if detected is None and payload[:1] == b"{":
        try:
            document = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict) and "asset" in document and "scenes" in document:
            detected = ("model", "gltf")
    if detected is None:
        return DetectedType(
            "unsupported", "unrecognised", False,
            "content does not match any supported asset format",
        )
    kind, fmt = detected
    suffix = Path(str(declared_name)).suffix.casefold()
    claimed = _EXTENSION_KIND.get(suffix)
    mismatch = bool(claimed) and claimed != fmt
    reason = (
        f"content is {fmt}, but the name claims {claimed}" if mismatch
        else f"content is {fmt}"
    )
    return DetectedType(kind, fmt, True, reason, mismatch)


# ------------------------------------------------------------------- budgets

@dataclass(frozen=True)
class AssetBudget:
    profile: str
    max_asset_bytes: int
    max_total_bytes: int
    max_image_pixels: int
    max_audio_seconds: float

    @property
    def record(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "max_asset_bytes": self.max_asset_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_image_pixels": self.max_image_pixels,
            "max_audio_seconds": self.max_audio_seconds,
        }


BUDGETS: Mapping[str, AssetBudget] = {
    budget.profile: budget
    for budget in (
        AssetBudget("baseline-pc", 32 * 1024 * 1024, 512 * 1024 * 1024, 4096 * 4096, 600.0),
        AssetBudget("low-end-laptop", 8 * 1024 * 1024, 128 * 1024 * 1024, 2048 * 2048, 300.0),
        AssetBudget("handheld", 4 * 1024 * 1024, 64 * 1024 * 1024, 1024 * 1024, 180.0),
    )
}


@dataclass(frozen=True)
class Measurement:
    pixels: int | None = None
    seconds: float | None = None

    @property
    def record(self) -> dict[str, object]:
        return {
            "pixels": self.pixels,
            "seconds": self.seconds,
            "measured": self.pixels is not None or self.seconds is not None,
        }


def measure(payload: bytes, detected: DetectedType) -> Measurement:
    """Measure only what this module can actually read. Never guess."""
    if detected.format == "png" and len(payload) >= 24 and payload[12:16] == b"IHDR":
        width, height = struct.unpack(">II", payload[16:24])
        return Measurement(pixels=int(width) * int(height))
    if detected.format == "jpeg":
        size = _jpeg_size(payload)
        if size:
            return Measurement(pixels=size[0] * size[1])
    if detected.format == "gif" and len(payload) >= 10:
        width, height = struct.unpack("<HH", payload[6:10])
        return Measurement(pixels=int(width) * int(height))
    if detected.format == "wav":
        seconds = _wav_seconds(payload)
        if seconds is not None:
            return Measurement(seconds=seconds)
    return Measurement()


def _jpeg_size(payload: bytes) -> tuple[int, int] | None:
    index = 2
    limit = len(payload)
    while index + 9 < limit:
        if payload[index] != 0xFF:
            index += 1
            continue
        marker = payload[index + 1]
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
            height, width = struct.unpack(">HH", payload[index + 5:index + 9])
            return int(width), int(height)
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack(">H", payload[index + 2:index + 4])[0]
        if length < 2:
            return None
        index += 2 + length
    return None


def _wav_seconds(payload: bytes) -> float | None:
    if len(payload) < 44 or payload[:4] != b"RIFF":
        return None
    index = 12
    rate = channels = bits = 0
    while index + 8 <= len(payload):
        chunk = payload[index:index + 4]
        size = struct.unpack("<I", payload[index + 4:index + 8])[0]
        body = payload[index + 8:index + 8 + size]
        if chunk == b"fmt " and len(body) >= 16:
            channels = struct.unpack("<H", body[2:4])[0]
            rate = struct.unpack("<I", body[4:8])[0]
            bits = struct.unpack("<H", body[14:16])[0]
        elif chunk == b"data" and rate and channels and bits:
            frame = channels * (bits // 8)
            return round(size / (rate * frame), 3) if frame else None
        index += 8 + size + (size % 2)
    return None


# ---------------------------------------------------------- archive inspection

@dataclass(frozen=True)
class ArchiveMember:
    name: str
    declared_bytes: int
    compressed_bytes: int
    accepted: bool
    reason: str


@dataclass(frozen=True)
class ArchiveReport:
    path: str
    members: tuple[ArchiveMember, ...]
    refused: tuple[str, ...]
    safe: bool
    reason: str

    @property
    def accepted(self) -> tuple[ArchiveMember, ...]:
        return tuple(member for member in self.members if member.accepted)

    @property
    def record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "safe": self.safe,
            "reason": self.reason,
            "members": [
                {
                    "name": member.name, "accepted": member.accepted,
                    "declared_bytes": member.declared_bytes, "reason": member.reason,
                }
                for member in self.members
            ],
            "refused": list(self.refused),
        }


def inspect_archive(
    path: Path, *, max_members: int = MAX_ARCHIVE_MEMBERS,
    max_total_bytes: int = 512 * 1024 * 1024, max_ratio: int = MAX_ARCHIVE_RATIO,
) -> ArchiveReport:
    """Report what an archive contains. Nothing is extracted here."""
    archive_path = Path(path)
    if not zipfile.is_zipfile(archive_path):
        return ArchiveReport(str(archive_path), (), (), False, "not a readable zip archive")
    members: list[ArchiveMember] = []
    refused: list[str] = []
    seen: set[str] = set()
    declared_total = 0
    compressed_total = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > max_members:
                return ArchiveReport(
                    str(archive_path), (), (),
                    False, f"archive declares {len(entries)} members, over the {max_members} limit",
                )
            for info in entries:
                reason = _member_refusal(info, seen)
                declared_total += int(info.file_size)
                compressed_total += int(info.compress_size)
                accepted = reason == ""
                if accepted:
                    seen.add(info.filename.casefold())
                else:
                    refused.append(f"{info.filename}: {reason}")
                members.append(ArchiveMember(
                    info.filename, int(info.file_size), int(info.compress_size),
                    accepted, reason or "member path and type are safe",
                ))
    except (OSError, zipfile.BadZipFile) as exc:
        return ArchiveReport(
            str(archive_path), (), (), False, f"archive could not be read: {type(exc).__name__}",
        )
    if declared_total > max_total_bytes:
        return ArchiveReport(
            str(archive_path), tuple(members), tuple(refused), False,
            f"archive expands to {declared_total} bytes, over the {max_total_bytes} limit",
        )
    if compressed_total and declared_total / max(compressed_total, 1) > max_ratio:
        return ArchiveReport(
            str(archive_path), tuple(members), tuple(refused), False,
            "archive compression ratio looks like a decompression bomb",
        )
    safe = not refused
    return ArchiveReport(
        str(archive_path), tuple(members), tuple(refused), safe,
        "every member is safe to extract" if safe
        else f"{len(refused)} member(s) refused",
    )


def _member_refusal(info: zipfile.ZipInfo, seen: set[str]) -> str:
    # ZipInfo normalizes Windows separators and truncates NULs in filename.
    # Validate the original archive entry before either transformation.
    name = info.orig_filename
    if not name or len(name) > MAX_MEMBER_NAME:
        return "member name is empty or too long"
    if "\x00" in name or "\\" in name:
        return "member name contains an unsafe separator"
    if name.endswith("/"):
        return _safe_path(name.rstrip("/"))
    problem = _safe_path(name)
    if problem:
        return problem
    # Only a writer that recorded Unix permissions gives us a file type to judge;
    # a zero type is "unspecified", not "irregular".
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        return "member is a symbolic link"
    if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        return "member is not a regular file"
    if name.casefold() in seen:
        return "duplicate member name"
    return ""


def _safe_path(name: str) -> str:
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return "member path is absolute"
    parts = name.split("/")
    if any(part == ".." for part in parts):
        return "member path escapes the destination"
    if any(part in {"", "."} for part in parts[:-1]):
        return "member path is not normalised"
    return ""


# ------------------------------------------------------------- import pipeline

@dataclass(frozen=True)
class AssetCandidate:
    name: str
    payload: bytes
    provenance: AssetProvenance

    @property
    def digest(self) -> str:
        return _digest(self.payload)


@dataclass(frozen=True)
class AssetDecision:
    name: str
    action: str
    accepted: bool
    kind: str
    format: str
    size_bytes: int
    digest: str
    measurement: Measurement
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AssetImportPlan:
    root: str
    budget: AssetBudget
    decisions: tuple[AssetDecision, ...]
    total_bytes_after: int
    over_total_budget: bool

    def _names(self, action: str) -> tuple[str, ...]:
        return tuple(item.name for item in self.decisions if item.action == action)

    @property
    def accepted(self) -> tuple[AssetDecision, ...]:
        return tuple(item for item in self.decisions if item.accepted)

    @property
    def refused(self) -> tuple[AssetDecision, ...]:
        return tuple(item for item in self.decisions if not item.accepted)

    @property
    def conflicts(self) -> tuple[str, ...]:
        return self._names("conflict")

    @property
    def safe(self) -> bool:
        return not self.conflicts and not self.over_total_budget

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical({
            "root": self.root,
            "budget": self.budget.profile,
            "decisions": [
                {
                    "name": item.name, "action": item.action,
                    "accepted": item.accepted, "digest": item.digest,
                }
                for item in self.decisions
            ],
        }).encode("utf-8")).hexdigest()

    def preview(self) -> dict[str, object]:
        return {
            "root": self.root,
            "budget": self.budget.record,
            "plan_digest": self.digest,
            "import": list(self._names("import")),
            "replace": list(self._names("replace")),
            "keep": list(self._names("keep")),
            "conflict": list(self.conflicts),
            "refused": [
                {"name": item.name, "reasons": list(item.reasons)}
                for item in self.refused
            ],
            "total_bytes_after": self.total_bytes_after,
            "over_total_budget": self.over_total_budget,
            "safe": self.safe,
        }


@dataclass(frozen=True)
class AssetImportReceipt:
    plan_digest: str
    root: str
    imported: tuple[str, ...]
    replaced: tuple[str, ...]
    refused: tuple[str, ...]
    backup_id: str
    actor: str
    recorded_at: str


@dataclass(frozen=True)
class RightsVerdict:
    operation: str
    allowed: bool
    blocking: tuple[tuple[str, str], ...] = field(default=())

    @property
    def record(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "allowed": self.allowed,
            "blocking": [{"asset": name, "reason": reason} for name, reason in self.blocking],
        }


class AssetLibrary:
    """Imports assets into one project folder, previewing and never executing."""

    def __init__(self, root: Path, *, budget: AssetBudget | str = "baseline-pc"):
        self.root = Path(root)
        resolved = BUDGETS[budget] if isinstance(budget, str) else budget
        self.budget = resolved

    # ------------------------------------------------------------- manifest

    def manifest(self) -> dict[str, object]:
        path = self.root / ASSET_MANIFEST_PATH
        if not path.is_file():
            return {"budget": self.budget.profile, "assets": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"budget": self.budget.profile, "assets": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("assets"), dict):
            return {"budget": self.budget.profile, "assets": {}}
        return payload

    def assets(self) -> dict[str, dict[str, object]]:
        return dict(self.manifest()["assets"])  # type: ignore[arg-type]

    def provenance(self, name: str) -> AssetProvenance:
        record = self.assets().get(str(name))
        if record is None:
            raise KeyError(f"Unknown asset: {name}")
        entry = record.get("provenance", {})
        return AssetProvenance(
            str(entry.get("source", "")), str(entry.get("licence_id", UNKNOWN)),
            str(entry.get("attribution", "")), str(entry.get("acquired_at", "")),
            str(entry.get("note", "")),
        )

    # ---------------------------------------------------------------- rights

    def rights_check(self, operation: str, *, names: Sequence[str] | None = None) -> RightsVerdict:
        """Whether every selected asset permits an operation, and what blocks it."""
        if operation not in RIGHTS_OPERATIONS:
            raise ValueError(f"Unknown rights operation: {operation!r}")
        selected = list(names) if names is not None else sorted(self.assets())
        blocking: list[tuple[str, str]] = []
        for name in selected:
            allowed, reason = self.provenance(name).permits(operation)
            if not allowed:
                blocking.append((name, reason))
        return RightsVerdict(operation, not blocking, tuple(blocking))

    # ------------------------------------------------------------------ plan

    def plan(self, candidates: Iterable[AssetCandidate]) -> AssetImportPlan:
        recorded = self.assets()
        existing_bytes = sum(
            int(entry.get("size_bytes", 0)) for entry in recorded.values()
        )
        decisions: list[AssetDecision] = []
        seen: set[str] = set()
        added_bytes = 0
        for candidate in candidates:
            decisions.append(self._decide(candidate, recorded, seen))
            if decisions[-1].accepted and decisions[-1].action == "import":
                added_bytes += decisions[-1].size_bytes
            elif decisions[-1].accepted and decisions[-1].action == "replace":
                previous = int(recorded.get(candidate.name, {}).get("size_bytes", 0))
                added_bytes += decisions[-1].size_bytes - previous
        total_after = existing_bytes + added_bytes
        return AssetImportPlan(
            str(self.root), self.budget, tuple(decisions), total_after,
            total_after > self.budget.max_total_bytes,
        )

    def _decide(
        self,
        candidate: AssetCandidate,
        recorded: Mapping[str, Mapping[str, object]],
        seen: set[str],
    ) -> AssetDecision:
        reasons: list[str] = []
        name = str(candidate.name)
        path_problem = _safe_path(name.replace("\\", "/")) if name else "asset name is empty"
        if "\\" in name:
            path_problem = path_problem or "asset name contains an unsafe separator"
        detected = detect_type(candidate.payload, declared_name=name)
        measurement = measure(candidate.payload, detected)
        size = len(candidate.payload)
        if path_problem:
            reasons.append(path_problem)
        if name.casefold() in seen:
            reasons.append("duplicate asset name in this import")
        seen.add(name.casefold())
        if not detected.supported:
            reasons.append(detected.reason)
        elif detected.declared_mismatch:
            reasons.append(detected.reason)
        if size > self.budget.max_asset_bytes:
            reasons.append(
                f"{size} bytes is over the {self.budget.profile} per-asset budget"
            )
        if measurement.pixels is not None and measurement.pixels > self.budget.max_image_pixels:
            reasons.append(
                f"{measurement.pixels} pixels is over the {self.budget.profile} image budget"
            )
        if measurement.seconds is not None and measurement.seconds > self.budget.max_audio_seconds:
            reasons.append(
                f"{measurement.seconds}s is over the {self.budget.profile} audio budget"
            )
        if reasons:
            action = "refused"
        elif name not in recorded:
            action = "import"
        elif str(recorded[name].get("digest")) == candidate.digest:
            action = "keep"
            reasons.append("already imported with the same content")
        elif self._on_disk_digest(name) == str(recorded[name].get("digest")):
            action = "replace"
        else:
            action = "conflict"
            reasons.append("the file in the project changed after it was imported")
        return AssetDecision(
            name, action, action in {"import", "replace", "keep"}, detected.kind,
            detected.format, size, candidate.digest, measurement, tuple(reasons),
        )

    def _on_disk_digest(self, name: str) -> str:
        path = self.root / name
        if not path.is_file():
            return "absent"
        try:
            return _digest(path.read_bytes())
        except OSError:
            return "unreadable"

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        plan: AssetImportPlan,
        candidates: Sequence[AssetCandidate],
        *,
        approved_overwrites: Sequence[str] = (),
        actor: str = "",
    ) -> AssetImportReceipt:
        if str(self.root) != plan.root:
            raise ValueError("The plan was produced for a different project")
        payloads = {candidate.name: candidate for candidate in candidates}
        approved = {str(value) for value in approved_overwrites}
        unknown = approved - set(plan.conflicts)
        if unknown:
            raise ValueError(
                "Approved overwrites must be conflicts from this plan: "
                + ", ".join(sorted(unknown))
            )
        unapproved = [name for name in plan.conflicts if name not in approved]
        if unapproved:
            raise AssetRefused(
                "These project files changed after import and were not approved for "
                "overwrite: " + ", ".join(sorted(unapproved))
            )
        if approved and not actor.strip():
            raise ValueError("Overwriting changed project files requires a named approver")
        if plan.over_total_budget:
            raise AssetRefused(
                f"the import would use {plan.total_bytes_after} bytes, over the "
                f"{self.budget.profile} total budget of {self.budget.max_total_bytes}"
            )
        backup_id = plan.digest[:16]
        backup_root = self.root / ASSET_BACKUP_DIR / backup_id
        manifest = self.manifest()
        assets = dict(manifest.get("assets", {}))
        imported: list[str] = []
        replaced: list[str] = []
        for decision in plan.decisions:
            if decision.action in {"refused", "keep"}:
                continue
            if decision.action == "conflict" and decision.name not in approved:
                continue
            candidate = payloads.get(decision.name)
            if candidate is None or candidate.digest != decision.digest:
                raise ValueError(f"No reviewed payload supplied for {decision.name}")
            destination = self.root / decision.name
            if destination.is_file():
                self._backup(backup_root, decision.name, destination)
                replaced.append(decision.name)
            else:
                imported.append(decision.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(candidate.payload)
            assets[decision.name] = {
                "digest": decision.digest,
                "size_bytes": decision.size_bytes,
                "kind": decision.kind,
                "format": decision.format,
                "measurement": decision.measurement.record,
                "provenance": candidate.provenance.record,
                "imported_at": _stamp(),
            }
        recorded_at = _stamp()
        manifest = {
            "budget": self.budget.profile,
            "updated_at": recorded_at,
            "last_backup_id": backup_id,
            "assets": assets,
        }
        self._write_manifest(manifest)
        return AssetImportReceipt(
            plan.digest, str(self.root), tuple(imported), tuple(replaced),
            tuple(item.name for item in plan.refused), backup_id,
            actor.strip(), recorded_at,
        )

    def rollback(self, receipt: AssetImportReceipt) -> tuple[str, ...]:
        """Undo one import: restore replaced files and remove newly added ones."""
        backup_root = self.root / ASSET_BACKUP_DIR / receipt.backup_id
        manifest = self.manifest()
        assets = dict(manifest.get("assets", {}))
        restored: list[str] = []
        for name in receipt.replaced:
            source = backup_root / name
            if not source.is_file():
                raise AssetRefused(f"No backup is available for {name}")
            (self.root / name).write_bytes(source.read_bytes())
            restored.append(name)
        for name in receipt.imported:
            target = self.root / name
            if target.is_file():
                target.unlink()
            assets.pop(name, None)
            restored.append(name)
        for name in receipt.replaced:
            record = assets.get(name)
            if record is not None:
                record["digest"] = self._on_disk_digest(name)
                record["size_bytes"] = (self.root / name).stat().st_size
        manifest["assets"] = assets
        manifest["updated_at"] = _stamp()
        manifest["rolled_back"] = receipt.backup_id
        self._write_manifest(manifest)
        return tuple(sorted(restored))

    def _backup(self, backup_root: Path, name: str, source: Path) -> None:
        destination = backup_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    def _write_manifest(self, manifest: Mapping[str, object]) -> None:
        path = self.root / ASSET_MANIFEST_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".lokvetia-tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
        os.replace(temporary, path)
