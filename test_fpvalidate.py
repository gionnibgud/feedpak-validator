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

def test_spec_info():
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


def _notation_pack(root: Path, measures, staves=None):
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
        "version": 1, "staves": staves if staves is not None else [{"id": "rh", "clef": "G2"}],
        "measures": measures,
    }))
    m = {"feedpak_version": "1.11.0", "title": "T", "artist": "A", "duration": 10.0,
         "arrangements": [{"id": "keys", "name": "Keys", "file": "arrangements/keys.json",
                            "notation": "notation_keys.json", "type": "piano"}],
         "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}]}
    (root / "manifest.yaml").write_text(yaml.safe_dump(m))


def _pack(root: Path, manifest_extra=None, note_s=0, chord_id=0, note_extra=None,
          notes=None, hs_end=2.0, chords=None, phrases=None, arr_extra=None,
          json_tuning=None, stems=None, song_timeline=None, lyric_tracks=None,
          templates=None, tones=None, drum_tab=None, rigs=None, keys=None,
          harmony=None, lyrics=None):
    (root / "arrangements").mkdir(parents=True)
    (root / "stems").mkdir()
    (root / "stems" / "full.ogg").write_bytes(b"x")
    note = {"t": 1.0, "s": note_s, "f": 0}
    note.update(note_extra or {})
    arr = {"name": "Lead", "tuning": json_tuning if json_tuning is not None else [0]*6,
           "templates": templates if templates is not None else [{"name": "A", "frets": [], "fingers": []}],
           "notes": notes if notes is not None else [note],
           "handshapes": [{"start_time": 1.0, "end_time": hs_end, "chord_id": chord_id, "arp": False}],
           "chords": chords if chords is not None else [{"t": 1.0, "id": 0, "notes": []}]}
    if phrases is not None:
        arr["phrases"] = phrases
    if tones is not None:
        arr["tones"] = tones
    (root / "arrangements" / "lead.json").write_text(json.dumps(arr))
    arr_entry = {"id": "lead", "name": "Lead", "file": "arrangements/lead.json", "type": "guitar"}
    if arr_extra:
        arr_entry.update(arr_extra)
    m = {"feedpak_version": "1.11.0", "title": "T", "artist": "A", "duration": 10.0,
         "arrangements": [arr_entry],
         "stems": stems if stems is not None else [{"id": "full", "file": "stems/full.ogg", "default": True}]}
    if song_timeline is not None:
        (root / "song_timeline.json").write_text(json.dumps(song_timeline))
        m["song_timeline"] = "song_timeline.json"
    if lyric_tracks is not None:
        entries = []
        for lt in lyric_tracks:
            entry = {k: v for k, v in lt.items() if k != "content"}
            if "content" in lt:
                (root / entry["file"]).write_text(json.dumps(lt["content"]))
            entries.append(entry)
        m["lyric_tracks"] = entries
    for key, payload, fname in (("drum_tab", drum_tab, "drum_tab.json"),
                                 ("rigs", rigs, "rigs.json"),
                                 ("keys", keys, "keys.json"),
                                 ("harmony", harmony, "harmony.json")):
        if payload is not None:
            (root / fname).write_text(json.dumps(payload))
            m[key] = fname
    if lyrics is not None:
        (root / "lyrics.json").write_text(json.dumps(lyrics))
        m["lyrics"] = "lyrics.json"
    if manifest_extra:
        m.update(manifest_extra)
    (root / "manifest.yaml").write_text(yaml.safe_dump(m))


def _jsonc_pack(root: Path):
    """A hand-edited pack whose arrangement file is .jsonc (spec-legal, §6/§8)
    and actually contains a comment — must not crash strict (formerly did:
    the strict layer used a raw json.loads with no comment-stripping)."""
    (root / "arrangements").mkdir(parents=True)
    (root / "stems").mkdir()
    (root / "stems" / "full.ogg").write_bytes(b"x")
    arr_text = (
        "{\n"
        "  // a hand-edited comment\n"
        '  "name": "Lead", "tuning": [0,0,0,0,0,0], "templates": [],\n'
        '  "notes": [{"t": 1.0, "s": 0, "f": 0}], "handshapes": [], "chords": []\n'
        "}\n"
    )
    (root / "arrangements" / "lead.jsonc").write_text(arr_text)
    m = {"feedpak_version": "1.11.0", "title": "T", "artist": "A", "duration": 10.0,
         "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.jsonc", "type": "guitar"}],
         "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}]}
    (root / "manifest.yaml").write_text(yaml.safe_dump(m))



