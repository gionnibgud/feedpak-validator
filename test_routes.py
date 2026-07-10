#!/usr/bin/env python3
"""Route-level self-check for the feedback-validator plugin backend.

Wires routes.setup() with a fake host `context` pointed at the vendored example
packs, then drives the three endpoints through Starlette's TestClient. Asserts
the happy paths AND the trust boundary (a forged/unknown id is rejected, never
validated). Run: python test_routes.py  (needs fastapi + httpx installed).
"""
import io
import logging
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
    return {
        "log": logging.getLogger("test"),
        "load_sibling": _load_sibling,
        "get_dlc_dir": lambda: str(EXAMPLES),      # examples/ has no sloppak/ subdir
        "get_sloppak_cache_dir": lambda: str(EXAMPLES),
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
