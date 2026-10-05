"""
Where things are kept, so a restart or a new release loses nothing.

A container's disk is wiped whenever it is replaced, so two things are mirrored to Azure Blob
Storage when JUSTIFY_BLOB_URL names a container:

* every finished scan's result — written once, as `results/<id>.json.gz`;
* the database — a consistent snapshot, `db/justify.sqlite3.gz`, a few seconds after it changes,
  and once more when the process is asked to stop.

There is no storage key anywhere. The container app's managed identity asks Azure for a token
(the same way the Jury reaches AI Foundry), and the storage account itself refuses keys. Without
JUSTIFY_BLOB_URL everything stays on local disk — which is what tests and a laptop want.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate

from ..llm import _ssl_context

API_VERSION = "2023-11-03"


class BlobError(Exception):
    pass


class ManagedToken:
    """An Entra access token for one resource, from the managed identity when running in Azure,
    or from `az login` on a developer's machine. Refreshed five minutes before it expires."""

    def __init__(self, resource: str = "https://storage.azure.com/"):
        self.resource = resource
        self.value, self.expires, self.lock = "", 0.0, threading.Lock()

    def get(self) -> str:
        with self.lock:
            if self.value and time.time() < self.expires - 300:
                return self.value
            endpoint, secret = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
            if endpoint and secret:
                q = urllib.parse.urlencode({"resource": self.resource, "api-version": "2019-08-01",
                                            **({"client_id": os.environ["AZURE_CLIENT_ID"]}
                                               if os.environ.get("AZURE_CLIENT_ID") else {})})
                req = urllib.request.Request(f"{endpoint}?{q}", headers={"X-IDENTITY-HEADER": secret})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read())
                self.value, self.expires = data["access_token"], float(data.get("expires_on") or time.time() + 3000)
            else:
                out = subprocess.run(["az", "account", "get-access-token", "--resource", self.resource,
                                      "--query", "[accessToken,expires_on]", "-o", "tsv"],
                                     capture_output=True, text=True, errors="replace", timeout=60)
                if out.returncode != 0:
                    raise BlobError(f"no managed identity and no `az login` for {self.resource}")
                token, expires = (out.stdout.split() + ["0"])[:2]
                self.value, self.expires = token, float(expires) if expires.isdigit() else time.time() + 1800
            return self.value


class BlobStore:
    def __init__(self, container_url: str):
        self.base = container_url.rstrip("/")
        self.token = ManagedToken("https://storage.azure.com/")

    def _call(self, method: str, name: str, body: bytes | None = None, headers: dict | None = None):
        h = {"Authorization": f"Bearer {self.token.get()}", "x-ms-version": API_VERSION,
             "x-ms-date": formatdate(usegmt=True), **(headers or {})}
        if body is not None:
            h["Content-Length"] = str(len(body))
        req = urllib.request.Request(f"{self.base}/{urllib.parse.quote(name)}", data=body, method=method, headers=h)
        try:
            return urllib.request.urlopen(req, timeout=60, context=_ssl_context())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise BlobError(f"{method} {name}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BlobError(f"{method} {name}: {exc}") from None

    def get(self, name: str) -> bytes | None:
        r = self._call("GET", name)
        if r is None:
            return None
        with r:
            return r.read()

    def meta(self, name: str) -> dict | None:
        r = self._call("HEAD", name)
        if r is None:
            return None
        with r:
            return {k[len("x-ms-meta-"):]: v for k, v in r.headers.items() if k.lower().startswith("x-ms-meta-")}

    def put(self, name: str, data: bytes, meta: dict | None = None) -> None:
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip"}
        headers.update({f"x-ms-meta-{k}": str(v) for k, v in (meta or {}).items()})
        r = self._call("PUT", name, data, headers)
        if r is not None:
            r.close()

    def delete(self, name: str) -> None:
        r = self._call("DELETE", name)
        if r is not None:
            r.close()


def blob_from_env() -> BlobStore | None:
    url = (os.environ.get("JUSTIFY_BLOB_URL") or "").strip()
    return BlobStore(url) if url.startswith("https://") else None


class Results:
    """Finished scan results: compressed files on local disk, mirrored to Blob Storage."""

    def __init__(self, directory: str, blob: BlobStore | None = None):
        self.dir = directory
        self.blob = blob
        os.makedirs(directory, exist_ok=True)

    def _path(self, scan_id: str) -> str:
        if not scan_id.startswith("s_") or not scan_id[2:].isalnum():
            raise ValueError("bad scan id")
        return os.path.join(self.dir, f"{scan_id}.json.gz")

    def put(self, scan_id: str, result: dict) -> None:
        data = gzip.compress(json.dumps(result, separators=(",", ":")).encode(), compresslevel=6)
        tmp = self._path(scan_id) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, self._path(scan_id))
        if self.blob:
            threading.Thread(target=self._mirror, args=(scan_id, data), daemon=True).start()

    def _mirror(self, scan_id: str, data: bytes) -> None:
        for attempt in range(3):
            try:
                self.blob.put(f"results/{scan_id}.json.gz", data)
                return
            except BlobError as exc:
                if attempt == 2:
                    print(json.dumps({"event": "blob_error", "op": "result", "id": scan_id, "error": str(exc)}),
                          flush=True)
                time.sleep(2 * (attempt + 1))

    def get(self, scan_id: str) -> dict | None:
        path = self._path(scan_id)
        if not os.path.exists(path) and self.blob:
            try:
                data = self.blob.get(f"results/{scan_id}.json.gz")
            except BlobError:
                data = None
            if data:
                with open(path + ".tmp", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".tmp", path)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _sealed_path(self, scan_id: str) -> str:
        return self._path(scan_id)[:-len(".json.gz")] + ".sealed"

    def put_sealed(self, scan_id: str, data: bytes) -> None:
        """A private result, already encrypted with a key the server does not keep."""
        tmp = self._sealed_path(scan_id) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, self._sealed_path(scan_id))
        if self.blob:
            threading.Thread(target=self._mirror_sealed, args=(scan_id, data), daemon=True).start()

    def _mirror_sealed(self, scan_id: str, data: bytes) -> None:
        for attempt in range(3):
            try:
                self.blob.put(f"results/{scan_id}.sealed", data)
                return
            except BlobError as exc:
                if attempt == 2:
                    print(json.dumps({"event": "blob_error", "op": "sealed", "id": scan_id, "error": str(exc)}),
                          flush=True)
                time.sleep(2 * (attempt + 1))

    def get_sealed(self, scan_id: str) -> bytes | None:
        path = self._sealed_path(scan_id)
        if not os.path.exists(path) and self.blob:
            try:
                data = self.blob.get(f"results/{scan_id}.sealed")
            except BlobError:
                data = None
            if data:
                with open(path + ".tmp", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".tmp", path)
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def delete(self, scan_id: str) -> None:
        for path in (self._path(scan_id), self._sealed_path(scan_id)):
            try:
                os.remove(path)
            except OSError:
                pass
        if self.blob:
            for name in (f"results/{scan_id}.json.gz", f"results/{scan_id}.sealed"):
                try:
                    self.blob.delete(name)
                except BlobError:
                    pass


