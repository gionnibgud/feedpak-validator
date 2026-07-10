# Feedpak Validator — feedBack plugin

Validates `.feedpak` packages against the [feedpak spec](https://github.com/got-feedBack/feedpak-spec)
from inside feedBack — as a standalone screen **and** as a service other plugins (e.g. the
editor) can call. Wraps the two-level validator (`fpvalidate.py`, vendored, pinned to
feedpak-spec **v1.14.0**).

| Level | What it checks |
|-------|----------------|
| **basic** | Spec conformance: JSON Schema, referenced-file existence, path safety, zip-slip guards. |
| **strict** | Everything in basic, plus invariants no schema can express: unknown keys, id uniqueness, `note.s` within the tuning, dangling chord refs, non-decreasing times, positive-length spans, lyric side-files exist, and `notation_<id>.json` measures that don't overflow their time signature. |

## Install

Copy this folder into a feedBack checkout as `plugins/feedback-validator/`. The loader
installs `requirements.txt` (`pyyaml`, `jsonschema`, `python-multipart`) on first boot and
mounts the routes. A **Validator** entry appears in the nav.

## Standalone use

Open **Validator**, search/pick library packs and/or drop `.feedpak` / `.sloppak` / `.zip`
files, toggle **Strict**, and read the per-pack PASS/FAIL report. Each failure names the file
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
`{ pack, level, ok, errors: [str], warnings: [str] }`:

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
| POST | `/validate` | `{ids: [str], strict: bool}` (max 200 ids) | `{results, passed, total}` |
| POST | `/validate-upload` | multipart `files[]` + `strict` | `{results, passed, total}` |

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

MIT (see `LICENSE`). The vendored spec under `vendor/feedpak-spec/` carries its own licenses.
