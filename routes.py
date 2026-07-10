"""feedback-validator plugin — routes.py

Surfaces the vendored feedpak validator (fpvalidate.py) as an in-app HTTP
service. Two ways to call it:

  POST /api/plugins/feedback-validator/validate         {ids, strict}
  POST /api/plugins/feedback-validator/validate-upload  multipart file(s)

plus GET /packs to enumerate validatable packs already in the library.

Security: the client only ever sends opaque pack *ids* (from /packs), never
filesystem paths, so the validator can't be aimed at arbitrary server files.
Every resolved path is containment-checked against the enumeration roots.
Uploads are validated as a private temp copy and deleted afterwards.
"""

import hashlib
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

# feedpak packages carry audio stems; allow room but cap to avoid disk abuse.
_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
_PACK_SUFFIXES = (".feedpak", ".sloppak")  # same on-disk format (see CLAUDE.md)

# A library can hold thousands of packs — /packs is paginated so the response
# and the DOM list it renders stay bounded, and /validate caps how many can be
# checked synchronously in one request (there's no job queue; see plan).
_DEFAULT_PACK_LIMIT = 300
_MAX_PACK_LIMIT = 1000
_MAX_VALIDATE_BATCH = 200


def _is_within(root: Path, candidate: Path) -> bool:
    """True iff candidate resolves inside root (normalising ../ and symlinks).
    Mirrors folder_library._is_within — the containment backstop that stops a
    symlinked pack from escaping an enumeration root."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _pack_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def setup(app, context):
    log = context["log"]
    fp = context["load_sibling"]("fpvalidate")  # the vendored validator
    router = APIRouter(prefix="/api/plugins/feedback-validator")

    def _roots() -> list[tuple[str, Path]]:
        """(source-label, dir) pairs to scan for packs. Library only — the
        sloppak_cache/ extraction cache is deliberately excluded: any pack
        that's been opened once gets an unpacked working copy there under a
        flattened name (see lib/sloppak.py resolve_source_dir), which showed
        up as a confusing duplicate of the same pack already in the library."""
        out: list[tuple[str, Path]] = []
        try:
            dlc = Path(context["get_dlc_dir"]())
            for cand in (dlc / "sloppak", dlc):  # DLC stores packs under sloppak/
                if cand.is_dir():
                    out.append(("library", cand))
                    break
        except Exception:
            pass
        return out

    def _enumerate() -> dict[str, dict]:
        """id -> {id, name, source, path, root}. Re-run per request so the id
        set is always authoritative for the current filesystem state."""
        packs: dict[str, dict] = {}
        for source, root in _roots():
            for suffix in _PACK_SUFFIXES:
                for p in sorted(root.rglob(f"*{suffix}")):
                    if not _is_within(root, p):
                        continue
                    pid = _pack_id(p)
                    packs.setdefault(pid, {
                        "id": pid, "name": p.name, "source": source,
                        "path": p, "root": root,
                    })
        return packs

    @router.get("/spec-info")
    def spec_info():
        return JSONResponse(fp.spec_info())

    @router.get("/packs")
    def list_packs(q: str = "", limit: int = _DEFAULT_PACK_LIMIT, offset: int = 0):
        # ponytail: rescans the filesystem on every call rather than caching —
        # simple and correct; if a library with tens of thousands of packs makes
        # this call noticeably slow, add a cache invalidated the way
        # folder_library invalidates its tree cache on mutation.
        entries = sorted(_enumerate().values(), key=lambda e: (e["source"], e["name"]))
        if q.strip():
            needle = q.strip().lower()
            entries = [e for e in entries if needle in e["name"].lower()]
        total = len(entries)
        limit = max(1, min(limit, _MAX_PACK_LIMIT))
        offset = max(0, offset)
        page = entries[offset:offset + limit]
        # Never leak server paths to the client — only id/name/source.
        return JSONResponse({
            "items": [{"id": e["id"], "name": e["name"], "source": e["source"]} for e in page],
            "total": total,
            "offset": offset,
            "limit": limit,
        })

    @router.post("/validate")
    async def validate_library(request: Request):
        body = await request.json()
        ids = body.get("ids") or []
        strict = bool(body.get("strict", False))
        if not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a list"}, status_code=400)
        if len(ids) > _MAX_VALIDATE_BATCH:
            return JSONResponse({
                "error": f"too many packs selected ({len(ids)}); validate at most "
                         f"{_MAX_VALIDATE_BATCH} at a time — narrow your search or "
                         f"run in smaller batches",
            }, status_code=400)

        packs = _enumerate()
        level = "strict" if strict else "basic"
        results = []
        for pid in ids:
            entry = packs.get(pid)
            if not entry or not _is_within(entry["root"], entry["path"]):
                results.append({
                    "pack": str(pid), "level": level, "ok": False,
                    "errors": ["unknown or unavailable pack (re-scan the library)"],
                    "warnings": [],
                })
                continue
            try:
                results.append(fp.validate(str(entry["path"]), strict))
            except Exception as exc:
                log.exception("validation crashed for pack %r", entry["name"])
                results.append({
                    "pack": entry["name"], "level": level, "ok": False,
                    "errors": [f"validator error: {exc}"], "warnings": [],
                })
        passed = sum(1 for r in results if r["ok"])
        return JSONResponse({"results": results, "passed": passed, "total": len(results)})

    @router.post("/validate-upload")
    async def validate_upload(files: list[UploadFile] = File(...),
                              strict: bool = Form(False)):
        results = []
        level = "strict" if strict else "basic"
        for uf in files:
            name = uf.filename or "upload"
            if not name.lower().endswith(_PACK_SUFFIXES + (".zip",)):
                results.append({
                    "pack": name, "level": level, "ok": False,
                    "errors": ["not a .feedpak / .sloppak / .zip file"], "warnings": [],
                })
                continue
            suffix = Path(name).suffix or ".feedpak"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            try:
                written = 0
                while chunk := await uf.read(1024 * 1024):
                    written += len(chunk)
                    if written > _MAX_UPLOAD_BYTES:
                        raise ValueError("file exceeds the upload size limit")
                    tmp.write(chunk)
                tmp.close()
                results.append(fp.validate(tmp.name, strict))
                # validate() reports the temp path as the pack label — restore the
                # user's original filename so the UI shows something meaningful.
                results[-1]["pack"] = name
            except Exception as exc:
                log.warning("upload validation failed for %r: %s", name, exc)
                results.append({
                    "pack": name, "level": level, "ok": False,
                    "errors": [f"{exc}"], "warnings": [],
                })
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        passed = sum(1 for r in results if r["ok"])
        return JSONResponse({"results": results, "passed": passed, "total": len(results)})

    app.include_router(router)
    log.info("feedback-validator ready (%d pack(s) discoverable)", len(_enumerate()))
