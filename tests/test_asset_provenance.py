"""Asset rights, content-based typing, archive safety and previewed import."""

from __future__ import annotations

import struct
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_factory.asset_provenance import (
    ASSET_MANIFEST_PATH,
    BUDGETS,
    AssetBudget,
    AssetCandidate,
    AssetLibrary,
    AssetProvenance,
    AssetRefused,
    detect_type,
    inspect_archive,
    measure,
)


def png(width: int = 16, height: int = 16) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * 32
    )


def gif(width: int = 8, height: int = 8) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 24


def jpeg(width: int = 32, height: int = 24) -> bytes:
    return (
        b"\xff\xd8\xff\xc0" + struct.pack(">H", 17) + b"\x08"
        + struct.pack(">HH", height, width) + b"\x00" * 24
    )


def wav(seconds: float = 1.0, rate: int = 8000) -> bytes:
    channels, bits = 1, 16
    frame = channels * bits // 8
    data = b"\x00" * int(rate * frame * seconds)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * frame, frame, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


OWNED = AssetProvenance.create(source="drawn in-house", licence_id="owned")
CC_BY = AssetProvenance.create(
    source="https://example.invalid/art", licence_id="CC-BY-4.0", attribution="A. Artist",
)


class ProvenanceRightsTest(unittest.TestCase):
    def test_unknown_rights_allow_local_use_and_block_distribution(self) -> None:
        unknown = AssetProvenance.unrecorded(note="dropped in by hand")
        self.assertFalse(unknown.known)
        for operation in ("import", "edit", "internal_build"):
            allowed, _ = unknown.permits(operation)
            self.assertTrue(allowed, operation)
        for operation in ("export", "share", "publish", "sell"):
            allowed, reason = unknown.permits(operation)
            self.assertFalse(allowed, operation)
            self.assertIn("rights are unknown", reason)

    def test_attribution_licence_needs_recorded_attribution(self) -> None:
        without = AssetProvenance.create(
            source="https://example.invalid/art", licence_id="CC-BY-4.0",
        )
        allowed, reason = without.permits("export")
        self.assertFalse(allowed)
        self.assertIn("requires attribution", reason)
        self.assertTrue(CC_BY.permits("export")[0])

    def test_internal_licence_blocks_redistribution(self) -> None:
        internal = AssetProvenance.create(
            source="vendor pack", licence_id="licensed-internal", attribution="Vendor",
        )
        allowed, reason = internal.permits("share")
        self.assertFalse(allowed)
        self.assertIn("does not permit redistribution", reason)
        self.assertTrue(internal.permits("internal_build")[0])

    def test_non_commercial_licence_blocks_only_selling(self) -> None:
        licence = AssetProvenance.create(
            source="bundle", licence_id="licensed-noncommercial", attribution="Studio",
        )
        self.assertTrue(licence.permits("share")[0])
        allowed, reason = licence.permits("sell")
        self.assertFalse(allowed)
        self.assertIn("commercial", reason)

    def test_public_domain_licence_permits_everything(self) -> None:
        licence = AssetProvenance.create(source="opengameart", licence_id="CC0-1.0")
        for operation in ("export", "share", "publish", "sell"):
            self.assertTrue(licence.permits(operation)[0], operation)

    def test_invalid_provenance_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            AssetProvenance.create(source="x", licence_id="WTFPL")
        with self.assertRaises(ValueError):
            AssetProvenance.create(source="", licence_id="CC0-1.0")
        with self.assertRaises(ValueError):
            AssetProvenance.unrecorded().permits("teleport")


class TypeDetectionTest(unittest.TestCase):
    def test_supported_formats_are_recognised_by_content(self) -> None:
        cases = (
            (png(), "image", "png"),
            (jpeg(), "image", "jpeg"),
            (gif(), "image", "gif"),
            (wav(0.1), "audio", "wav"),
            (b"OggS" + b"\x00" * 32, "audio", "ogg"),
            (b"glTF" + b"\x00" * 32, "model", "glb"),
            (b"OTTO" + b"\x00" * 32, "font", "otf"),
            (b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16, "image", "webp"),
        )
        for payload, kind, fmt in cases:
            with self.subTest(fmt=fmt):
                detected = detect_type(payload, declared_name=f"a.{fmt}")
                self.assertTrue(detected.supported)
                self.assertEqual((detected.kind, detected.format), (kind, fmt))

    def test_executables_and_scripts_are_refused(self) -> None:
        for payload in (b"MZ\x90\x00", b"\x7fELF\x02", b"#!/bin/sh\nrm -rf /"):
            with self.subTest(payload=payload[:4]):
                detected = detect_type(payload, declared_name="texture.png")
                self.assertFalse(detected.supported)
                self.assertIn("executable", detected.reason)

    def test_extension_claim_does_not_override_content(self) -> None:
        detected = detect_type(png(), declared_name="sprite.jpg")
        self.assertEqual(detected.format, "png")
        self.assertTrue(detected.declared_mismatch)
        self.assertIn("claims jpeg", detected.reason)

    def test_unrecognised_content_is_refused(self) -> None:
        detected = detect_type(b"just some text", declared_name="notes.png")
        self.assertFalse(detected.supported)
        self.assertEqual(detected.format, "unrecognised")

    def test_gltf_json_is_recognised_without_a_signature(self) -> None:
        payload = b'{"asset": {"version": "2.0"}, "scenes": []}'
        self.assertEqual(detect_type(payload, declared_name="s.gltf").format, "gltf")


