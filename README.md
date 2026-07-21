# Feedpak Validator — feedBack plugin

Validates `.feedpak` packages against the [feedpak spec](https://github.com/got-feedBack/feedpak-spec)
from inside feedBack — as a standalone screen **and** as a service other plugins (e.g. the
editor) can call. Wraps the two-level validator (`fpvalidate.py`, vendored, pinned to
feedpak-spec **v1.18.0**).

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
6. Every `arrangements[].notation` and `arrangements[].drum_tab` pointer (if present): same
   path/existence checks, and the JSON conforms to `notation.schema.json` /
   `drum-tab.schema.json` (per-arrangement drum charts, v1.17).
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
   flagged — not a real spec field the schema just doesn't list. A `.jsonc` arrangement (comments,
   §6/§8) is parsed the same way basic parses it, instead of crashing strict.
3. **Duplicate ids** — `arrangements[].id`, `stems[].id`, `lyric_tracks[].id`, `rigs.json`
   `rigs[].id`, `drum_tab.json` `kit[].id`, and each `notation_<id>.json`'s `staves[].id`.
4. **Reserved `full` stem misuse (§5.3, v1.15/v1.16)** — a stem `id: full` is the complete mixdown,
   not an instrument layer, so it **MUST NOT** be `default: true` in a pack that also ships
   per-instrument stems (a Reader summing enabled stems would double the whole song). `default` is
   read via the case-insensitive `true`/`false`/`on`/`off`/`yes`/`no` strings the spec requires
   readers to understand, not just a real boolean. (Multiple *instrument* stems marked default is a
   normal mix and is **not** flagged — the spec sets no at-most-one-default rule.) Retaining `full`
   after separation is **version-scoped** (v1.16): a `stem_separation` block with no `full` stem is
   an **error** when the pack declares `feedpak_version ≥ 1.16.0` (the SHOULD became a MUST), and a
   warning below that — see [warnings](#warnings-strict--should-level). The optional per-stem `name`
   / `description` display fields added in v1.16 are recognized (schema-defined), so strict's
   closed-world check doesn't flag them.
5. **`lyric_tracks[].file` existence and schema** — basic/the reference validator never opens
   these; strict also schema-validates each track's contents against the plain `lyrics.schema.json`
   and checks `lyric_tracks[].stem` resolves to a real `stems[].id`.
6. **`note.s` / chord-note `.s` (string index) in range** for the arrangement's *effective*
   tuning — the manifest-level `tuning` overrides the arrangement JSON's (§5.2) — including
   notes and chords inside `phrases[].levels[]`, not just the chart's top level.
7. **`handshape.chord_id` / `chord.id` in range** of the arrangement's chord `templates`,
   including inside `phrases[].levels[]`.
8. **Arrangement tuning length is 4–8 strings** (§5.2), and chord **template `frets`/`fingers`
   arrays match the string count** with **`fingers` values in `-1..4`** (§6.6) — skipped when a
   shape array is empty (the default/absent case).
9. **Non-decreasing time ordering** for `notes[].t`, `chords[].t`, `anchors[].time`,
   `beats[].time`, `sections[].time`, `tempos[].time` (including inside `phrases[].levels[]`),
   plus note/chord-note `bnv` curve points, an arrangement's `tones.changes[].t`,
   `song_timeline.json`'s `tempos`/`time_signatures`/`beats`/`sections`, `keys.json`/`harmony.json`
   `events[].t`, `drum_tab.json` `hits[].t`, `rigs.json` per-block `automation[].points[].t`, and
   `notation_<id>.json` `measures[].idx` (flags the first out-of-order entry per array; equal
   timestamps are legal).
10. **Positive-length spans** for `handshapes[]` and `phrases[]` (`end_time` must be `>
    start_time`), including handshapes inside `phrases[].levels[]`.
11. **Dangling references** — `tones.base_rig` / `tones.changes[].rig` must resolve against
    `rigs.json` (and the manifest must have a `rigs` pointer at all if any are referenced). Since
    v1.18 a tone binding may also sit on a **manifest arrangement entry** (`arrangements[].tones`)
    or on the song-level **`drum_tones`** — including on notation-only and `type: drums` entries
    that have no arrangement JSON at all — and those resolve too. Also: `rigs.json` `graph`
    edges/nodes must resolve to declared nodes/block ids; a `nam`/`ir`/`soundfont` realization
    `ref` must be a safe relative path (or contain `://` for a URI).
12. **`notation_<id>.json` measures don't overflow their time signature** — each stave/voice's
    beat durations (honoring `dot` and `tu` tuplet ratios) are summed and compared against the
    measure's `ts` capacity, carried forward across measures that omit `ts` (§7.6 "omit if
    unchanged"). Catches schema-invisible corruption like a beat-grid generator that stopped
    early and dumped an entire song into one measure.
13. **`notation_<id>.json` `beat_groups` sum to the time signature numerator** (§7.6), and every
    measure `staves` key resolves to a declared `staves[].id`.
14. **`lyrics.json` / `lyric_tracks` entries reject a bare `"-"`/`"+"`** as `w` (§7.1) — a
    join/line marker is a suffix on a real syllable, not a standalone entry.
15. **Drum parts as arrangements (§5.2/§7.5, v1.17)** — since a pack may carry several drum
    charts, **every** `drum_tab` gets the §7.5 checks above (hit time-ordering, duplicate
    `kit[].id`, piece-vocabulary warning), not just the song-level one. Per-arrangement pointers
    are included, deduped by **resolved path** (the primary drum part aliases the song-level
    file, and `x.json` / `./x.json` are one file). The drum-part consistency rules themselves are
    SHOULD-level warnings — see [warnings](#warnings-strict--should-level) — and are
    **version-scoped to packs declaring `feedpak_version` ≥ 1.17.0**, because `type` predates
    v1.17 as a free-form instrument hint and no earlier pack may be retroactively faulted.
16. **MIDI sound sources (§7.9, v1.18)** — a `soundfont` realization **MUST** carry a `ref`
    (the schema only documents this in a `$comment`, so basic can't enforce it); its `ref` gets
    the same path-safety guard as `nam`/`ir`. Being on a non-`source` block is a *warning*, and
    a block that simply **omits** the OPTIONAL `role` is not flagged at all — that is the spec's
    own minimal single-block instrument rig. `intent.gm` ranges (`program` 0–127, `kit` ≥ 0) are
    schema-enforced, so basic already covers them.
17. **Manifest tone bindings reject unknown keys** — `arrangements[].tones` and `drum_tones` are
    typed as bare objects in the schema, so the closed-world manifest re-check can't see inside
    them; strict closes them against §6.9's key set (`base`, `base_rig`, `changes`,
    `definitions`). Without this a typo'd `base_rigg` leaves the part silently unbound.

**Path safety.** Strict runs even when basic has already rejected a pointer, so every strict
side-file read is guarded — a `..` pointer is never opened, and no content from outside the
package root can reach a validation message.

### warnings (strict) — SHOULD-level

Strict also emits **warnings** (don't fail the pack) for SHOULD-level rules the spec allows a
Reader to tolerate:

- **Separated pack with no `full` mixdown** (§5.3) — a `stem_separation` block is present
  (the stems were machine-separated) but no stem `id: full` is retained. Separation is lossy, so
  the original mix can't be rebuilt from the parts; the spec keeps `full` (`default: false`).
  **Warning below `feedpak_version` 1.16.0** (SHOULD); a pack declaring **≥ 1.16.0** turns this into
  an **error** (the MUST is version-scoped, so no pre-1.16 pack becomes non-conformant).
The drum-part rules below are **version-scoped to packs declaring `feedpak_version` ≥ 1.17.0**.

- **An arrangement with a `drum_tab` but no `type: drums`** (§5.2, v1.17) — a drum-tab pointer
  makes the entry a drum part, so it SHOULD say so or a player may not offer it as one.
- **A `type: drums` entry carrying a `file` or `notation`** (§5.2, v1.17) — a drum part's chart
  is its `drum_tab`. A warning, not an error: the spec's MUST NOT covers only *selection and
  grading*, and the schema's `anyOf` permits the combination.
- **A song-level `drum_tab` that aliases no drum part, or is missing entirely** (§7.5, v1.17) —
  when `type: drums` arrangements exist the song-level key is the *primary* part's back-compat
  alias and SHOULD name one of their files; a wrong pointer shows an older Reader a stray extra
  chart, and an absent one leaves it with no drum chart at all. With **no** `type: drums`
  arrangements the song-level key is legitimately the single drum part, so nothing is flagged.
- **`drum_tones` naming a different rig than the primary drum part's own `tones`** (§5.1/§7.5,
  v1.18) — the entry `tones` takes precedence, so a divergence voices the kit differently
  depending on whether the Reader supports per-arrangement drum parts.
- **A `soundfont` realization on an explicitly non-`source` block** (§7.9, v1.18) — the engine is
  reserved for generator blocks. A block that omits the OPTIONAL `role` is not flagged.
- **An arrangement carrying both manifest and in-JSON `tones`** (§5.2, v1.18) — the manifest
  binding wins *wholesale*, so the chart's own tones are silently discarded. Writers SHOULD NOT
  emit both.
- **No OGG/WAV baseline stem** (§5.3.2) — the pack's resolved stem codecs (explicit `codec`
  field, else file extension) include neither `vorbis` nor `pcm`, so a leaner Reader may have
  nothing it can decode.
- **A note/chord-note carries a bend shape (`bt`/`bnv`) but `bn` is `0`** (§6.2.1 SHOULD NOT).
- **A `lyric_tracks[].kind`** isn't `original`/`transliteration`/`translation`, or **`lyrics`
  doesn't point at the `kind: original` track's file** (§5.5) — a pre-1.11 Reader may show
  nothing.
- **A `drum_tab.json` hit's piece id is outside the closed v1 vocabulary** (§7.5) — still
  round-trips, just renders with a generic fallback.
- **A note's fret exceeds 24** (§6.2) — beyond the fretboard of nearly every real instrument.

## Install

Copy this folder into a feedBack checkout as `plugins/feedback-validator/`. The loader
installs `requirements.txt` (`pyyaml`, `jsonschema`, `python-multipart`) on first boot and
mounts the routes. A **Validator** entry appears in the nav.

## Standalone use

Open **Validator**, search/pick library packs and/or drop `.feedpak` / `.sloppak` / `.zip`
files, and read the per-pack PASS/FAIL report. **Strict is on by default** — basic is spec
conformance only and misses things like a notation measure overflowing its time signature
(schema-valid, but broken), so a pack that would silently pass basic is exactly what strict
exists to catch; uncheck it to see the looser, schema-only result. Each pack card leads with a
plain-language takeaway ("This pack has 2 problems that need fixing…") for a non-dev reader,
then a collapsible **Technical details** section with the precise, per-field breakdown — each
failure names the file and the exact cause, e.g.
`arrangements/lead.json: notes/0: unexpected field 'xyz' — not part of the feedpak spec`, with
its own plain-English line underneath it (e.g. "There's a field feedBack doesn't recognize…").
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
`{ pack, level, ok, errors: [str], warnings: [str], explanations: [str], warning_explanations: [str] }`.
`explanations`/`warning_explanations` are index-aligned with `errors`/`warnings` — one
plain-English sentence per technical line, pattern-matched by error category
(`fpvalidate._EXPLAIN`), not a per-pack summary. **Defaults to `strict: true`** — pass
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
