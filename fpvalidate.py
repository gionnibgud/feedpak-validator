#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""feedpak validator — two levels.

  basic   spec conformance: exactly what the reference validate.py checks
          (JSON Schema + file existence + path safety). Loose by design:
          the schemas set additionalProperties:true and require few fields.

  strict  everything basic checks, PLUS the invariants the spec's own schemas
          can't express (they use additionalProperties:true and few required):
            - unknown keys rejected (manifest + arrangements, prose-aware)
            - arrangement/stem id uniqueness
            - at most one default stem
            - note.string index within the tuning
            - handshape.chord_id / chord.id index within templates
            - note/chord/beat/section/anchor/tempo times non-decreasing
            - handshape/phrase spans have positive length
            - lyric_tracks side-files exist (validate.py never opens them)
            - notation_<id>.json measures don't overflow their time signature
              (voice beat-durations, incl. dot/tuplet, summed against ts)

PACK is a *.feedpak/ directory or a *.feedpak zip archive; both levels handle both.

Usage:
    python fpvalidate.py PACK [PACK ...]            # basic
    python fpvalidate.py --strict PACK [PACK ...]

Reuses the vendored reference validator for the basic pass instead of
reimplementing schema loading, zip-slip guards, and path safety.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

# basic level = the official reference validator, vendored (pinned v1.14.0).
# See vendor/feedpak-spec/VENDOR.txt for the exact source commit.
SPEC = Path(__file__).resolve().parent / "vendor" / "feedpak-spec"