def test_strict_checks():
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

        # --- Phase 1 (Group A): holes in existing strict checks ---------------

        # chord notes carry the note field set minus `t` (§6.3) — including `s`,
        # which must be range-checked exactly like a standalone note's.
        chordbad = Path(t) / "chordbad.feedpak"
        _pack(chordbad, chords=[{"t": 1.0, "id": 0, "notes": [{"s": 99, "f": 0}]}])
        assert fp.check(chordbad, strict=False).ok, "loose spec: basic must PASS an out-of-range chord note"
        r = fp.check(chordbad, strict=True)
        assert not r.ok, "strict must FAIL an out-of-range chord note"
        assert "note.s=99" in "\n".join(r.errors), r.errors

        # phrases[].levels[] (§6.7) carry their own notes/chords/anchors/handshapes
        # — the schema doesn't shape-check `levels` at all, so this is basic-legal.
        phrasebad = Path(t) / "phrasebad.feedpak"
        _pack(phrasebad, phrases=[{
            "start_time": 0.0, "end_time": 1.0, "max_difficulty": 1,
            "levels": [{"difficulty": 0, "notes": [{"t": 0.5, "s": 99, "f": 0}],
                        "chords": [], "anchors": [], "handshapes": []}],
        }])
        assert fp.check(phrasebad, strict=False).ok, "loose spec: basic must PASS a bad note inside phrases[].levels[]"
        r = fp.check(phrasebad, strict=True)
        assert not r.ok, "strict must FAIL a bad note inside phrases[].levels[]"
        joined = "\n".join(r.errors)
        assert "phrases[0].levels[0]" in joined and "note.s=99" in joined, joined

        # §5.2: manifest-level tuning overrides the arrangement JSON's — a note
        # legal for a 6-string JSON tuning can be out of range once the manifest
        # narrows the arrangement to 4 strings.
        tuningover = Path(t) / "tuningover.feedpak"
        _pack(tuningover, note_s=5, json_tuning=[0] * 6, arr_extra={"tuning": [0, 0, 0, 0]})
        assert fp.check(tuningover, strict=False).ok, \
            "loose spec: basic doesn't cross-check note.s against tuning at all"
        r = fp.check(tuningover, strict=True)
        assert not r.ok, "strict must use the manifest's tuning override, not the arrangement JSON's"
        assert "out of range for 4-string tuning" in "\n".join(r.errors), r.errors

        # §5.3: `default` accepts case-insensitive true/false/on/off/yes/no, not
        # just a real boolean — two stems both "off" must NOT look like two defaults.
        stemsoff = Path(t) / "stemsoff.feedpak"
        _pack(stemsoff, stems=[{"id": "full", "file": "stems/full.ogg", "default": "off"},
                                {"id": "alt", "file": "stems/full.ogg", "default": "off"}])
        assert fp.check(stemsoff, strict=True).ok, "two stems default:\"off\" must not false-positive"

        stemson = Path(t) / "stemson.feedpak"
        _pack(stemson, stems=[{"id": "full", "file": "stems/full.ogg", "default": "on"},
                               {"id": "alt", "file": "stems/full.ogg", "default": "ON"}])
        r = fp.check(stemson, strict=True)
        assert not r.ok, "two stems default:\"on\"/\"ON\" must still trigger the more-than-one-default check"
        assert "more than one stem marked default" in "\n".join(r.errors), r.errors

        # song_timeline.json (§7.4): tempos/time_signatures/beats/sections are
        # each independently time-ordered; validate.py schema-checks the file's
        # shape but never sums or orders it, so descending beats is basic-legal.
        stbad = Path(t) / "stbad.feedpak"
        _pack(stbad, song_timeline={"version": 1, "beats": [
            {"time": 2.0, "measure": 1}, {"time": 1.0, "measure": 2}]})
        assert fp.check(stbad, strict=False).ok, "loose spec: basic must PASS descending song_timeline beats"
        r = fp.check(stbad, strict=True)
        assert not r.ok, "strict must FAIL descending song_timeline beats"
        assert "song_timeline.json" in "\n".join(r.errors) and "not in time order" in "\n".join(r.errors), r.errors

        # lyric_tracks[].file (§5.5): validate.py never opens these — only the
        # single `lyrics` pointer gets schema-validated. Strict must now open and
        # schema-check every track's content too.
        ltbad = Path(t) / "ltbad.feedpak"
        _pack(ltbad, lyric_tracks=[{"id": "en", "file": "lyrics_en.json", "language": "en",
                                     "kind": "original", "content": [{"t": "x"}]}])
        assert fp.check(ltbad, strict=False).ok, "loose spec: basic never opens lyric_tracks files"
        r = fp.check(ltbad, strict=True)
        assert not r.ok, "strict must schema-validate lyric_tracks file contents"
        assert "lyrics_en.json" in "\n".join(r.errors), r.errors

        # .jsonc arrangement (spec-legal, §6/§8): the strict layer used to read
        # arrangement files with a raw json.loads and crash on the comment.
        jsoncok = Path(t) / "jsoncok.feedpak"
        _jsonc_pack(jsoncok)
        assert fp.check(jsoncok, strict=False).ok, "a .jsonc arrangement must pass basic"
        assert fp.check(jsoncok, strict=True).ok, "a .jsonc arrangement must not crash strict"

        # --- Phase 2 (Group B): unchecked normative MUSTs ---------------------

        # §5.2: tuning length must be 4..8 strings.
        tun3 = Path(t) / "tun3.feedpak"
        _pack(tun3, json_tuning=[0, 0, 0])
        assert fp.check(tun3, strict=False).ok, "loose spec: the schema has no tuning length limit"
        r = fp.check(tun3, strict=True)
        assert not r.ok and "tuning has 3 strings" in "\n".join(r.errors), r.errors

        tun9 = Path(t) / "tun9.feedpak"
        _pack(tun9, json_tuning=[0] * 9)
        r = fp.check(tun9, strict=True)
        assert not r.ok and "tuning has 9 strings" in "\n".join(r.errors), r.errors

        # §6.6: template frets/fingers length must match the string count; empty
        # arrays (the default/absent case) must NOT trigger the check.
        tplbad = Path(t) / "tplbad.feedpak"
        _pack(tplbad, templates=[{"name": "A", "frets": [0, 0, 0], "fingers": [-1] * 6}])
        assert fp.check(tplbad, strict=False).ok
        r = fp.check(tplbad, strict=True)
        assert not r.ok and "templates[0].frets has 3 entries for a 6-string tuning" in "\n".join(r.errors), r.errors

        fingerbad = Path(t) / "fingerbad.feedpak"
        _pack(fingerbad, templates=[{"name": "A", "frets": [0] * 6, "fingers": [7, 0, 0, 0, 0, 0]}])
        assert fp.check(fingerbad, strict=False).ok
        r = fp.check(fingerbad, strict=True)
        assert not r.ok and "templates[0].fingers value 7 out of range (-1..4)" in "\n".join(r.errors), r.errors

        # §6.2.1: bnv curve points must be non-descending.
        bnvbad = Path(t) / "bnvbad.feedpak"
        _pack(bnvbad, notes=[{"t": 1.0, "s": 0, "f": 0, "bn": 1.0,
                               "bnv": [{"t": 0.5, "v": 1.0}, {"t": 0.1, "v": 0.0}]}])
        assert fp.check(bnvbad, strict=False).ok
        r = fp.check(bnvbad, strict=True)
        joined = "\n".join(r.errors)
        assert not r.ok and "bnv" in joined and "not in time order" in joined, joined

        # §6.9: tones.changes is time-sorted.
        tonesbad = Path(t) / "tonesbad.feedpak"
        _pack(tonesbad, tones={"base": "Clean", "changes": [{"t": 2.0, "name": "A"}, {"t": 1.0, "name": "B"}]})
        assert fp.check(tonesbad, strict=False).ok
        r = fp.check(tonesbad, strict=True)
        joined = "\n".join(r.errors)
        assert not r.ok and "tones.changes" in joined and "not in time order" in joined, joined

        # §7.7/§7.8: keys.json / harmony.json events are time-ordered.
        keysbad = Path(t) / "keysbad.feedpak"
        _pack(keysbad, keys={"version": 1, "events": [{"t": 2.0, "key": "Em"}, {"t": 1.0, "key": "G"}]})
        assert fp.check(keysbad, strict=False).ok
        r = fp.check(keysbad, strict=True)
        assert not r.ok and "not in time order" in "\n".join(r.errors), r.errors

        harmbad = Path(t) / "harmbad.feedpak"
        _pack(harmbad, harmony={"version": 1, "events": [{"t": 2.0, "root": "G"}, {"t": 1.0, "root": "C"}]})
        assert fp.check(harmbad, strict=False).ok
        r = fp.check(harmbad, strict=True)
        assert not r.ok and "not in time order" in "\n".join(r.errors), r.errors

        # §7.5: drum hits are monotonic.
        drumbad = Path(t) / "drumbad.feedpak"
        _pack(drumbad, drum_tab={"version": 1, "hits": [{"t": 2.0, "p": "kick"}, {"t": 1.0, "p": "snare"}]})
        assert fp.check(drumbad, strict=False).ok
        r = fp.check(drumbad, strict=True)
        joined = "\n".join(r.errors)
        assert not r.ok and "hits" in joined and "not in time order" in joined, joined

        # id uniqueness: rig id, drum kit piece id, lyric track id, notation stave id.
        rigdup = Path(t) / "rigdup.feedpak"
        _pack(rigdup, rigs={"version": 1, "rigs": [{"id": "a", "blocks": []}, {"id": "a", "blocks": []}]})
        assert fp.check(rigdup, strict=False).ok
        r = fp.check(rigdup, strict=True)
        assert not r.ok and "duplicate rig id: 'a'" in "\n".join(r.errors), r.errors

        kitdup = Path(t) / "kitdup.feedpak"
        _pack(kitdup, drum_tab={"version": 1, "kit": [{"id": "kick"}, {"id": "kick"}], "hits": []})
        assert fp.check(kitdup, strict=False).ok
        r = fp.check(kitdup, strict=True)
        assert not r.ok and "duplicate drum kit piece id: 'kick'" in "\n".join(r.errors), r.errors

        ltdup = Path(t) / "ltdup.feedpak"
        _pack(ltdup, lyric_tracks=[
            {"id": "en", "file": "lyrics_en1.json", "language": "en", "kind": "original", "content": []},
            {"id": "en", "file": "lyrics_en2.json", "language": "en", "kind": "translation", "content": []},
        ])
        assert fp.check(ltdup, strict=False).ok
        r = fp.check(ltdup, strict=True)
        assert not r.ok and "duplicate lyric track id: 'en'" in "\n".join(r.errors), r.errors

        stavedup_measures = [{"idx": 1, "t": 0.0, "ts": [4, 4],
                               "staves": {"rh": {"voices": [{"v": 1, "beats": _beats(1)}]}}}]
        stavedup = Path(t) / "stavedup.feedpak"
        _notation_pack(stavedup, stavedup_measures, staves=[{"id": "rh", "clef": "G2"}, {"id": "rh", "clef": "F4"}])
        assert fp.check(stavedup, strict=False).ok
        r = fp.check(stavedup, strict=True)
        assert not r.ok and "duplicate notation stave id: 'rh'" in "\n".join(r.errors), r.errors

        # dangling references: lyric_tracks[].stem, tones rig ids, notation stave
        # keys, rigs.json graph nodes.
        stemdangle = Path(t) / "stemdangle.feedpak"
        _pack(stemdangle, lyric_tracks=[{"id": "en", "file": "lyrics_en.json", "language": "en",
                                          "kind": "original", "stem": "nope", "content": []}])
        assert fp.check(stemdangle, strict=False).ok
        r = fp.check(stemdangle, strict=True)
        assert not r.ok and "does not match any stems[].id" in "\n".join(r.errors), r.errors

        norigs = Path(t) / "norigs.feedpak"
        _pack(norigs, tones={"base": "Clean", "base_rig": "clean-rhythm"})
        assert fp.check(norigs, strict=False).ok
        r = fp.check(norigs, strict=True)
        assert not r.ok and "tones reference rig ids but the manifest has no rigs file" in "\n".join(r.errors), r.errors

        ridbad = Path(t) / "ridbad.feedpak"
        _pack(ridbad, tones={"base": "Clean", "base_rig": "nope"},
              rigs={"version": 1, "rigs": [{"id": "clean-rhythm", "blocks": []}]})
        assert fp.check(ridbad, strict=False).ok
        r = fp.check(ridbad, strict=True)
        assert not r.ok and "tones rig 'nope' not found in rigs.json" in "\n".join(r.errors), r.errors

        undeclared_measures = [{"idx": 1, "t": 0.0, "ts": [4, 4],
                                 "staves": {"ghost": {"voices": [{"v": 1, "beats": _beats(1)}]}}}]
        undeclared = Path(t) / "undeclared.feedpak"
        _notation_pack(undeclared, undeclared_measures)
        assert fp.check(undeclared, strict=False).ok
        r = fp.check(undeclared, strict=True)
        assert not r.ok and "references undeclared stave 'ghost'" in "\n".join(r.errors), r.errors

        graphbad = Path(t) / "graphbad.feedpak"
        _pack(graphbad, rigs={"version": 1, "rigs": [{"id": "r1", "blocks": [{"id": "amp"}],
              "graph": {"nodes": ["input", "amp", "output"], "edges": [["input", "ghost"]]}}]})
        assert fp.check(graphbad, strict=False).ok
        r = fp.check(graphbad, strict=True)
        assert not r.ok and "graph edge references unknown node 'ghost'" in "\n".join(r.errors), r.errors

        graphnodebad = Path(t) / "graphnodebad.feedpak"
        _pack(graphnodebad, rigs={"version": 1, "rigs": [{"id": "r1", "blocks": [{"id": "amp"}],
              "graph": {"nodes": ["input", "amp", "ghost", "output"],
                        "edges": [["input", "amp"], ["amp", "output"]]}}]})
        assert fp.check(graphnodebad, strict=False).ok
        r = fp.check(graphnodebad, strict=True)
        assert not r.ok and "graph node 'ghost' does not match any block id" in "\n".join(r.errors), r.errors

        # §7.6: beat_groups must sum to the time signature numerator.
        bgbad_measures = [{"idx": 1, "t": 0.0, "ts": [4, 4], "beat_groups": [3, 3],
                            "staves": {"rh": {"voices": [{"v": 1, "beats": _beats(1)}]}}}]
        bgbad = Path(t) / "bgbad.feedpak"
        _notation_pack(bgbad, bgbad_measures)
        assert fp.check(bgbad, strict=False).ok
        r = fp.check(bgbad, strict=True)
        assert not r.ok and "beat_groups sum to 6 but the time signature numerator is 4" in "\n".join(r.errors), r.errors

        # §7.9: a nam/ir realization ref must be a safe relative path. (File
        # existence is deliberately NOT checked — see PR notes: the vendored
        # extended.feedpak example ships rigs.json referencing capture/IR assets
        # it doesn't include, so "missing" would false-positive the canary pack.)
        refunsafe = Path(t) / "refunsafe.feedpak"
        _pack(refunsafe, rigs={"version": 1, "rigs": [{"id": "r1", "blocks": [
              {"id": "amp", "realizations": [{"engine": "nam", "ref": "../x.nam"}]}]}]})
        assert fp.check(refunsafe, strict=False).ok
        r = fp.check(refunsafe, strict=True)
        assert not r.ok and "realization ref is not a safe relative path" in "\n".join(r.errors), r.errors

        # §7.1: a bare '-'/'+' is a join/line marker with no syllable text.
        lyricsuffix = Path(t) / "lyricsuffix.feedpak"
        _pack(lyricsuffix, lyrics=[{"t": 1.0, "d": 0.1, "w": "-"}])
        assert fp.check(lyricsuffix, strict=False).ok
        r = fp.check(lyricsuffix, strict=True)
        assert not r.ok and "lyrics[0].w is a bare '-'" in "\n".join(r.errors), r.errors

        # --- Phase 3 (Group C): SHOULD-level warnings --------------------------
        # Warnings never fail rep.ok — only errors do.

        # §5.3.2: a distributable pack needs at least one OGG/WAV baseline stem;
        # an explicit codec override (here "mp3") wins over the .ogg extension.
        mp3only = Path(t) / "mp3only.feedpak"
        _pack(mp3only, stems=[{"id": "full", "file": "stems/full.ogg", "codec": "mp3", "default": True}])
        r = fp.check(mp3only, strict=True)
        assert r.ok, "a warning must not fail rep.ok"
        assert "no baseline OGG/WAV stem" in "\n".join(r.warnings), r.warnings

        # §6.2.1 (SHOULD NOT): a bend-shape hint (bnv) with no actual bend (bn=0).
        bendhint = Path(t) / "bendhint.feedpak"
        _pack(bendhint, notes=[{"t": 1.0, "s": 0, "f": 0, "bnv": [{"t": 0.0, "v": 0.0}]}])
        r = fp.check(bendhint, strict=True)
        assert r.ok
        joined = "\n".join(r.warnings)
        assert "carries bend shape" in joined and "bn is 0" in joined, joined

        # §5.5: a non-standard lyric_tracks kind, and a `lyrics` pointer that
        # doesn't name any kind:original track's file.
        kindbad = Path(t) / "kindbad.feedpak"
        _pack(kindbad, lyric_tracks=[{"id": "en", "file": "lyrics_en.json", "language": "en",
                                       "kind": "dub", "content": []}])
        r = fp.check(kindbad, strict=True)
        assert r.ok
        joined = "\n".join(r.warnings)
        assert "lyric_tracks[0].kind 'dub' is non-standard" in joined, joined
        assert "lyrics pointer does not name a kind:original track's file" in joined, joined

        res = fp.validate(kindbad, strict=True)
        assert len(res["warning_explanations"]) == len(res["warnings"]) == 2, res
        assert all(e != fp._EXPLAIN_FALLBACK for e in res["warning_explanations"]), res

        # §7.5: an unrecognized drum piece id (closed v1 vocabulary — warn, don't
        # reject, since unknown ids MUST still round-trip).
        drumvocab = Path(t) / "drumvocab.feedpak"
        _pack(drumvocab, drum_tab={"version": 1, "hits": [{"t": 1.0, "p": "cowbell"}]})
        r = fp.check(drumvocab, strict=True)
        assert r.ok
        assert "drum piece id 'cowbell' is outside the v1 vocabulary" in "\n".join(r.warnings), r.warnings

        # §6.2: 24 is the max playable fret.
        fretceil = Path(t) / "fretceil.feedpak"
        _pack(fretceil, notes=[{"t": 1.0, "s": 0, "f": 25}])
        r = fp.check(fretceil, strict=True)
        assert r.ok
        assert "notes[0].f=25 exceeds fret 24" in "\n".join(r.warnings), r.warnings


