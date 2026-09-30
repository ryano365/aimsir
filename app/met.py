"""Thin client for the Met Éireann open data portal backend
(https://api.opendata.met.ie/swagger-ui/index.html)."""
from __future__ import annotations

import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger(__name__)


class MetError(RuntimeError):
    pass


def _items(payload) -> list[dict]:
    """The listing is documented as NearRealtimeModel {name,size,timestamp};
    accept it bare, or wrapped in a list/object."""
    if isinstance(payload, dict):
        for k in ("files", "data", "items", "content", "results"):
            if isinstance(payload.get(k), list):
                return _items(payload[k])
        if "name" in payload:
            return [payload]
        return []
    if isinstance(payload, list):
        out = []
        for p in payload:
            if isinstance(p, dict) and "name" in p:
                out.append(p)
            elif isinstance(p, str):
                out.append({"name": p})
        return out
    return []


class MetClient:
    def __init__(self):
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=300.0),
            headers={"api-key": settings.api_key, "User-Agent": "aimsir-selfhosted/1.0"},
            follow_redirects=True,
        )

    async def close(self):
        await self.http.aclose()

    FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.000Z")

    async def _list_once(self, url: str, since: datetime, until: datetime, fmt: str) -> httpx.Response:
        return await self.http.get(url, params={"from": since.strftime(fmt), "to": until.strftime(fmt)})

    async def list(self, dataset: str, since: datetime, until: datetime | None = None) -> list[dict]:
        """List near-realtime files. The portal has been seen returning 500s for some
        windows, so on failure we try the other date formats, then smaller time slices."""
        until = until or datetime.now(timezone.utc)
        url = f"{settings.api_base}/near-realtime/{dataset}"
        tried: list[str] = []
        fmts = ([self._fmt] if getattr(self, "_fmt", None) else []) + [f for f in self.FORMATS if f != getattr(self, "_fmt", None)]
        for fmt in fmts:
            r = await self._list_once(url, since, until, fmt)
            if r.status_code in (401, 403):
                raise MetError(f"{dataset} listing refused ({r.status_code}) - check MET_API_KEY")
            if r.status_code == 200:
                self._fmt = fmt
                return _items(r.json())
            tried.append(f"{r.status_code} [{fmt}] {r.text[:120]!r}")
        # Whole window failed: walk it in 20-minute slices and merge what works.
        fmt = getattr(self, "_fmt", None) or self.FORMATS[0]
        items: dict[str, dict] = {}
        ok = 0
        t = since
        while t < until:
            t2 = min(t + timedelta(minutes=20), until)
            r = await self._list_once(url, t, t2, fmt)
            if r.status_code == 200:
                ok += 1
                for it in _items(r.json()):
                    items[it["name"]] = it
            else:
                tried.append(f"{r.status_code} slice {t:%H:%M}-{t2:%H:%M}")
            t = t2
        if ok:
            log.info("%s listing: full window failed, %d slices OK, %d files", dataset, ok, len(items))
            return list(items.values())
        raise MetError(f"{dataset} listing failed on Met's side: " + " | ".join(tried[:5]))

    async def download(self, dataset: str, name: str, dest_dir: Path, max_bytes: int | None = None) -> list[Path]:
        """Download one file; a zip response is unpacked. Returns local paths."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / f".{name}.part"
        url = f"{settings.api_base}/near-realtime/download/{dataset}"
        async with self.http.stream("GET", url, params={"files": name}) as r:
            if r.status_code in (401, 403):
                raise MetError(f"download refused ({r.status_code}) - check MET_API_KEY")
            r.raise_for_status()
            size = 0
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    size += len(chunk)
                    if max_bytes and size > max_bytes:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        raise MetError(f"{name} is larger than the configured limit")
                    f.write(chunk)
        with open(tmp, "rb") as f:
            magic = f.read(4)
        if magic == b"PK\x03\x04":
            out = []
            with zipfile.ZipFile(tmp) as z:
                for m in z.infolist():
                    if m.is_dir():
                        continue
                    target = dest_dir / Path(m.filename).name
                    with z.open(m) as src, open(target, "wb") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                    out.append(target)
            tmp.unlink()
            return out
        final = dest_dir / name
        tmp.replace(final)
        return [final]
