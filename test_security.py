#!/usr/bin/env python3
"""Security regressions for fpvalidate + routes — the paths that used to be
untested: zip-slip, decompression bombs, a manifest that crashes strict, route
path-containment, and host-path leakage in the /validate response.

Run: python -m pytest test_security.py   (or: python test_security.py)
"""
import io
import json
import logging
import tempfile
import zipfile
from pathlib import Path

import fpvalidate as fp


def _make_zip(dest: Path, entries):
    """entries: list of (arcname, data-bytes). Uses writestr so arbitrary
    (even unsafe) arcnames and large declared sizes are possible."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)


def _no_extract(monkeypatch):
    """Patch ZipFile.extractall to record calls; returns the call list so a
    test can assert extraction never happened."""
    calls = []
    orig = zipfile.ZipFile.extractall

    def spy(self, *a, **k):
        calls.append(True)
        return orig(self, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "extractall", spy)
    return calls


def test_zip_slip_rejected_without_extraction(tmp_path, monkeypatch):
    calls = _no_extract(monkeypatch)
    z = tmp_path / "evil.feedpak"
    _make_zip(z, [("../evil.txt", b"pwned")])
    rep = fp.check(z, strict=False)
    assert not rep.ok
    assert any("unsafe path inside archive" in e for e in rep.errors), rep.errors
    assert calls == [], "zip-slip archive must not be extracted"


def test_zip_bomb_uncompressed_cap(tmp_path, monkeypatch):
    calls = _no_extract(monkeypatch)
    monkeypatch.setattr(fp, "_MAX_ARCHIVE_UNCOMPRESSED", 100)
    z = tmp_path / "bomb.feedpak"
    _make_zip(z, [("big.bin", b"\0" * 200)])  # file_size=200 > 100, tiny on disk
    rep = fp.check(z, strict=False)
    assert not rep.ok
    assert any("too large uncompressed" in e for e in rep.errors), rep.errors
    assert calls == [], "over-cap archive must not be extracted"


def test_zip_too_many_entries_cap(tmp_path, monkeypatch):
    calls = _no_extract(monkeypatch)
    monkeypatch.setattr(fp, "_MAX_ARCHIVE_ENTRIES", 2)
    z = tmp_path / "many.feedpak"
    _make_zip(z, [(f"f{i}.txt", b"x") for i in range(3)])
    rep = fp.check(z, strict=False)
    assert not rep.ok
    assert any("too many entries" in e for e in rep.errors), rep.errors
    assert calls == [], "over-count archive must not be extracted"


def test_malformed_yaml_manifest_strict_does_not_crash(tmp_path):
    pack = tmp_path / "bad.feedpak"
    pack.mkdir()
    # Invalid YAML: a mapping value where none is allowed.
    (pack / "manifest.yaml").write_text("title: a: b: c\n")
    res = fp.validate(str(pack), strict=True)   # must not raise
    assert res["ok"] is False
    assert res["level"] == "strict"
    json.dumps(res)  # still JSON-serializable


def test_routes_is_within_containment(tmp_path):
    import routes
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "a.feedpak"
    inside.write_bytes(b"x")
    assert routes._is_within(root, inside)
    # A traversal path resolving outside the root is rejected.
    assert not routes._is_within(root, root / ".." / "b.feedpak")
    # A symlink pointing outside the root is rejected (resolve() follows it).
    outside = tmp_path / "outside.feedpak"
    outside.write_bytes(b"x")
    link = root / "link.feedpak"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return  # platform without symlink support — the .. case above suffices
    assert not routes._is_within(root, link)


def test_validate_response_has_no_host_path():
    """The /validate result's `pack` must be the library name, not the absolute
    server path the underlying validator labels it with."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes

    examples = Path(__file__).resolve().parent / "vendor" / "feedpak-spec" / "examples"
    app = FastAPI()
    routes.setup(app, {
        "log": logging.getLogger("test-sec"),
        "load_sibling": lambda name: __import__(name),
        "get_dlc_dir": lambda: str(examples),
    })
    client = TestClient(app)
    base = "/api/plugins/feedback-validator"
    pid = next(p["id"] for p in client.get(f"{base}/packs").json()["items"]
               if p["name"] == "minimal.feedpak")
    r = client.post(f"{base}/validate", json={"ids": [pid], "strict": True}).json()
    pack = r["results"][0]["pack"]
    assert pack == "minimal.feedpak", pack
    assert "/" not in pack and str(examples) not in pack, pack


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