def _load_ref():
    """Import the vendored reference validator by file path under a private name,
    so embedding this module in a host app (e.g. a fee[dB]ack plugin) can't clash
    with a generic `validate` module and we never mutate the caller's sys.path."""
    spec = importlib.util.spec_from_file_location(
        "_feedpak_ref_validate", SPEC / "tools" / "validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def spec_info() -> dict:
    """Parse vendor/feedpak-spec/VENDOR.txt into {repo, tag, commit} — what
    basic (and the closed-world patches strict layers on top of) is actually
    checking against. For display (e.g. the plugin UI), not validation.
    Best-effort: a missing or reformatted VENDOR.txt yields None fields rather
    than raising, since this is metadata, not a correctness path."""
    info = {"repo": None, "tag": None, "commit": None}
    try:
        text = (SPEC / "VENDOR.txt").read_text(encoding="utf-8")
    except OSError:
        return info
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Vendored from "):
            info["repo"] = line[len("Vendored from "):].strip()
        elif line.startswith("tag:"):
            info["tag"] = line.split(":", 1)[1].strip()
        elif line.startswith("commit:"):
            info["commit"] = line.split(":", 1)[1].strip()
    return info


ref = _load_ref()  # the reference validator (basic level)

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    sys.exit("error: needs pyyaml + jsonschema (pip install pyyaml jsonschema)")


# ---- strict layer -----------------------------------------------------------

def _no_extra_keys(schema: dict) -> dict:
    """Clone a schema with additionalProperties:false on every object node.
    Turns the spec's forward-compatible looseness into a closed-world check
    without hand-listing field names — the schema stays the source of truth."""
    s = copy.deepcopy(schema)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "properties" in node:            # strict: forbid extras even where the spec allowed them
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


# Note fields the spec prose (§6.2) defines but the arrangement schema's `note`
# def omits — every one documented in vendor/feedpak-spec/spec/feedpak-v1.md §6.2.
# The schema stays additionalProperties:true, so basic never flags a typo'd field;
# strict closes the world but must first admit these real fields or it false-
# positives on valid packs. review on spec pin bump (see vendor/.../VENDOR.txt).
NOTE_EXTRA = {"ho", "po", "hm", "hp", "pm", "mt", "vb", "tr", "ac", "tp", "ln",
              "fhm", "plk", "slp", "rh", "pkd", "ig"}


def _closed_arrangement_validator() -> Draft202012Validator:
    """Arrangement schema patched to match the prose, then closed-world.

    The schema's `note` def lists 12 of the 29 fields in §6.2, and `chordNote`
    lists only f/s/sus though §6.3 says chord notes carry the full note field set
    minus `t`. Patch both before forbidding extras so genuinely-unknown keys error
    while every spec field passes."""
    s = _load_raw("arrangement.schema.json")
    note = s["$defs"]["note"]["properties"]
    for k in NOTE_EXTRA:
        note.setdefault(k, {})
    # chord notes = note fields minus `t` (§6.3).
    s["$defs"]["chordNote"]["properties"] = {k: v for k, v in note.items() if k != "t"}
    return Draft202012Validator(_no_extra_keys(s))


def _strict_schema_errors(manifest: dict, root: Path, rep: ref.Report) -> None:
    """Re-run schema validation with unknown keys forbidden, for the manifest
    and every arrangement file (prose-patched — see _closed_arrangement_validator)."""
    mv = Draft202012Validator(_no_extra_keys(_load_raw("manifest.schema.json")))
    for e in mv.iter_errors(manifest):
        rep.err(f"manifest.yaml [strict]: {_loc(e)}: {e.message}")

    av = _closed_arrangement_validator()
    for i, arr in enumerate(manifest.get("arrangements", []) or []):
        if not isinstance(arr, dict):
            continue
        f = arr.get("file")
        if f and (root / f).is_file():
            data = _read_json(root, f)
            if data is None:
                continue  # malformed JSON — basic already reports it
            for e in av.iter_errors(data):
                rep.err(f"{f} [strict]: {_loc(e)}: {e.message}")


def _stem_default(v: object) -> bool:
    """§5.3: readers MUST also accept the case-insensitive strings
    true/false, on/off, yes/no for `default`, not just a real boolean."""
    return v is True or (isinstance(v, str) and v.lower() in ("true", "on", "yes"))


# §5.3.2: extension -> resolved codec, when `codec` isn't given explicitly.
_EXT_CODEC = {"ogg": "vorbis", "opus": "opus", "wav": "pcm", "mp3": "mp3", "flac": "flac"}
_BASELINE_CODECS = {"vorbis", "pcm"}


def _stem_codec(s: dict) -> str | None:
    """§5.3.2 codec resolution: explicit `codec` (lowercased) overrides the
    file extension; an unrecognized/missing extension resolves to None."""
    codec = s.get("codec")
    if isinstance(codec, str) and codec:
        return codec.lower()
    f = s.get("file")
    if isinstance(f, str) and "." in f:
        return _EXT_CODEC.get(f.rsplit(".", 1)[-1].lower())
    return None


# §7.5: closed piece-id vocabulary for v1; unknown ids MUST still round-trip
# (a Reader renders them with a fallback), so this is a warning, not an error.
_DRUM_VOCAB = {
    "kick", "snare", "snare_xstick", "tom_hi", "tom_mid", "tom_low", "tom_floor",
    "hh_closed", "hh_open", "hh_pedal", "stack", "crash_l", "crash_r", "splash",
    "china", "ride", "ride_bell", "bell",
}


def _check_lyrics_suffix(rep: ref.Report, rel: str, data: list) -> None:
    """§7.1: a trailing '-' or '+' is a suffix on a real syllable, not a
    standalone entry — `w` being exactly '-' or '+' means the syllable text
    itself is missing."""
    if not isinstance(data, list):
        return
    for i, e in enumerate(data):
        if not isinstance(e, dict):
            continue
        w = e.get("w")
        if w in ("-", "+"):
            rep.err(f"{rel}: lyrics[{i}].w is a bare {w!r} — join/line markers "
                     "are suffixes on a syllable, not standalone entries")


def _strict_semantics(manifest: dict, root: Path, rep: ref.Report) -> None:
    arrs = manifest.get("arrangements", []) or []
    stems = manifest.get("stems", []) or []

    _dupes(rep, "arrangement id", [a.get("id") for a in arrs if isinstance(a, dict)])
    _dupes(rep, "stem id", [s.get("id") for s in stems if isinstance(s, dict)])
    _dupes(rep, "lyric track id",
           [t.get("id") for t in manifest.get("lyric_tracks", []) or [] if isinstance(t, dict)])

    if sum(1 for s in stems if isinstance(s, dict) and _stem_default(s.get("default"))) > 1:
        rep.err("more than one stem marked default:true")

    # §5.3.2 (SHOULD): a distributable pack needs at least one OGG/WAV
    # (resolved codec vorbis/pcm) baseline stem so a leaner Reader has
    # something guaranteed-playable.
    if stems and not any(_stem_codec(s) in _BASELINE_CODECS for s in stems if isinstance(s, dict)):
        rep.warn("no baseline OGG/WAV stem — pack is not portable (spec §5.3.2)")

    # lyric_tracks side-files: validate.py never opens these (PLAN.MD:107).
    # Also schema-validate their contents against the plain (not closed-world)
    # lyrics schema — basic only ever validates the single `lyrics` pointer —
    # and apply the §7.1 standalone-suffix rule (2.8) to each track.
    stem_ids = {s.get("id") for s in stems if isinstance(s, dict) and isinstance(s.get("id"), str)}
    lyrics_validator = Draft202012Validator(_load_raw("lyrics.schema.json"))
    original_files: set = set()
    lyric_tracks_list = manifest.get("lyric_tracks", []) or []
    for i, t in enumerate(lyric_tracks_list):
        if not isinstance(t, dict):
            continue
        f = t.get("file")
        stem_ref = t.get("stem")
        if isinstance(stem_ref, str) and stem_ref not in stem_ids:
            rep.err(f"lyric_tracks[{i}].stem {stem_ref!r} does not match any stems[].id")

        # §5.5 (SHOULD): kind is one of the three canonical values — a Reader
        # MUST accept any value but treats an unrecognized one as translation.
        kind = t.get("kind")
        if isinstance(kind, str) and kind not in ("original", "transliteration", "translation"):
            rep.warn(f"lyric_tracks[{i}].kind {kind!r} is non-standard — "
                      "readers will treat it as a translation")
        if kind == "original" and isinstance(f, str):
            original_files.add(f)

        if not f:
            continue
        if not (root / f).is_file():
            rep.err(f"lyric_tracks[{i}].file missing: {f}")
            continue
        data = _read_json(root, f)
        if data is None:
            continue  # malformed JSON — not this check's job
        for e in lyrics_validator.iter_errors(data):
            rep.err(f"{f} [strict]: {_loc(e)}: {e.message}")
        _check_lyrics_suffix(rep, f, data)

    # the legacy single `lyrics` pointer (§7.1) gets the same suffix rule —
    # basic schema-validates it but never inspects `w`'s content.
    lyrics_rel = manifest.get("lyrics")
    if isinstance(lyrics_rel, str) and (root / lyrics_rel).is_file():
        ldata = _read_json(root, lyrics_rel)
        if ldata is not None:
            _check_lyrics_suffix(rep, lyrics_rel, ldata)

    # §5.5 (SHOULD): when lyric_tracks is present, `lyrics` SHOULD point at
    # the kind:original track's file, or a pre-1.11 Reader may show nothing.
    if lyric_tracks_list and (not isinstance(lyrics_rel, str) or lyrics_rel not in original_files):
        rep.warn("lyrics pointer does not name a kind:original track's file — "
                  "pre-1.11 readers may show nothing")

    # --- side-files loaded once, reused across ordering / uniqueness /
    # dangling-ref checks. Missing pointer, missing file, or a parse failure
    # (already a basic-level concern) all mean "skip", never raise.
    keys_rel = manifest.get("keys")
    if isinstance(keys_rel, str) and (root / keys_rel).is_file():
        kdata = _read_json(root, keys_rel)
        if isinstance(kdata, dict):
            _monotonic(rep, keys_rel, "events", kdata.get("events", []) or [], "t")

    harmony_rel = manifest.get("harmony")
    if isinstance(harmony_rel, str) and (root / harmony_rel).is_file():
        hdata = _read_json(root, harmony_rel)
        if isinstance(hdata, dict):
            _monotonic(rep, harmony_rel, "events", hdata.get("events", []) or [], "t")

    drum_rel = manifest.get("drum_tab")
    drum_data = None
    if isinstance(drum_rel, str) and (root / drum_rel).is_file():
        drum_data = _read_json(root, drum_rel)
        if isinstance(drum_data, dict):
            _monotonic(rep, drum_rel, "hits", drum_data.get("hits", []) or [], "t")
            _dupes(rep, "drum kit piece id",
                   [k.get("id") for k in drum_data.get("kit", []) or [] if isinstance(k, dict)])
            # §7.5 (SHOULD): unknown piece-ids MUST still round-trip, so this
            # is a warning — dedupe so a repeated id warns once.
            seen_pieces: set = set()
            for h in drum_data.get("hits", []) or []:
                if not isinstance(h, dict):
                    continue
                p = h.get("p")
                if isinstance(p, str) and p not in _DRUM_VOCAB and p not in seen_pieces:
                    seen_pieces.add(p)
                    rep.warn(f"{drum_rel}: drum piece id {p!r} is outside the v1 vocabulary")
        else:
            drum_data = None

    rigs_rel = manifest.get("rigs")
    rig_ids: set = set()
    rigs_loaded = False
    if isinstance(rigs_rel, str) and (root / rigs_rel).is_file():
        rdata = _read_json(root, rigs_rel)
        if isinstance(rdata, dict):
            rigs_loaded = True
            rigs_list = rdata.get("rigs", []) or []
            _dupes(rep, "rig id", [rg.get("id") for rg in rigs_list if isinstance(rg, dict)])
            for i, rg in enumerate(rigs_list):
                if not isinstance(rg, dict):
                    continue
                if isinstance(rg.get("id"), str):
                    rig_ids.add(rg["id"])
                blocks = rg.get("blocks", []) or []
                block_ids = {b.get("id") for b in blocks
                             if isinstance(b, dict) and isinstance(b.get("id"), str)}
                for j, blk in enumerate(blocks):
                    if not isinstance(blk, dict):
                        continue
                    for k, lane in enumerate(blk.get("automation", []) or []):
                        if not isinstance(lane, dict):
                            continue
                        _monotonic(rep, rigs_rel, f"rigs[{i}].blocks[{j}].automation[{k}].points",
                                   lane.get("points", []) or [], "t")
                    for k, real in enumerate(blk.get("realizations", []) or []):
                        if not isinstance(real, dict):
                            continue
                        eng, rr = real.get("engine"), real.get("ref")
                        if eng in ("nam", "ir") and isinstance(rr, str) and "://" not in rr:
                            if not ref.safe_relpath(rr):
                                rep.err(f"{rigs_rel}: rigs[{i}] realization ref is not a "
                                         f"safe relative path: {rr!r}")
                            # NOTE: existence of the referenced asset (a capture/IR
                            # binary) is deliberately NOT checked — see PR notes:
                            # the vendored extended.feedpak example ships rigs.json
                            # referencing nam/ir assets it doesn't include.

                # §7.9 graph integrity: every edge endpoint must be a declared
                # node, and every node other than input/output must be a block id.
                graph = rg.get("graph")
                if isinstance(graph, dict):
                    nodes = {n for n in (graph.get("nodes") or []) if isinstance(n, str)}
                    bad_edge_nodes: set = set()
                    for edge in graph.get("edges", []) or []:
                        if not (isinstance(edge, list) and len(edge) == 2):
                            continue
                        for n in edge:
                            if isinstance(n, str) and n not in nodes:
                                bad_edge_nodes.add(n)
                    for n in sorted(bad_edge_nodes):
                        rep.err(f"{rigs_rel}: rigs[{i}] graph edge references unknown node {n!r}")
                    for n in sorted(nodes - block_ids - {"input", "output"}):
                        rep.err(f"{rigs_rel}: rigs[{i}] graph node {n!r} does not match any block id")

    for idx, a in enumerate(arrs):
        if not isinstance(a, dict):
            continue
        f = a.get("file")
        if not f or not (root / f).is_file():
            continue
        d = _read_json(root, f)
        if not isinstance(d, dict):
            continue

        # §5.2: manifest-level tuning overrides the arrangement JSON's.
        mtuning = a.get("tuning")
        jtuning = d.get("tuning")
        mtuning_supplied = isinstance(mtuning, list) and bool(mtuning)
        tuning = mtuning if mtuning_supplied else (jtuning if isinstance(jtuning, list) else [])
        nstr = len(tuning) or 6
        ntpl = len(d.get("templates", []) or [])   # chord_id / chord.id index into templates

        # §5.2: accepted tuning lengths are 4–8 strings.
        if tuning and not (4 <= len(tuning) <= 8):
            loc = f"manifest.yaml: arrangements[{idx}]" if mtuning_supplied else f
            rep.err(f"{loc}: tuning has {len(tuning)} strings — the spec accepts 4 to 8")

        _check_chart_arrays(rep, f, d, nstr, ntpl)
        for i, ph in enumerate(d.get("phrases", []) or []):
            if not isinstance(ph, dict):
                continue
            for j, lvl in enumerate(ph.get("levels", []) or []):
                if not isinstance(lvl, dict):
                    continue
                _check_chart_arrays(rep, f, lvl, nstr, ntpl, where=f"phrases[{i}].levels[{j}]: ")

        # §6.6: template shape arrays match the string count; fingers -1..4.
        for ti, tpl in enumerate(d.get("templates", []) or []):
            if not isinstance(tpl, dict):
                continue
            for key in ("frets", "fingers"):
                v = tpl.get(key)
                if isinstance(v, list) and v and len(v) != nstr:
                    rep.err(f"{f}: templates[{ti}].{key} has {len(v)} entries "
                             f"for a {nstr}-string tuning")
            for x in tpl.get("fingers") or []:
                if isinstance(x, int) and not (-1 <= x <= 4):
                    rep.err(f"{f}: templates[{ti}].fingers value {x} out of range (-1..4)")

        # §6.2.1: bnv curve points are non-descending, for notes and chord notes.
        for i, n in enumerate(d.get("notes", []) or []):
            if not isinstance(n, dict):
                continue
            bnv = n.get("bnv")
            if isinstance(bnv, list) and bnv:
                _monotonic(rep, f, f"notes[{i}].bnv", bnv, "t")
        for ci, c in enumerate(d.get("chords", []) or []):
            if not isinstance(c, dict):
                continue
            for ni, cn in enumerate(c.get("notes", []) or []):
                if not isinstance(cn, dict):
                    continue
                bnv = cn.get("bnv")
                if isinstance(bnv, list) and bnv:
                    _monotonic(rep, f, f"chords[{ci}].notes[{ni}].bnv", bnv, "t")

        # §6.2.1 (SHOULD NOT): bend-shape hints (bt/bnv) with no actual bend
        # (bn absent/0) — first occurrence only, one warning per array.
        # §6.2 (24 = max fret): first occurrence only, one warning per array.
        bend_warned = fret_warned = False
        for i, n in enumerate(d.get("notes", []) or []):
            if not isinstance(n, dict):
                continue
            if not bend_warned:
                bnv, bt = n.get("bnv"), n.get("bt")
                has_shape = (isinstance(bnv, list) and bnv) or (isinstance(bt, int) and bt != 0)
                if has_shape and not n.get("bn"):
                    rep.warn(f"{f}: notes[{i}] carries bend shape (bt/bnv) but bn is 0")
                    bend_warned = True
            if not fret_warned:
                fr = n.get("f")
                if isinstance(fr, int) and fr > 24:
                    rep.warn(f"{f}: notes[{i}].f={fr} exceeds fret 24")
                    fret_warned = True
            if bend_warned and fret_warned:
                break

        # §6.9: tones.changes is time-sorted; base_rig/changes[].rig must
        # resolve against rigs.json.
        tones = d.get("tones")
        if isinstance(tones, dict):
            _monotonic(rep, f, "tones.changes", tones.get("changes", []) or [], "t")
            referenced: set = set()
            br = tones.get("base_rig")
            if isinstance(br, str):
                referenced.add(br)
            for ch in tones.get("changes", []) or []:
                if isinstance(ch, dict) and isinstance(ch.get("rig"), str):
                    referenced.add(ch["rig"])
            if referenced:
                if not isinstance(rigs_rel, str):
                    rep.err(f"{f}: tones reference rig ids but the manifest has no rigs file")
                elif rigs_loaded:
                    for rid in sorted(referenced - rig_ids):
                        rep.err(f"{f}: tones rig {rid!r} not found in rigs.json")

        # song-level-ish arrays that only ever live at chart top level, plus
        # the phrases[] span check — levels don't carry phrases/beats/etc.
        _monotonic(rep, f, "beats", d.get("beats", []) or [], "time")
        _monotonic(rep, f, "sections", d.get("sections", []) or [], "time")
        _monotonic(rep, f, "tempos", d.get("tempos", []) or [], "time")
        for j, s in enumerate(d.get("phrases", []) or []):
            if not isinstance(s, dict):
                continue
            a2, b2 = s.get("start_time"), s.get("end_time")
            if isinstance(a2, (int, float)) and isinstance(b2, (int, float)) and b2 <= a2:
                rep.err(f"{f}: phrases[{j}] end_time {b2} <= start_time {a2}")
                break

    # notation_<id>.json (§7.6): a per-arrangement side-file, not the `file`
    # loop above — a notation-only arrangement MAY omit `file` entirely, so
    # this runs independent of the `if not f` skip.
    for a in arrs:
        if not isinstance(a, dict):
            continue
        nf = a.get("notation")
        if not nf or not (root / nf).is_file():
            continue
        ndata = _read_json(root, nf)
        if not isinstance(ndata, dict):
            continue  # malformed JSON is already a basic-level schema failure
        _dupes(rep, "notation stave id",
               [s.get("id") for s in ndata.get("staves", []) or [] if isinstance(s, dict)])
        _check_notation_measures(rep, nf, ndata)

    # song_timeline.json (§7.4): tempos/time_signatures/beats/sections are
    # each independently time-ordered.
    stl = manifest.get("song_timeline")
    if isinstance(stl, str) and (root / stl).is_file():
        st = _read_json(root, stl)
        if isinstance(st, dict):
            _monotonic(rep, stl, "tempos", st.get("tempos", []) or [], "time")
            _monotonic(rep, stl, "time_signatures", st.get("time_signatures", []) or [], "time")
            _monotonic(rep, stl, "beats", st.get("beats", []) or [], "time")
            _monotonic(rep, stl, "sections", st.get("sections", []) or [], "time")


def _check_chart_arrays(rep: ref.Report, f: str, d: dict, nstr: int, ntpl: int, where: str = "") -> None:
    """Range/order/span checks shared by an arrangement's top level and each
    phrase level. `where` prefixes locations (e.g. "phrases[2].levels[0]: ")."""
    for n in d.get("notes", []) or []:
        if not isinstance(n, dict):
            continue
        s = n.get("s")
        if isinstance(s, int) and not (0 <= s < nstr):
            rep.err(f"{f}: {where}note.s={s} out of range for {nstr}-string tuning")
    for c in d.get("chords", []) or []:
        if not isinstance(c, dict):
            continue
        for cn in c.get("notes", []) or []:
            if not isinstance(cn, dict):
                continue
            s = cn.get("s")
            if isinstance(s, int) and not (0 <= s < nstr):
                rep.err(f"{f}: {where}note.s={s} out of range for {nstr}-string tuning")
    for h in d.get("handshapes", []) or []:
        if not isinstance(h, dict):
            continue
        cid = h.get("chord_id")
        if isinstance(cid, int) and not (0 <= cid < ntpl):
            rep.err(f"{f}: {where}handshape.chord_id={cid} out of range (templates=[0,{ntpl}))")
    for c in d.get("chords", []) or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if isinstance(cid, int) and not (0 <= cid < ntpl):
            rep.err(f"{f}: {where}chord.id={cid} out of range (templates=[0,{ntpl}))")

    # time ordering: these arrays are authored in time order; flag the first
    # strictly-decreasing step. Shared timestamps are legal (many objects at
    # the same t), so compare with '<', not '<='.
    _monotonic(rep, f, f"{where}notes", d.get("notes", []) or [], "t")
    _monotonic(rep, f, f"{where}chords", d.get("chords", []) or [], "t")
    _monotonic(rep, f, f"{where}anchors", d.get("anchors", []) or [], "time")

    # spans must have positive length.
    for j, s in enumerate(d.get("handshapes", []) or []):
        if not isinstance(s, dict):
            continue
        a, b = s.get("start_time"), s.get("end_time")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b <= a:
            rep.err(f"{f}: {where}handshapes[{j}] end_time {b} <= start_time {a}")
            break


def _beat_whole_notes(b: dict) -> float:
    """A beat's duration in whole notes: 1/dur, scaled by `dot` (1->1.5x,
    2->1.75x per standard dotted-note arithmetic) and `tu` ([num, den]
    tuplet — actual duration = written * den/num, e.g. a 3-in-the-time-of-2
    triplet plays at 2/3 written length). Malformed fields contribute 0
    rather than raising — schema validation already flags the field itself."""
    dur = b.get("dur")
    if not isinstance(dur, (int, float)) or dur <= 0:
        return 0.0
    val = 1.0 / dur
    dot = b.get("dot")
    if dot == 1:
        val *= 1.5
    elif dot == 2:
        val *= 1.75
    tu = b.get("tu")
    if (isinstance(tu, list) and len(tu) == 2
            and all(isinstance(x, (int, float)) and x for x in tu)):
        val *= tu[1] / tu[0]
    return val


def _check_notation_measures(rep: ref.Report, f: str, data: dict) -> None:
    """Flag any (measure, stave, voice) whose beats sum to more whole-note
    duration than its time signature can hold. `ts` is 'omit if unchanged'
    (§7.6) so it carries forward across measures. Doesn't flag under-capacity
    measures — pickups (anacrusis) are legitimately short by design; only
    overflow (schema-invisible "too many notes in a measure") is an error.

    Also (§7.6): `beat_groups` must sum to the time signature numerator,
    every measure-stave key must reference a declared stave, and measures
    must be ordered by `idx`."""
    declared_staves = {s.get("id") for s in data.get("staves", []) or []
                        if isinstance(s, dict) and isinstance(s.get("id"), str)}
    ts = None
    for m in data.get("measures", []) or []:
        if not isinstance(m, dict):
            continue
        idx = m.get("idx")
        m_ts = m.get("ts")
        if isinstance(m_ts, list) and len(m_ts) == 2:
            ts = m_ts

        bg = m.get("beat_groups")
        if (isinstance(bg, list) and bg and all(isinstance(x, (int, float)) for x in bg)
                and isinstance(ts, list) and isinstance(ts[0], (int, float))):
            total_bg = sum(bg)
            if total_bg != ts[0]:
                rep.err(f"{f}: measure {idx}: beat_groups sum to {total_bg} but the "
                         f"time signature numerator is {ts[0]}")

        staves_map = m.get("staves") if isinstance(m.get("staves"), dict) else {}
        for sid in sorted(k for k in staves_map if k not in declared_staves):
            rep.err(f"{f}: measure {idx} references undeclared stave {sid!r}")

        if not ts or not isinstance(ts[0], (int, float)) or not ts[1]:
            continue  # capacity unknown (no ts seen yet) — nothing to compare against
        capacity = ts[0] / ts[1]
        for stave_id, stave in staves_map.items():
            if not isinstance(stave, dict):
                continue
            for v in stave.get("voices", []) or []:
                if not isinstance(v, dict):
                    continue
                total = sum(_beat_whole_notes(b) for b in v.get("beats", []) or [] if isinstance(b, dict))
                if total > capacity + 1e-6:
                    rep.err(
                        f"{f}: measure {idx} stave {stave_id!r} voice {v.get('v')}: "
                        f"beats sum to {total:.3g} whole note(s) but time signature "
                        f"{ts[0]}/{ts[1]} only holds {capacity:.3g}"
                    )

    # §7.6: measures MUST be ordered.
    _monotonic(rep, f, "measures", data.get("measures", []) or [], "idx")


def _monotonic(rep: ref.Report, f: str, kind: str, items: list, key: str) -> None:
    """Flag the first strictly-decreasing step in items[*][key] (one error per array).
    Non-dict items are skipped rather than raising — malformed entries are
    already a basic-level schema concern."""
    prev = None
    for j, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        v = it.get(key)
        if not isinstance(v, (int, float)):
            continue
        if prev is not None and v < prev:
            rep.err(f"{f}: {kind}[{j}].{key}={v} < previous {prev} (not in time order)")
            return
        prev = v


def _dupes(rep: ref.Report, label: str, ids: list) -> None:
    """Report each id that appears more than once. Unhashable ids (malformed
    data — e.g. a list/dict where a string was expected) are skipped rather
    than raising; that's a basic-level schema concern, not this check's job."""
    seen, dup = set(), set()
    for i in ids:
        try:
            hash(i)
        except TypeError:
            continue
        if i in seen:
            dup.add(i)
        seen.add(i)
    for d in dup:
        rep.err(f"duplicate {label}: {d!r}")


def _load_raw(name: str) -> dict:
    return json.loads((SPEC / "schemas" / name).read_text(encoding="utf-8"))


def _read_json(root: Path, rel: str):
    """Read a pack-relative JSON/JSONC file for strict checks. Returns the parsed
    value or None — malformed/missing files are basic-level errors already, and
    .jsonc (spec-legal for hand-edited packs, §6/§7) must not crash strict."""
    try:
        raw = (root / rel).read_text(encoding="utf-8")
        return ref._parse_jsonc(raw) if rel.endswith(".jsonc") else json.loads(raw)
    except (OSError, ValueError):
        return None


def _loc(e) -> str:
    return "/".join(str(x) for x in e.path) or "<root>"


# ---- driver -----------------------------------------------------------------

def _validate_root(root: Path, strict: bool, rep: ref.Report) -> None:
    """Basic (reference) validation of an unpacked root, plus the strict layer."""
    ref.validate_dir(root, rep)            # basic level, verbatim
    if strict and (root / "manifest.yaml").is_file():
        manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
        if isinstance(manifest, dict):
            _strict_schema_errors(manifest, root, rep)
            _strict_semantics(manifest, root, rep)


def check(pack: Path, strict: bool) -> ref.Report:
    """Resolve a *.feedpak dir or zip and run both levels on the same root, so
    strict covers zip archives (not just directories). Mirrors the reference
    validator's zip-slip guard rather than re-implementing path safety."""
    rep = ref.Report(str(pack))
    if pack.is_dir():
        _validate_root(pack, strict, rep)
    elif pack.is_file() and zipfile.is_zipfile(pack):
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(pack) as zf:
                for name in zf.namelist():
                    if (name.startswith("/") or ".." in Path(name).parts
                            or "\\" in name or ":" in name):
                        rep.err(f"unsafe path inside archive: {name}")
                if rep.ok:
                    zf.extractall(tmp)
                    _validate_root(Path(tmp), strict, rep)
    else:
        rep.err("not a directory or a zip archive")
    return rep


# Rewrite the raw validator/JSON-Schema wording into a plain statement of the
# problem, readable by a tester who has never seen a JSON Schema. Ordered subs,
# applied to each error line; anything unmatched passes through so a developer
# still gets the precise original for anything we don't have a friendly form for.
_HUMANIZE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r" \[strict\]"), ""),   # internal level tag — the header already says the level
    (re.compile(r"<root>"), "top level"),
    (re.compile(r"Additional properties are not allowed \('([^']+)' was unexpected\)"),
     r"unexpected field '\1' — not part of the feedpak spec (a typo, or data this validator doesn't recognize)"),
    (re.compile(r"'([^']+)' is a required property"), r"required field '\1' is missing"),
    (re.compile(r"([^:\s][^:]*?) is not of type '(\w+)'"), r"should be of type \2 (got \1)"),
    (re.compile(r"([^:\s][^:]*?) is not one of (\[[^\]]*\])"), r"value \1 is not allowed — must be one of \2"),
    (re.compile(r"\S+ is too short"), "must not be empty"),
    (re.compile(r"([^:\s][^:]*?) does not match [\"'][^\"']*[\"']"), r"\1 is not in the required format"),
    (re.compile(r" out of range \(templates=\[0,(\d+)\)\)"),
     r" — but this arrangement has only \1 chord template(s)"),
]