class MeasurementTest(unittest.TestCase):
    def test_image_and_audio_are_measured(self) -> None:
        self.assertEqual(
            measure(png(64, 32), detect_type(png(64, 32))).pixels, 2048,
        )
        self.assertEqual(measure(gif(4, 4), detect_type(gif(4, 4))).pixels, 16)
        self.assertEqual(measure(jpeg(20, 10), detect_type(jpeg(20, 10))).pixels, 200)
        self.assertAlmostEqual(
            measure(wav(0.5), detect_type(wav(0.5))).seconds, 0.5, places=2,
        )

    def test_unmeasurable_formats_say_so_instead_of_guessing(self) -> None:
        payload = b"OggS" + b"\x00" * 64
        result = measure(payload, detect_type(payload))
        self.assertIsNone(result.pixels)
        self.assertIsNone(result.seconds)
        self.assertFalse(result.record["measured"])


class ArchiveSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def archive(self, entries, *, symlink: str | None = None) -> Path:
        path = self.root / "pack.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as handle:
            for name, payload in entries:
                info = zipfile.ZipInfo(name)
                # Write the requested raw entry; Windows ZipInfo construction
                # otherwise silently turns the unsafe fixture into a safe one.
                info.filename = name
                info.compress_type = zipfile.ZIP_DEFLATED
                handle.writestr(info, payload)
            if symlink:
                info = zipfile.ZipInfo(symlink)
                info.external_attr = (0o120777 << 16)
                handle.writestr(info, "/etc/passwd")
        return path

    def test_a_clean_archive_is_safe(self) -> None:
        report = inspect_archive(self.archive([
            ("art/hero.png", png()), ("art/tile.png", png()),
        ]))
        self.assertTrue(report.safe, report.reason)
        self.assertEqual(len(report.accepted), 2)
        self.assertEqual(report.refused, ())

    def test_path_traversal_is_refused(self) -> None:
        report = inspect_archive(self.archive([("../../evil.png", png())]))
        self.assertFalse(report.safe)
        self.assertIn("escapes the destination", report.refused[0])

    def test_absolute_and_windows_paths_are_refused(self) -> None:
        for name, marker in (("/etc/hosts", "absolute"), ("C:/windows/x", "absolute")):
            with self.subTest(name=name):
                report = inspect_archive(self.archive([(name, b"x")]))
                self.assertFalse(report.safe)
                self.assertIn(marker, report.refused[0])

    def test_backslash_separators_are_refused(self) -> None:
        report = inspect_archive(self.archive([("art\\hero.png", png())]))
        self.assertFalse(report.safe)
        self.assertIn("unsafe separator", report.refused[0])

    def test_nul_in_the_original_entry_is_refused_before_zipinfo_truncates_it(self) -> None:
        report = inspect_archive(self.archive([("art/hero\x00.png", png())]))
        self.assertFalse(report.safe)
        self.assertIn("unsafe separator", report.refused[0])

    def test_symbolic_links_are_refused(self) -> None:
        report = inspect_archive(self.archive([("ok.png", png())], symlink="link.png"))
        self.assertFalse(report.safe)
        self.assertIn("symbolic link", report.refused[0])

    def test_duplicate_member_names_are_refused(self) -> None:
        report = inspect_archive(self.archive([("a.png", png()), ("a.png", png())]))
        self.assertFalse(report.safe)
        self.assertIn("duplicate", report.refused[0])

    def test_member_count_limit_is_enforced(self) -> None:
        report = inspect_archive(
            self.archive([(f"a{index}.png", png()) for index in range(5)]),
            max_members=2,
        )
        self.assertFalse(report.safe)
        self.assertIn("over the 2 limit", report.reason)

    def test_expanded_size_limit_is_enforced(self) -> None:
        report = inspect_archive(
            self.archive([("big.png", png() + b"\x00" * 20_000)]), max_total_bytes=1_000,
        )
        self.assertFalse(report.safe)
        self.assertIn("over the 1000 limit", report.reason)

    def test_decompression_bomb_ratio_is_refused(self) -> None:
        report = inspect_archive(self.archive([("bomb.png", b"0" * 4_000_000)]))
        self.assertFalse(report.safe)
        self.assertIn("decompression bomb", report.reason)

    def test_a_non_archive_is_not_treated_as_one(self) -> None:
        path = self.root / "not.zip"
        path.write_bytes(png())
        report = inspect_archive(path)
        self.assertFalse(report.safe)
        self.assertIn("not a readable zip", report.reason)

    def test_inspection_never_writes_anything(self) -> None:
        archive = self.archive([("../../evil.png", png()), ("ok.png", png())])
        before = sorted(item.name for item in self.root.iterdir())
        inspect_archive(archive)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), before)


