"""Thin client for the Met Éireann open data portal backend
(https://api.opendata.met.ie/swagger-ui/index.html)."""
from __future__ import annotations

import logging
import zipfile
from datetime import datetime, timezone
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

    async def list(self, dataset: str, since: datetime, until: datetime | None = None) -> list[dict]:
        until = until or datetime.now(timezone.utc)
        url = f"{settings.api_base}/near-realtime/{dataset}"
        last = None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.000Z"):
            r = await self.http.get(url, params={"from": since.strftime(fmt), "to": until.strftime(fmt)})
            if r.status_code == 400:
                last = r
                continue
            if r.status_code in (401, 403):
                raise MetError(f"{dataset} listing refused ({r.status_code}) - check MET_API_KEY")
            r.raise_for_status()
            return _items(r.json())
        raise MetError(f"{dataset} listing rejected: {last.status_code if last else '?'} {last.text[:200] if last else ''}")

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