def _humanize(line: str) -> str:
    for pat, repl in _HUMANIZE:
        line = pat.sub(repl, line)
    return line


def _friendly(errors: list[str]) -> list[str]:
    """Humanize and de-duplicate (basic and strict re-check the manifest schema,
    so the same problem can be reported twice — collapse to one line)."""
    out, seen = [], set()
    for e in errors:
        h = _humanize(e)
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# ---- plain-English explanations (one per error/warning line) ----------------
# A friendly() line is still precise ("notes/0: unexpected field 'xyz'...") —
# these are a further, one-sentence "why this matters" translation for a
# reader who doesn't know what a JSON Schema is. Pattern-matched on the
# HUMANIZED text (not the raw jsonschema/reference-validator wording), since
# basic's errors come from the vendored reference validator verbatim — we
# can't tag them with a category at the source without patching a file that's
# supposed to stay pinned-verbatim. Every pattern here is tied to wording this
# module (or the vendored validator, matched post-_humanize) actually
# produces; review this list whenever a new check is added to
# _strict_semantics / _check_notation_measures / _HUMANIZE so a new error type
# doesn't silently fall through to the generic fallback.
def _has(*needles: str):
    return lambda s: all(n in s for n in needles)


_EXPLAIN: list[tuple[object, str]] = [
    (_has("beats sum to", "only holds"),
     "The sheet music crams more notes into a measure than the time signature allows — "
     "it will likely look wrong or fail to display correctly."),
    (_has("unexpected field"),
     "There's a field feedBack doesn't recognize — probably a typo or leftover from "
     "another tool. Usually harmless, but worth checking."),
    (_has("required field", "is missing"),
     "A piece of information feedBack needs is missing from this file."),
    (_has("should be of type"),
     "A value in this file is the wrong kind of data (e.g. text where a number was expected)."),
    (_has("is not allowed", "must be one of"),
     "A value in this file isn't one of the options feedBack understands."),
    (_has("must not be empty"),
     "A required piece of text is empty."),
    (_has("is not in the required format"),
     "A value in this file doesn't match the format feedBack expects (e.g. a date or "
     "version number written wrong)."),
    (_has("chord template"),
     "A chord shape points to a chord definition that doesn't exist in this pack."),
    (_has("out of range for", "string tuning"),
     "A note is written for a guitar/bass string that doesn't exist on this instrument's tuning."),
    (_has("not in time order"),
     "Some events in this song are listed out of chronological order, which can cause "
     "stutters or glitches during playback."),
    (_has("end_time", "start_time"),
     "A timed section in this song has a zero or negative length, which doesn't make "
     "sense and may break playback."),
    (lambda s: s.startswith("duplicate "),
     "Two parts of this pack are using the same internal ID, which can confuse feedBack "
     "about which one to use."),
    (_has("more than one stem marked default"),
     "More than one audio track is marked as the default — feedBack won't know which one "
     "to play first."),
    (_has("missing file referenced by manifest"),
     "This pack points to a file that isn't actually there — that file won't load."),
    (_has("lyric_tracks", "missing"),
     "This pack points to a lyrics file that isn't actually there — lyrics won't load."),
    (_has("realization ref is not a safe relative path"),
     "A tone points at an amp/cab capture file using an unsafe path — rejected for safety."),
    (_has("is not a safe relative path"),
     "This pack references a file in a way that looks unsafe — rejected for safety."),
    (_has("escapes the package root"),
     "This pack references a file in a way that looks unsafe (trying to reach outside "
     "the pack) — rejected for safety."),
    (_has("unsafe path inside archive"),
     "This pack's zip file contains an entry that looks unsafe — rejected for safety."),
    (_has("not valid YAML"),
     "This pack's core info file isn't formatted correctly and can't be read at all."),
    (_has("not valid JSON"),
     "A file in this pack isn't formatted correctly and can't be read at all."),
    (_has("no manifest.yaml at package root"),
     "This pack is missing its core info file — feedBack can't identify the song at all."),
    (_has("top level must be a mapping"),
     "This pack's core info file is structured incorrectly — feedBack can't read it."),
    (_has("is not a valid semver string"),
     "The pack's version number isn't formatted correctly."),
    (_has("not a directory or a zip archive"),
     "This isn't a valid feedpak package at all — feedBack can't open it."),
    (_has("tuning has", "the spec accepts 4 to 8"),
     "This arrangement declares an instrument with an impossible number of strings — "
     "feedpak supports 4 to 8."),
    (_has("entries for a", "-string tuning"),
     "A chord shape's fret/finger list doesn't have one entry per string, so it can't be "
     "displayed correctly."),
    (_has("fingers value", "out of range"),
     "A chord shape uses a finger number that doesn't exist (valid: thumb through pinky, "
     "or unset)."),
    (_has("does not match any stems[].id"),
     "A lyrics track points at an audio stem that isn't in this pack."),
    (_has("tones reference rig ids but the manifest has no rigs file"),
     "This song switches guitar tones by name, but the pack has no tone definitions file at all."),
    (_has("not found in rigs.json"),
     "This song references a guitar tone that isn't defined in the pack's tone library."),
    (_has("references undeclared stave"),
     "The sheet music writes notes onto a staff that was never declared, so those notes "
     "can't be rendered."),
    (_has("graph edge references unknown node"),
     "The tone's signal-routing graph connects a block that doesn't exist."),
    (_has("graph node", "does not match any block id"),
     "The tone's signal-routing graph names an effect block that isn't defined."),
    (_has("beat_groups sum to"),
     "A measure's beat grouping doesn't add up to its time signature, so beaming will "
     "render wrong."),
    (_has("is a bare", "standalone entries"),
     "A lyrics entry is just a join/line marker with no syllable text — the marker belongs "
     "at the end of a real syllable."),
    (_has("no baseline OGG/WAV stem"),
     "None of the audio files are in a universally-supported format, so some players may "
     "have nothing they can play."),
    (_has("carries bend shape", "bn is 0"),
     "A note describes the shape of a string bend but its bend amount is zero — the shape "
     "data will be ignored."),
    (_has("is non-standard", "treat it as a translation"),
     "A lyrics track has an unrecognized type label; players will fall back to treating it "
     "as a translation."),
    (_has("lyrics pointer does not name"),
     "Older players that predate multi-track lyrics may not find any lyrics to display."),
    (_has("outside the v1 vocabulary"),
     "A drum hit uses a piece name this spec version doesn't define — it will render with "
     "a generic fallback."),
    (_has("exceeds fret 24"),
     "A note sits above the 24th fret — beyond the fretboard of nearly every real instrument."),
]

