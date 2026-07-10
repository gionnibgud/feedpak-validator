#!/usr/bin/env python3
"""Self-check: strict must catch what basic (spec) lets through.

Builds a minimal pack in a temp dir, corrupts it in ways the loose spec
allows, and asserts basic PASSes while strict FAILs. Run: python test_fpvalidate.py
(needs pyyaml + jsonschema on the path — use afk-app/tools/.venv)."""
import json, tempfile, yaml, zipfile
from pathlib import Path
import fpvalidate as fp


def _zip(src: Path, dest: Path):
    """Zip a pack dir into a *.feedpak archive with manifest.yaml at the root."""
    with zipfile.ZipFile(dest, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())


def _pack(root: Path, manifest_extra=None, note_s=0, chord_id=0, note_extra=None,
          notes=None, hs_end=2.0):
    (root / "arrangements").mkdir(parents=True)
    (root / "stems").mkdir()
    (root / "stems" / "full.ogg").write_bytes(b"x")
    note = {"t": 1.0, "s": note_s, "f": 0}
    note.update(note_extra or {})
    arr = {"name": "Lead", "tuning": [0]*6, "templates": [{"name": "A", "frets": [], "fingers": []}],
           "notes": notes if notes is not None else [note],
           "handshapes": [{"start_time": 1.0, "end_time": hs_end, "chord_id": chord_id, "arp": False}],
           "chords": [{"t": 1.0, "id": 0, "notes": []}]}
    (root / "arrangements" / "lead.json").write_text(json.dumps(arr))
    m = {"feedpak_version": "1.11.0", "title": "T", "artist": "A", "duration": 10.0,
         "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json", "type": "guitar"}],
         "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}]}
    if manifest_extra:
        m.update(manifest_extra)
    (root / "manifest.yaml").write_text(yaml.safe_dump(m))


with tempfile.TemporaryDirectory() as t:
    # A real §6.2 technique field (pm) the schema omits must pass strict.
    good = Path(t) / "good.feedpak"; _pack(good, note_extra={"pm": True})
    assert fp.check(good, strict=False).ok, "valid pack must pass basic"
    assert fp.check(good, strict=True).ok, "valid pack (incl. pm) must pass strict"

    # bogus_key (manifest), out-of-range string, dangling chord ref, and a
    # note field that is in NEITHER schema nor spec prose (xyz) — strict-only.
    bad = Path(t) / "bad.feedpak"
    _pack(bad, manifest_extra={"bogus_key": 1}, note_s=99, chord_id=42, note_extra={"xyz": 1})
    assert fp.check(bad, strict=False).ok, "loose spec: basic must still PASS the bad pack"
    r = fp.check(bad, strict=True)
    assert not r.ok, "strict must FAIL the bad pack"
    joined = "\n".join(r.errors)
    for token in ("bogus_key", "note.s=99", "chord_id=42", "xyz"):
        assert token in joined, f"strict missed {token!r}:\n{joined}"

    # friendly output: humanized + de-duplicated, no raw JSON-Schema jargon.
    friendly = fp._friendly(r.errors)
    ftext = "\n".join(friendly)
    assert "Additional properties are not allowed" not in ftext, ftext
    assert "unexpected field 'xyz'" in ftext and "unexpected field 'bogus_key'" in ftext, ftext
    assert len(friendly) == len(set(friendly)), f"duplicate lines in friendly output:\n{ftext}"

    # structured API for embedders (e.g. the fee[dB]ack plugin backend).
    res = fp.validate(good, strict=True)
    assert res["ok"] and res["level"] == "strict" and res["errors"] == [], res
    res = fp.validate(bad, strict=True)
    assert res["ok"] is False and any("xyz" in e for e in res["errors"]), res
    import json as _json; _json.dumps(res)  # must be JSON-serializable

    # time ordering: descending note times + an inverted handshape span. Both are
    # schema-valid (loose spec), so basic PASSes; strict must FAIL on both.
    tbad = Path(t) / "timebad.feedpak"
    _pack(tbad, notes=[{"t": 5.0, "s": 0, "f": 0}, {"t": 2.0, "s": 0, "f": 0}], hs_end=0.5)
    assert fp.check(tbad, strict=False).ok, "loose spec: basic must PASS out-of-order times"
    r = fp.check(tbad, strict=True)
    assert not r.ok, "strict must FAIL out-of-order times"
    joined = "\n".join(r.errors)
    assert "not in time order" in joined and "end_time" in joined, joined

    # zip archives: strict must run on zips too (not just directories).
    goodzip = Path(t) / "good.feedpak.zip"; _zip(good, goodzip)
    assert fp.check(goodzip, strict=True).ok, "valid zip must pass strict"
    badzip = Path(t) / "bad.feedpak.zip"; _zip(bad, badzip)
    assert not fp.check(badzip, strict=True).ok, "strict must FAIL a bad zip"

print("ok — strict catches unknown keys, bad ranges, dangling refs, out-of-order times, in dirs and zips")
