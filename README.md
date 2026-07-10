# Feedpak Validator — feedBack plugin

Validates `.feedpak` packages against the [feedpak spec](https://github.com/got-feedBack/feedpak-spec)
from inside feedBack — as a standalone screen **and** as a service other plugins (e.g. the
editor) can call. Wraps the two-level validator (`fpvalidate.py`, vendored, pinned to
feedpak-spec **v1.14.0**).

| Level | What it checks |
|-------|----------------|
| **basic** | Spec conformance: JSON Schema, referenced-file existence, path safety, zip-slip guards. Exactly what the official reference validator checks — see [What gets checked](#what-gets-checked) for the full, explicit list. |
| **strict** | Everything in basic, plus invariants no schema can express — full list below. |

## What gets checked

### basic — spec conformance

Runs the vendored official reference validator (`vendor/feedpak-spec/tools/validate.py`)
unmodified. On every pack:

1. `manifest.yaml` exists at the package root and doesn't escape it through a symlink.
2. `manifest.yaml` is valid YAML and its top level is a mapping.
3. `manifest.yaml` conforms to `manifest.schema.json` (JSON Schema, Draft 2020-12).
4. `feedpak_version` is a valid semver string; a major version newer than this validator
   supports is a **warning**, not a failure.
5. Every `arrangements[].file` pointer: safe relative path (no `..`, no leading `/`, no
   backslash, no drive letter), exists, doesn't escape the package root — and the JSON it
   points to conforms to `arrangement.schema.json`.
6. Every `arrangements[].notation` pointer (if present): same path/existence checks, and the
   JSON conforms to `notation.schema.json`.
7. Every `stems[].file` pointer: safe relative path, exists, doesn't escape the root.
8. Every optional JSON side-file the manifest actually references — `lyrics`, `vocal_pitch`,
   `song_timeline`, `drum_tab`, `vocal_pitch_contour`, `keys`, `harmony`, `rigs`: safe path,
   exists, doesn't escape the root, and conforms to its own schema.
9. Non-JSON pointers — `cover`, `preview`: safe path, exists, doesn't escape the root (no
   schema check — not JSON).
10. Zip-form packs only: a zip-slip guard rejects archive entries with absolute paths, `..`
    segments, backslashes, or drive letters/colons *before* extracting anything.

Loose by design — the schemas allow unknown fields and require few of them, so basic is
"nothing is missing or malformed," not "every field is exactly right."

### strict — everything in basic, plus

Invariants the JSON Schemas can't express (`fpvalidate.py`'s `_strict_schema_errors` /
`_strict_semantics`):

1. **Unknown keys rejected on `manifest.yaml`** — closed-world re-check of the manifest schema
   (`additionalProperties: false` on every object node).
2. **Unknown keys rejected on every arrangement JSON** — same closed-world check, but the
   `note`/`chordNote` schema defs are first patched with the real §6.2 field set the schema
   omits (`NOTE_EXTRA` in `fpvalidate.py`), so a genuinely-unknown field (a typo) is what gets
   flagged — not a real spec field the schema just doesn't list.
3. **Duplicate `arrangements[].id` values.**
4. **Duplicate `stems[].id` values.**
5. **More than one stem marked `default: true`.**
6. **`lyric_tracks[].file` existence** — basic/the reference validator never opens these.
7. **`note.s` (string index) in range** for the arrangement's tuning.
8. **`handshape.chord_id` in range** of the arrangement's chord `templates`.
9. **`chord.id` in range** of the arrangement's chord `templates`.
10. **Non-decreasing time ordering** for `notes[].t`, `chords[].t`, `anchors[].time`,
    `beats[].time`, `sections[].time`, `tempos[].time` (flags the first out-of-order entry per
    array; equal timestamps are legal).
11. **Positive-length spans** for `handshapes[]` and `phrases[]` (`end_time` must be `>
    start_time`).
12. **`notation_<id>.json` measures don't overflow their time signature** — each stave/voice's
    beat durations (honoring `dot` and `tu` tuplet ratios) are summed and compared against the
    measure's `ts` capacity, carried forward across measures that omit `ts` (§7.6 "omit if
    unchanged"). Catches schema-invisible corruption like a beat-grid generator that stopped
    early and dumped an entire song into one measure.

## Install

Copy this folder into a feedBack checkout as `plugins/feedback-validator/`. The loader
installs `requirements.txt` (`pyyaml`, `jsonschema`, `python-multipart`) on first boot and
mounts the routes. A **Validator** entry appears in the nav.

## Standalone use