def test_explanations():
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
        # Phase 2 (Group B)
        "arrangements/lead.json: tuning has 3 strings — the spec accepts 4 to 8",
        "arrangements/lead.json: templates[0].frets has 3 entries for a 6-string tuning",
        "arrangements/lead.json: templates[0].fingers value 7 out of range (-1..4)",
        "lyric_tracks[0].stem 'nope' does not match any stems[].id",
        "arrangements/lead.json: tones reference rig ids but the manifest has no rigs file",
        "arrangements/lead.json: tones rig 'nope' not found in rigs.json",
        "notation_keys.json: measure 1 references undeclared stave 'ghost'",
        "rigs.json: rigs[0] graph edge references unknown node 'ghost'",
        "rigs.json: rigs[0] graph node 'ghost' does not match any block id",
        "notation_keys.json: measure 1: beat_groups sum to 6 but the time signature numerator is 4",
        "rigs.json: rigs[0] realization ref is not a safe relative path: '../x.nam'",
        "lyrics.json: lyrics[0].w is a bare '-' — join/line markers are suffixes on a syllable, not standalone entries",
        # Phase 3 (Group C)
        "no baseline OGG/WAV stem — pack is not portable (spec §5.3.2)",
        "arrangements/lead.json: notes[0] carries bend shape (bt/bnv) but bn is 0",
        "lyric_tracks[0].kind 'dub' is non-standard — readers will treat it as a translation",
        "lyrics pointer does not name a kind:original track's file — pre-1.11 readers may show nothing",
        "drum_tab.json: drum piece id 'cowbell' is outside the v1 vocabulary",
        "arrangements/lead.json: notes[0].f=25 exceeds fret 24",
    ]
    explanations = [fp._explain(c) for c in _EXPLAIN_CASES]
    assert all(e != fp._EXPLAIN_FALLBACK for e in explanations), \
        [c for c, e in zip(_EXPLAIN_CASES, explanations) if e == fp._EXPLAIN_FALLBACK]
    assert len(set(explanations)) == len(_EXPLAIN_CASES), \
        "two trigger cases produced the same explanation — one rule is unreachable"
    assert fp._explain("some future check nobody wrote a rule for yet") == fp._EXPLAIN_FALLBACK


if __name__ == "__main__":
    test_spec_info()
    test_strict_checks()
    test_explanations()
    print("ok — strict catches unknown keys, bad ranges, dangling refs, out-of-order times, "
          "notation measure overflow, per-error explanations, and all of the above in dirs and zips")