class Snapshots:
    """Keeps the database's copy in Blob Storage current.

    On start, a missing local database is restored from the last snapshot. Afterwards a snapshot
    is uploaded within `every` seconds of any change. Each upload is stamped with this process's
    start time; a process that finds a newer stamp on the blob knows a newer release has taken
    over, and stops uploading, so an old replica draining away cannot overwrite the new one."""

    NAME = "db/justify.sqlite3.gz"

    def __init__(self, blob: BlobStore, db_path: str, every: float = 10.0):
        self.blob, self.db_path, self.every = blob, db_path, every
        self.born = f"{time.time():.3f}"
        self.last_seen: tuple = ()
        self.stopped = False
        self.lock = threading.Lock()

    def restore(self) -> bool:
        if os.path.exists(self.db_path):
            return False
        try:
            data = self.blob.get(self.NAME)
        except BlobError as exc:
            print(json.dumps({"event": "blob_error", "op": "restore", "error": str(exc)}), flush=True)
            return False
        if not data:
            return False
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with open(self.db_path + ".tmp", "wb") as fh:
            fh.write(gzip.decompress(data))
        os.replace(self.db_path + ".tmp", self.db_path)
        print(json.dumps({"event": "db_restored", "bytes": len(data)}), flush=True)
        return True

    def _fingerprint(self) -> tuple:
        out = []
        for suffix in ("", "-wal"):
            try:
                st = os.stat(self.db_path + suffix)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def save(self, database, force: bool = False) -> bool:
        with self.lock:
            if self.stopped:
                return False
            fp = self._fingerprint()
            if not force and fp == self.last_seen:
                return False
            try:
                meta = self.blob.meta(self.NAME) or {}
                if float(meta.get("born") or 0) > float(self.born) + 1:
                    self.stopped = True
                    print(json.dumps({"event": "db_snapshot_handover", "newer": meta.get("born")}), flush=True)
                    return False
                tmpdir = tempfile.mkdtemp(prefix="snap-")
                try:
                    copy = os.path.join(tmpdir, "db.sqlite3")
                    database.snapshot(copy)
                    with open(copy, "rb") as fh:
                        data = gzip.compress(fh.read(), compresslevel=6)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                self.blob.put(self.NAME, data, {"born": self.born, "saved": f"{time.time():.0f}"})
                self.last_seen = fp
                return True
            except BlobError as exc:
                print(json.dumps({"event": "blob_error", "op": "snapshot", "error": str(exc)}), flush=True)
                return False

    def run_forever(self, database) -> None:
        self.last_seen = self._fingerprint()
        while not self.stopped:
            time.sleep(self.every)
            self.save(database)