Open **Validator**, search/pick library packs and/or drop `.feedpak` / `.sloppak` / `.zip`
files, and read the per-pack PASS/FAIL report. **Strict is on by default** — basic is spec
conformance only and misses things like a notation measure overflowing its time signature
(schema-valid, but broken), so a pack that would silently pass basic is exactly what strict
exists to catch; uncheck it to see the looser, schema-only result. Each failure names the file
and the plain-English cause, e.g.
`arrangements/lead.json: notes/0: unexpected field 'xyz' — not part of the feedpak spec`.
The header shows which pinned feedpak-spec version basic is checking against (linked to the
exact commit) — purely informational; see [Versioning](#versioning) for how it's updated.

**Large libraries.** `/packs` is paginated (300 per page, 1000 max) and searchable by name, so
the UI never renders a library's full pack list at once — type to narrow it down. Selection is
tracked independently of what's currently rendered, so picking packs across multiple searches
doesn't lose earlier picks. A single `/validate` call is capped at 200 packs (`_MAX_VALIDATE_BATCH`
in `routes.py`) since validation is synchronous with no job queue behind it — the UI blocks
"Validate selected" and explains when a selection exceeds that.

## Service API (for other plugins)

`screen.js` publishes `window.feedBackValidator` at plugin load and emits `validator:ready`
on the `window.feedBack` bus. Each call resolves to one result dict
`{ pack, level, ok, errors: [str], warnings: [str] }`. **Defaults to `strict: true`** — pass
`{ strict: false }` explicitly for the looser, schema-only check:

```js
// feature-detect (plugin load order isn't guaranteed)
if (typeof window.feedBackValidator?.validate === 'function') {
    // a saved library pack, by id (ids come from GET /api/plugins/feedback-validator/packs)
    const r = await window.feedBackValidator.validatePack(packId, { strict: true });

    // an unsaved song the editor is holding — serialize to a .feedpak zip Blob, then:
    const r2 = await window.feedBackValidator.validateBytes(zipBlob, { strict: true, filename: 'wip.feedpak' });

    // dispatches by type: string → validatePack, Blob/File → validateBytes
    const r3 = await window.feedBackValidator.validate(input, { strict: false });
}
```

The validator is path/bytes-based; a plugin with an in-memory song serializes it to a
`.feedpak` zip and passes the bytes — no server-side coupling to any editor's data model.

## HTTP endpoints

All under `/api/plugins/feedback-validator/`:

| Method | Path | Query / Body | Response |
|--------|------|------|----------|
| GET  | `/spec-info` | — | `{repo, tag, commit}` — the pinned feedpak-spec version (from `vendor/feedpak-spec/VENDOR.txt`) that basic validates against |
| GET  | `/packs` | `?q=&limit=300&offset=0` | `{items: [{id, name, source}], total, offset, limit}` |
| POST | `/validate` | `{ids: [str], strict: bool}` (default `true`, max 200 ids) | `{results, passed, total}` |
| POST | `/validate-upload` | multipart `files[]` + `strict` (default `true`) | `{results, passed, total}` |

Clients send opaque pack **ids** (never filesystem paths); the server resolves them against
the current library enumeration and containment-checks every path, so the validator can't be
aimed at arbitrary server files. Uploads are validated as a private temp copy and deleted.
`/validate` returns 400 for more than 200 ids — batch synchronously, not all at once.

## Versioning

Basic validation is the official `got-feedback/feedpak-spec` reference validator, vendored
verbatim under `vendor/feedpak-spec/`, pinned to a specific tag + commit in
`vendor/feedpak-spec/VENDOR.txt`. `/spec-info` (and the Validator screen header) surfaces that
pin so users can see exactly what basic is checking against.

This is deliberately **not** a live setting. Strict is hand-patched against the exact shape of
the pinned schema (`NOTE_EXTRA`'s field allowlist, the `chordNote` derivation in
`fpvalidate.py`) — a spec bump needs a human to re-check those patches, not just swap files, or
strict can silently false-positive, miss things, or crash on a restructured schema. Bumping the
pin is a maintainer action: update `vendor/feedpak-spec/` + `VENDOR.txt`, re-run
`test_fpvalidate.py`, and review `_strict_schema_errors` / `_strict_semantics` against the new
schema shape.

## Tests

```sh
python test_fpvalidate.py   # validator self-check (dirs + zips, basic vs strict)
python test_routes.py       # backend: enumeration, library + upload validation, rejected forgery
```

## License

AGPL-3.0-only (see `LICENSE`) — matching feedBack core, since this plugin is authored by and
lives with the rest of the feedBack ecosystem; no separate license for the validator.

`vendor/feedpak-spec/` is a third-party dependency, not our code — it stays under its own
upstream terms regardless of this plugin's license: `LICENSE` (CC0-1.0, the spec document +
schemas) and `LICENSE-CODE` (MIT, the reference validator), per `VENDOR.txt`'s pin. AGPL and
MIT/CC0 are compatible for inclusion — the vendored files keep their own notices and license,
only this plugin's own code (`fpvalidate.py`, `routes.py`, `screen.js`, etc.) is AGPL-3.0-only.