_EXPLAIN_FALLBACK = "This is a lower-level technical issue that could affect how the pack loads or displays."


def _explain(line: str) -> str:
    for pred, text in _EXPLAIN:
        if pred(line):
            return text
    return _EXPLAIN_FALLBACK


def validate(path, strict: bool = False) -> dict:
    """Programmatic entry point for embedders (e.g. a fee[dB]ack plugin backend).

    Returns a JSON-serializable result — no printing, no Report internals:
        {"pack", "level", "ok", "errors": [friendly str...], "warnings": [...],
         "explanations": [str...], "warning_explanations": [str...]}
    `explanations`/`warning_explanations` are index-aligned with
    `errors`/`warnings` — one plain-English sentence per line, for a reader
    who wants to know what a specific technical line actually means.
    """
    rep = check(Path(path), strict)
    errors = _friendly(rep.errors)
    warnings = [_humanize(w) for w in rep.warnings]
    return {
        "pack": rep.label,
        "level": "strict" if strict else "basic",
        "ok": rep.ok,
        "errors": errors,
        "warnings": warnings,
        "explanations": [_explain(e) for e in errors],
        "warning_explanations": [_explain(w) for w in warnings],
    }


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    packs = [a for a in argv[1:] if not a.startswith("--")]
    if not packs:
        print(__doc__.strip())
        return 2
    failed = 0
    for arg in packs:
        rep = check(Path(arg), strict)
        for w in rep.warnings:
            print(f"  warning: {_humanize(w)}")
        print(f"{'PASS' if rep.ok else 'FAIL'}  [{'strict' if strict else 'basic'}]  {rep.label}")
        for e in _friendly(rep.errors):
            print(f"  - {e}")
        failed += not rep.ok
    print(f"\n{len(packs) - failed}/{len(packs)} valid")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