class AssetFixture(unittest.TestCase):
    """Shared helpers; no test methods are inherited by the suites below."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "game"
        self.library = AssetLibrary(self.root, budget="baseline-pc")

    def candidate(self, name: str, payload: bytes, provenance=OWNED) -> AssetCandidate:
        return AssetCandidate(name, payload, provenance)

    def import_one(self, name="art/hero.png", payload=None, provenance=OWNED):
        candidate = self.candidate(name, payload or png(), provenance)
        plan = self.library.plan([candidate])
        return plan, self.library.apply(plan, [candidate])


class AssetImportTest(AssetFixture):
    def test_import_writes_the_file_and_records_provenance(self) -> None:
        plan, receipt = self.import_one()
        self.assertTrue(plan.safe)
        self.assertEqual(plan.preview()["import"], ["art/hero.png"])
        self.assertEqual(receipt.imported, ("art/hero.png",))
        self.assertTrue((self.root / "art/hero.png").is_file())
        record = self.library.assets()["art/hero.png"]
        self.assertEqual(record["kind"], "image")
        self.assertEqual(record["provenance"]["licence_id"], "owned")
        self.assertEqual(record["measurement"]["pixels"], 256)
        self.assertTrue((self.root / ASSET_MANIFEST_PATH).is_file())

    def test_reimporting_identical_content_keeps_it(self) -> None:
        self.import_one()
        candidate = self.candidate("art/hero.png", png())
        plan = self.library.plan([candidate])
        self.assertEqual(plan.preview()["keep"], ["art/hero.png"])
        receipt = self.library.apply(plan, [candidate])
        self.assertEqual(receipt.imported, ())
        self.assertEqual(receipt.replaced, ())

    def test_unsupported_content_is_refused_with_a_reason(self) -> None:
        candidate = self.candidate("art/tool.png", b"MZ\x90\x00binary")
        plan = self.library.plan([candidate])
        self.assertEqual(plan.accepted, ())
        self.assertIn("executable", plan.refused[0].reasons[0])
        self.library.apply(plan, [candidate])
        self.assertFalse((self.root / "art/tool.png").exists())

    def test_unsafe_asset_names_are_refused(self) -> None:
        for name in ("../escape.png", "/abs.png", "art\\hero.png"):
            with self.subTest(name=name):
                plan = self.library.plan([self.candidate(name, png())])
                self.assertEqual(plan.accepted, ())

    def test_per_asset_and_pixel_budgets_are_enforced(self) -> None:
        handheld = AssetLibrary(self.root, budget="handheld")
        oversized = handheld.plan([self.candidate("art/big.png", png(4096, 4096))])
        self.assertEqual(oversized.accepted, ())
        self.assertIn("image budget", " ".join(oversized.refused[0].reasons))

        heavy = handheld.plan([
            self.candidate("art/heavy.png", png(8, 8) + b"\x00" * (5 * 1024 * 1024)),
        ])
        self.assertIn("per-asset budget", " ".join(heavy.refused[0].reasons))

    def test_total_budget_refuses_the_apply(self) -> None:
        tiny = AssetBudget("tiny", 10_000, 200, 10**9, 10**6)
        library = AssetLibrary(self.root, budget=tiny)
        candidate = self.candidate("art/hero.png", png() + b"\x00" * 500)
        plan = library.plan([candidate])
        self.assertTrue(plan.over_total_budget)
        self.assertFalse(plan.safe)
        with self.assertRaises(AssetRefused):
            library.apply(plan, [candidate])
        self.assertFalse((self.root / "art/hero.png").exists())
        self.assertIn("handheld", BUDGETS)

    def test_a_locally_edited_asset_becomes_a_conflict(self) -> None:
        self.import_one()
        (self.root / "art/hero.png").write_bytes(png(8, 8))
        candidate = self.candidate("art/hero.png", png(32, 32))
        plan = self.library.plan([candidate])
        self.assertEqual(plan.conflicts, ("art/hero.png",))
        self.assertFalse(plan.safe)
        with self.assertRaises(AssetRefused):
            self.library.apply(plan, [candidate])
        self.assertEqual((self.root / "art/hero.png").read_bytes(), png(8, 8))

    def test_overwriting_a_conflict_needs_an_approver(self) -> None:
        self.import_one()
        (self.root / "art/hero.png").write_bytes(png(8, 8))
        candidate = self.candidate("art/hero.png", png(32, 32))
        plan = self.library.plan([candidate])
        with self.assertRaises(ValueError):
            self.library.apply(plan, [candidate], approved_overwrites=("art/hero.png",))
        receipt = self.library.apply(
            plan, [candidate], approved_overwrites=("art/hero.png",), actor="miha",
        )
        self.assertEqual(receipt.replaced, ("art/hero.png",))
        self.assertEqual((self.root / "art/hero.png").read_bytes(), png(32, 32))

    def test_approval_must_name_a_conflict(self) -> None:
        candidate = self.candidate("art/hero.png", png())
        plan = self.library.plan([candidate])
        with self.assertRaises(ValueError):
            self.library.apply(
                plan, [candidate], approved_overwrites=("art/other.png",), actor="miha",
            )

    def test_apply_requires_the_reviewed_payload(self) -> None:
        candidate = self.candidate("art/hero.png", png())
        plan = self.library.plan([candidate])
        with self.assertRaises(ValueError):
            self.library.apply(plan, [self.candidate("art/hero.png", png(8, 8))])

    def test_duplicate_names_in_one_import_are_refused(self) -> None:
        plan = self.library.plan([
            self.candidate("art/hero.png", png()),
            self.candidate("art/hero.png", png(8, 8)),
        ])
        self.assertEqual(len(plan.accepted), 1)
        self.assertIn("duplicate", " ".join(plan.refused[0].reasons))


class RollbackTest(AssetFixture):
    def test_rollback_removes_new_assets(self) -> None:
        _, receipt = self.import_one()
        restored = self.library.rollback(receipt)
        self.assertEqual(restored, ("art/hero.png",))
        self.assertFalse((self.root / "art/hero.png").exists())
        self.assertEqual(self.library.assets(), {})

    def test_rollback_restores_the_previous_bytes(self) -> None:
        self.import_one()
        original = (self.root / "art/hero.png").read_bytes()
        replacement = self.candidate("art/hero.png", png(64, 64))
        plan = self.library.plan([replacement])
        self.assertEqual(plan.preview()["replace"], ["art/hero.png"])
        receipt = self.library.apply(plan, [replacement])
        self.assertEqual((self.root / "art/hero.png").read_bytes(), png(64, 64))

        self.library.rollback(receipt)
        self.assertEqual((self.root / "art/hero.png").read_bytes(), original)
        self.assertIn("art/hero.png", self.library.assets())


class LibraryRightsTest(AssetFixture):
    def test_one_unknown_asset_blocks_export_for_the_project(self) -> None:
        self.import_one("art/hero.png", png(), OWNED)
        self.import_one("art/found.png", png(8, 8), AssetProvenance.unrecorded())

        verdict = self.library.rights_check("export")
        self.assertFalse(verdict.allowed)
        self.assertEqual([name for name, _ in verdict.blocking], ["art/found.png"])
        self.assertTrue(self.library.rights_check("internal_build").allowed)
        self.assertTrue(
            self.library.rights_check("export", names=["art/hero.png"]).allowed
        )

    def test_recording_the_licence_unblocks_export(self) -> None:
        self.import_one("art/hero.png", png(), CC_BY)
        self.assertTrue(self.library.rights_check("export").allowed)
        self.assertEqual(
            self.library.provenance("art/hero.png").licence_id, "CC-BY-4.0",
        )

    def test_unknown_asset_is_refused_for_selling_even_when_shared(self) -> None:
        self.import_one(
            "art/hero.png", png(),
            AssetProvenance.create(
                source="bundle", licence_id="licensed-noncommercial", attribution="S",
            ),
        )
        self.assertTrue(self.library.rights_check("share").allowed)
        self.assertFalse(self.library.rights_check("sell").allowed)

    def test_unknown_operation_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.library.rights_check("mint")

    def test_missing_asset_has_no_provenance(self) -> None:
        with self.assertRaises(KeyError):
            self.library.provenance("art/nothing.png")

    def test_unreadable_manifest_is_treated_as_empty(self) -> None:
        self.import_one()
        (self.root / ASSET_MANIFEST_PATH).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.library.assets(), {})
        self.assertEqual(self.library.manifest()["budget"], "baseline-pc")


if __name__ == "__main__":
    unittest.main()
