#!/usr/bin/env python3
"""Self-check: strict must catch what basic (spec) lets through.

Builds a minimal pack in a temp dir, corrupts it in ways the loose spec
allows, and asserts basic PASSes while strict FAILs. Run: python test_fpvalidate.py
(needs pyyaml + jsonschema on the path — use afk-app/tools/.venv)."""
import json, tempfile, yaml, zipfile
from pathlib import Path
import fpvalidate as fp

# spec_info() surfaces what basic is actually checking against (VENDOR.txt's
# pin) for display in the plugin UI — must match the pin this repo ships.
info = fp.spec_info()
assert info["tag"] == "v1.14.0", info
assert info["commit"] and len(info["commit"]) == 40, info
assert info["repo"] and info["repo"].startswith("https://"), info


def _zip(src: Path, dest: Path):
    """Zip a pack dir into a *.feedpak archive with manifest.yaml at the root."""
    with zipfile.ZipFile(dest, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())


def _notation_pack(root: Path, measures):
    """A minimal pack with one arrangement that has both a tab `file` (kept
    trivially valid) and a `notation` side-file, mirroring how real packs
    (e.g. the TGA_Full_07-06_Testing.feedpak bug report) pair the two."""
    (root / "arrangements").mkdir(parents=True)
    (root / "stems").mkdir()
    (root / "stems" / "full.ogg").write_bytes(b"x")
    arr = {"name": "Keys", "tuning": [0] * 6, "templates": [],
           "notes": [{"t": 1.0, "s": 0, "f": 0}], "handshapes": [], "chords": []}
    (root / "arrangements" / "keys.json").write_text(json.dumps(arr))
    (root / "notation_keys.json").write_text(json.dumps({
        "version": 1, "staves": [{"id": "rh", "clef": "G2"}], "measures": measures,
    }))
    m = {"feedpak_version": "1.11.0", "title": "T", "artist": "A", "duration": 10.0,
         "arrangements": [{"id": "keys", "name": "Keys", "file": "arrangements/keys.json",
                            "notation": "notation_keys.json", "type": "piano"}],
         "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}]}
    (root / "manifest.yaml").write_text(yaml.safe_dump(m))


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

    # explanations: index-aligned 1:1 with errors, one plain-English sentence
    # each — not a generic boilerplate line repeated for every error.
    assert len(res["explanations"]) == len(res["errors"]), res
    assert len(set(res["explanations"])) > 1, "every error got the same explanation"
    assert res["warning_explanations"] == [], res

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

    # notation_<id>.json measure-capacity overflow (feedback: TGA_Full_07-06_
    # Testing.feedpak — a beat-grid generator stopped early and a downstream
    # measure-splitter dumped the rest of the song into one 4/4 measure).
    # No JSON Schema sums beat durations against ts, so this is schema-valid —
    # basic must PASS; strict must FAIL.
    def _beats(n, dur=4):
        return [{"t": i * 0.1, "dur": dur, "notes": [{"midi": 60}]} for i in range(n)]

    ok_measures = [{"idx": 1, "t": 0.0, "ts": [4, 4],
                     "staves": {"rh": {"voices": [{"v": 1, "beats": _beats(4)}]}}}]
    notok = Path(t) / "notationok.feedpak"
    _notation_pack(notok, ok_measures)
    assert fp.check(notok, strict=True).ok, "a measure exactly filling its time signature must pass strict"

    bad_measures = [{"idx": 1, "t": 0.0, "ts": [4, 4],
                      "staves": {"rh": {"voices": [{"v": 1, "beats": _beats(20)}]}}}]
    notbad = Path(t) / "notationbad.feedpak"
    _notation_pack(notbad, bad_measures)
    assert fp.check(notbad, strict=False).ok, \
        "loose spec: basic must PASS a schema-valid but measure-overflowing notation file"
    r = fp.check(notbad, strict=True)
    assert not r.ok, "strict must FAIL a notation measure that overflows its time signature"
    joined = "\n".join(r.errors)
    assert "notation_keys.json" in joined and "4/4" in joined and "only holds" in joined, joined

    # ts is 'omit if unchanged' (§7.6) and must carry forward — measure 2 sets
    # no ts of its own but still overflows the ts=[4,4] measure 1 established.
    carry_measures = [
        {"idx": 1, "t": 0.0, "ts": [4, 4],
         "staves": {"rh": {"voices": [{"v": 1, "beats": _beats(4)}]}}},
        {"idx": 2, "t": 1.0,
         "staves": {"rh": {"voices": [{"v": 1, "beats": _beats(20)}]}}},
    ]
    notcarry = Path(t) / "notationcarry.feedpak"
    _notation_pack(notcarry, carry_measures)
    r = fp.check(notcarry, strict=True)
    assert not r.ok, "ts must carry forward across measures that omit it"
    assert "measure 2" in "\n".join(r.errors), r.errors

# _explain(): every rule must match its own trigger text (each rule proven
# reachable) and produce a DIFFERENT sentence from its neighbors (otherwise a
# rule is dead weight — already covered by a broader one). An unmatched line
# falls back to the generic sentence rather than raising or returning empty.
_EXPLAIN_CASES = [
    "notation_keys.json: measure 2 stave 'rh' voice 1: beats sum to 5 whole note(s) but time signature 4/4 only holds 1",
    "arrangements/lead.json: notes/0: unexpected field 'xyz' — not part of the feedpak spec (a typo, or data this validator doesn't recognize)",
    "manifest.yaml: top level: required field 'title' is missing",
    "manifest.yaml: year: should be of type integer (got 'nineteen')",
    "manifest.yaml: format: value 'wav' is not allowed — must be one of ['ogg', 'mp3']",
    "manifest.yaml: title: must not be empty",
    "manifest.yaml: feedpak_version: is not in the required format",
    "handshape.chord_id=42 out of range — but this arrangement has only 5 chord template(s)",
    "arrangements/lead.json: note.s=99 out of range for 6-string tuning",
    "arrangements/lead.json: notes[3].t=2.0 < previous 5.0 (not in time order)",
    "arrangements/lead.json: handshapes[0] end_time 1.0 <= start_time 2.0",
    "duplicate arrangement id: 'lead'",
    "more than one stem marked default:true",
    "missing file referenced by manifest: arrangements/lead.json",
    "lyric_tracks[0].file missing: lyrics/en.json",
    "manifest 'cover' is not a safe relative path: '/etc/passwd'",
    "manifest 'cover' escapes the package root (symlink?): cover.jpg",
    "unsafe path inside archive: ../../etc/passwd",
    "manifest.yaml: not valid YAML (mapping values are not allowed here)",
    "arrangements/lead.json: not valid JSON (Expecting value: line 1 column 1)",
    "no manifest.yaml at package root",
    "manifest.yaml: top level must be a mapping",
    "feedpak_version is not a valid semver string: '1.0'",
    "not a directory or a zip archive",
]
explanations = [fp._explain(c) for c in _EXPLAIN_CASES]
assert all(e != fp._EXPLAIN_FALLBACK for e in explanations), \
    [c for c, e in zip(_EXPLAIN_CASES, explanations) if e == fp._EXPLAIN_FALLBACK]
assert len(set(explanations)) == len(_EXPLAIN_CASES), \
    "two trigger cases produced the same explanation — one rule is unreachable"
assert fp._explain("some future check nobody wrote a rule for yet") == fp._EXPLAIN_FALLBACK

print("ok — strict catches unknown keys, bad ranges, dangling refs, out-of-order times, "
      "notation measure overflow, per-error explanations, and all of the above in dirs and zips")
