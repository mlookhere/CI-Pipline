"""Built artifacts must be traceable to a real-shaped commit and immutable payload hashes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import release_manifest as release  # noqa: E402

HEAD = "a" * 40


def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    root.mkdir()
    (root / "extension.zip").write_bytes(b"chrome-mv3-bundle")
    (root / "manifest.json").write_text('{"manifest_version":3}', encoding="utf-8")
    return root


def test_release_manifest_round_trip_and_hashes(tmp_path):
    root = bundle(tmp_path)
    assert release.create(root, HEAD) == 0
    assert release.verify(root, HEAD) == 0
    data = json.loads((root / "release-metadata.json").read_text(encoding="utf-8"))
    assert data["source_commit"] == HEAD
    assert {item["path"] for item in data["artifacts"]} == {"extension.zip", "manifest.json"}


@pytest.mark.parametrize("bad", ["", "a" * 39, "a" * 41, "not-a-git-commit"])
def test_release_creation_rejects_untrusted_commit_id(tmp_path, bad):
    with pytest.raises(SystemExit, match="complete lowercase Git SHA"):
        release.create(bundle(tmp_path), bad)


def test_wrong_expected_head_rejects_verification(tmp_path):
    root = bundle(tmp_path)
    release.create(root, HEAD)
    with pytest.raises(SystemExit, match="does not match expected"):
        release.verify(root, "b" * 40)


def test_modified_package_bytes_are_not_accepted(tmp_path):
    root = bundle(tmp_path)
    release.create(root, HEAD)
    (root / "extension.zip").write_bytes(b"changed")
    with pytest.raises(SystemExit, match="integrity check"):
        release.verify(root, HEAD)


def test_added_release_file_fails_manifest_parity(tmp_path):
    root = bundle(tmp_path)
    release.create(root, HEAD)
    (root / "untracked.zip").write_bytes(b"extra")
    with pytest.raises(SystemExit, match="contents differ"):
        release.verify(root, HEAD)


def test_checksum_list_tampering_is_detected(tmp_path):
    root = bundle(tmp_path)
    release.create(root, HEAD)
    (root / "SHA256SUMS").write_text("forged\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="SHA256SUMS"):
        release.verify(root, HEAD)


@pytest.mark.parametrize("change", ["duplicate", "bad-digest", "negative-size"])
def test_manifest_malformed_artifacts_fail_closed(tmp_path, change):
    root = bundle(tmp_path)
    release.create(root, HEAD)
    path = root / "release-metadata.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if change == "duplicate":
        data["artifacts"].append(dict(data["artifacts"][0]))
    elif change == "bad-digest":
        data["artifacts"][0]["sha256"] = "missing"
    else:
        data["artifacts"][0]["size"] = -1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate|digest|size"):
        release.verify(root, HEAD)


def test_release_source_without_payload_fails(tmp_path):
    with pytest.raises(SystemExit, match="no payload files"):
        release.create(tmp_path / "empty", HEAD)


def test_release_does_not_follow_symlinked_payload(tmp_path):
    root = bundle(tmp_path)
    (root / "link.zip").symlink_to(root / "extension.zip")
    with pytest.raises(SystemExit, match="symlinks are forbidden"):
        release.create(root, HEAD)
