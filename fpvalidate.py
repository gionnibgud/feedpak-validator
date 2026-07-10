#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
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
        f = arr.get("file")
        if f and (root / f).is_file():
            data = json.loads((root / f).read_text(encoding="utf-8"))
            for e in av.iter_errors(data):
                rep.err(f"{f} [strict]: {_loc(e)}: {e.message}")


def _strict_semantics(manifest: dict, root: Path, rep: ref.Report) -> None:
    arrs = manifest.get("arrangements", []) or []
    stems = manifest.get("stems", []) or []

    _dupes(rep, "arrangement id", [a.get("id") for a in arrs])
    _dupes(rep, "stem id", [s.get("id") for s in stems])

    if sum(1 for s in stems if s.get("default")) > 1:
        rep.err("more than one stem marked default:true")

    # lyric_tracks side-files: validate.py never opens these (PLAN.MD:107)
    for i, t in enumerate(manifest.get("lyric_tracks", []) or []):
        f = t.get("file")
        if f and not (root / f).is_file():
            rep.err(f"lyric_tracks[{i}].file missing: {f}")

    for a in arrs:
        f = a.get("file")
        if not f or not (root / f).is_file():
            continue
        d = json.loads((root / f).read_text(encoding="utf-8"))
        nstr = len(d.get("tuning", [])) or 6
        ntpl = len(d.get("templates", []))   # chord_id / chord.id index into templates
        for n in d.get("notes", []):
            s = n.get("s")
            if isinstance(s, int) and not (0 <= s < nstr):
                rep.err(f"{f}: note.s={s} out of range for {nstr}-string tuning")
        for h in d.get("handshapes", []):
            cid = h.get("chord_id")
            if isinstance(cid, int) and not (0 <= cid < ntpl):
                rep.err(f"{f}: handshape.chord_id={cid} out of range (templates=[0,{ntpl}))")
        for c in d.get("chords", []):
            cid = c.get("id")
            if isinstance(cid, int) and not (0 <= cid < ntpl):
                rep.err(f"{f}: chord.id={cid} out of range (templates=[0,{ntpl}))")

        # time ordering: these arrays are authored in time order; flag the first
        # strictly-decreasing step. Shared timestamps are legal (many objects at
        # the same t), so compare with '<', not '<='.
        _monotonic(rep, f, "notes", d.get("notes", []), "t")
        _monotonic(rep, f, "chords", d.get("chords", []), "t")
        _monotonic(rep, f, "anchors", d.get("anchors", []), "time")
        _monotonic(rep, f, "beats", d.get("beats", []), "time")
        _monotonic(rep, f, "sections", d.get("sections", []), "time")
        _monotonic(rep, f, "tempos", d.get("tempos", []), "time")
        # spans must have positive length.
        for kind, span in (("handshapes", d.get("handshapes", [])),
                           ("phrases", d.get("phrases", []))):
            for j, s in enumerate(span):
                a, b = s.get("start_time"), s.get("end_time")
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b <= a:
                    rep.err(f"{f}: {kind}[{j}] end_time {b} <= start_time {a}")
                    break

    # notation_<id>.json (§7.6): a per-arrangement side-file, not the `file`
    # loop above — a notation-only arrangement MAY omit `file` entirely, so
    # this runs independent of the `if not f` skip.
    for a in arrs:
        nf = a.get("notation")
        if not nf or not (root / nf).is_file():
            continue
        try:
            ndata = json.loads((root / nf).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # malformed JSON is already a basic-level schema failure
        _check_notation_measures(rep, nf, ndata)


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
    overflow (schema-invisible "too many notes in a measure") is an error."""
    ts = None
    for m in data.get("measures", []) or []:
        m_ts = m.get("ts")
        if isinstance(m_ts, list) and len(m_ts) == 2:
            ts = m_ts
        if not ts or not isinstance(ts[0], (int, float)) or not ts[1]:
            continue  # capacity unknown (no ts seen yet) — nothing to compare against
        capacity = ts[0] / ts[1]
        idx = m.get("idx")
        for stave_id, stave in (m.get("staves") or {}).items():
            for v in stave.get("voices", []) or []:
                total = sum(_beat_whole_notes(b) for b in v.get("beats", []) or [])
                if total > capacity + 1e-6:
                    rep.err(
                        f"{f}: measure {idx} stave {stave_id!r} voice {v.get('v')}: "
                        f"beats sum to {total:.3g} whole note(s) but time signature "
                        f"{ts[0]}/{ts[1]} only holds {capacity:.3g}"
                    )


def _monotonic(rep: ref.Report, f: str, kind: str, items: list, key: str) -> None:
    """Flag the first strictly-decreasing step in items[*][key] (one error per array)."""
    prev = None
    for j, it in enumerate(items):
        v = it.get(key)
        if not isinstance(v, (int, float)):
            continue
        if prev is not None and v < prev:
            rep.err(f"{f}: {kind}[{j}].{key}={v} < previous {prev} (not in time order)")
            return
        prev = v


def _dupes(rep: ref.Report, label: str, ids: list) -> None:
    seen, dup = set(), set()
    for i in ids:
        if i in seen:
            dup.add(i)
        seen.add(i)
    for d in dup:
        rep.err(f"duplicate {label}: {d!r}")


def _load_raw(name: str) -> dict:
    return json.loads((SPEC / "schemas" / name).read_text(encoding="utf-8"))


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


def validate(path, strict: bool = False) -> dict:
    """Programmatic entry point for embedders (e.g. a fee[dB]ack plugin backend).

    Returns a JSON-serializable result — no printing, no Report internals:
        {"pack", "level", "ok", "errors": [friendly str...], "warnings": [...]}
    """
    rep = check(Path(path), strict)
    return {
        "pack": rep.label,
        "level": "strict" if strict else "basic",
        "ok": rep.ok,
        "errors": _friendly(rep.errors),
        "warnings": [_humanize(w) for w in rep.warnings],
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


def _cli() -> None:
    """Zero-arg entry point for the `fpvalidate` console script."""
    raise SystemExit(main(sys.argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
