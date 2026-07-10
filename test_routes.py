#!/usr/bin/env python3
"""Route-level self-check for the feedback-validator plugin backend.

Wires routes.setup() with a fake host `context` pointed at the vendored example
packs, then drives the three endpoints through Starlette's TestClient. Asserts
the happy paths AND the trust boundary (a forged/unknown id is rejected, never
validated). Run: python test_routes.py  (needs fastapi + httpx installed).
"""
import io
import logging
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE / "vendor" / "feedpak-spec" / "examples"


def _load_sibling(name):
    import importlib
    return importlib.import_module(name)


def _context():
    # No get_sloppak_cache_dir: /packs is library-only (feedback: a pack once
    # opened gets an extracted working copy in sloppak_cache/ under a
    # flattened name, which showed up as a confusing duplicate of the same
    # pack already in the library) — routes.py must not need this key at all.
    return {
        "log": logging.getLogger("test"),
        "load_sibling": _load_sibling,
        "get_dlc_dir": lambda: str(EXAMPLES),      # examples/ has no sloppak/ subdir
    }


app = FastAPI()
routes.setup(app, _context())
client = TestClient(app)
BASE = "/api/plugins/feedback-validator"

# /packs — both example packs discovered, no server paths leaked.
resp = client.get(f"{BASE}/packs").json()
packs = resp["items"]
names = {p["name"] for p in packs}
assert {"minimal.feedpak", "extended.feedpak"} <= names, packs
assert all(set(p) == {"id", "name", "source"} for p in packs), "leaked fields"
assert resp["total"] == len(packs), resp
by_name = {p["name"]: p["id"] for p in packs}

# Library-only: even if a host still passes get_sloppak_cache_dir, a pack
# living only under the cache root must NOT be enumerated — only the DLC
# library, to avoid the extracted-working-copy duplicates users were seeing.
with tempfile.TemporaryDirectory() as t:
    dlc_only, cache_only = Path(t) / "dlc", Path(t) / "cache"
    dlc_only.mkdir(); cache_only.mkdir()
    (dlc_only / "in_library.feedpak").write_bytes(b"x")
    (cache_only / "in_cache_only.feedpak").write_bytes(b"x")
    iso_app = FastAPI()
    routes.setup(iso_app, {
        "log": logging.getLogger("test-iso"),
        "load_sibling": _load_sibling,
        "get_dlc_dir": lambda: str(dlc_only),
        "get_sloppak_cache_dir": lambda: str(cache_only),  # must be ignored
    })
    iso_names = {p["name"] for p in TestClient(iso_app).get(f"{BASE}/packs").json()["items"]}
    assert iso_names == {"in_library.feedpak"}, iso_names

# search narrows by name (case-insensitive substring) — the interface a user
# with a large library relies on instead of scrolling a giant checkbox list.
r = client.get(f"{BASE}/packs", params={"q": "MINIMAL"}).json()
assert {p["name"] for p in r["items"]} == {"minimal.feedpak"}, r
assert r["total"] == 1, r

# limit/offset paginate so the payload and rendered list stay bounded
# regardless of how many packs are in the library.
r = client.get(f"{BASE}/packs", params={"limit": 1, "offset": 0}).json()
assert len(r["items"]) == 1 and r["total"] == len(packs), r
r2 = client.get(f"{BASE}/packs", params={"limit": 1, "offset": 1}).json()
assert r["items"][0]["id"] != r2["items"][0]["id"], "offset did not advance the page"

# /validate — known-good pack passes at both levels.
r = client.post(f"{BASE}/validate", json={"ids": [by_name["minimal.feedpak"]], "strict": True}).json()
assert r["total"] == 1 and r["passed"] == 1 and r["results"][0]["ok"], r
assert r["results"][0]["level"] == "strict", r

# Trust boundary — a forged id is rejected, not resolved to any path.
r = client.post(f"{BASE}/validate", json={"ids": ["deadbeefdeadbeef"], "strict": False}).json()
assert r["passed"] == 0 and not r["results"][0]["ok"], r
assert "unknown" in r["results"][0]["errors"][0], r

# Bad body shape.
assert client.post(f"{BASE}/validate", json={"ids": "nope"}).status_code == 400

# Batch cap — a library can hold thousands of packs; without this a client
# selecting "all" could turn one request into a multi-minute synchronous scan
# with no job queue behind it. Server rejects an oversized batch outright.
huge = [f"deadbeef{i:08x}" for i in range(routes._MAX_VALIDATE_BATCH + 1)]
r = client.post(f"{BASE}/validate", json={"ids": huge})
assert r.status_code == 400 and "too many" in r.json()["error"], r.text

# /validate-upload — zip the minimal example and validate the bytes.
buf = io.BytesIO()
src = EXAMPLES / "minimal.feedpak"
with zipfile.ZipFile(buf, "w") as zf:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            zf.write(p, p.relative_to(src).as_posix())
buf.seek(0)
r = client.post(f"{BASE}/validate-upload",
                files={"files": ("minimal.feedpak", buf, "application/zip")},
                data={"strict": "true"}).json()
assert r["total"] == 1 and r["passed"] == 1, r
assert r["results"][0]["pack"] == "minimal.feedpak" and r["results"][0]["level"] == "strict", r

# Upload with a wrong extension is refused before touching the validator.
r = client.post(f"{BASE}/validate-upload",
                files={"files": ("notes.txt", io.BytesIO(b"x"), "text/plain")}).json()
assert not r["results"][0]["ok"] and "not a" in r["results"][0]["errors"][0], r

print("ok — packs enumerated, library + upload validation work, forged/bad inputs rejected")
