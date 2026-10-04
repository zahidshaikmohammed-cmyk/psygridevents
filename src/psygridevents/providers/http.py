"""Shared async HTTP client for public sources: conditional GET, size caps, robots.txt."""
from __future__ import annotations

import asyncio
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .base import RobotsDisallowed, SourceError


@dataclass
class HttpResponse:
    url: str
    status: int
    content: bytes
    headers: dict[str, str]
    not_modified: bool
    latency_ms: float


class SourceHttpClient:
    """One pooled httpx.AsyncClient for every source.

    - Identifies itself honestly with a configurable User-Agent.
    - Honours robots.txt per host (cached), so an operator never has to wonder
      whether the engine respects a site's access policy.
    - Uses ETag/Last-Modified conditional requests to avoid re-downloading
      unchanged feeds.
    - Caps response size so one misbehaving source cannot exhaust memory.
    - Never sends cookies/credentials to public sources and never attempts to
      solve challenges: a 401/403/429/challenge page is simply a failure.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 15.0,
        max_bytes: int = 5_000_000,
        respect_robots: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._validators: dict[str, tuple[str | None, str | None]] = {}
        self._robots: dict[str, tuple[float, str, urllib.robotparser.RobotFileParser | None]] = {}
        self._robots_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _robots_for(self, url: str) -> tuple[str, urllib.robotparser.RobotFileParser | None]:
        """RFC 9309 semantics.

        200      -> parse and obey the rules (cached 24h)
        4xx      -> "unavailable": no restrictions apply (cached 24h)
        5xx/network error -> "unreachable": complete disallow for now (cached 10 min,
                    surfaced as a transient source error, not a permanent block)
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_lock:
            cached = self._robots.get(origin)
            if cached and time.monotonic() < cached[0]:
                return cached[1], cached[2]
            try:
                response = await self._client.get(f"{origin}/robots.txt")
            except httpx.HTTPError:
                self._robots[origin] = (time.monotonic() + 600, "unreachable", None)
                return "unreachable", None
            if response.status_code == 200 and len(response.content) < 500_000:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
                result = ("rules", parser)
                ttl = 24 * 3600
            elif 400 <= response.status_code < 500:
                result, ttl = ("unavailable", None), 24 * 3600
            else:
                result, ttl = ("unreachable", None), 600
            self._robots[origin] = (time.monotonic() + ttl, *result)
            return result

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        state, parser = await self._robots_for(url)
        if state == "unreachable":
            raise SourceError(f"robots.txt for {url} is unreachable; not fetching until it can be read")
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def get(self, url: str, *, params: dict | None = None, conditional: bool = True) -> HttpResponse:
        if not await self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url} for this user agent")
        headers: dict[str, str] = {}
        key = url if not params else f"{url}?{sorted(params.items())}"
        if conditional and key in self._validators:
            etag, modified = self._validators[key]
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
        started = time.monotonic()
        try:
            async with self._client.stream("GET", url, params=params, headers=headers) as response:
                if response.status_code == 304:
                    return HttpResponse(str(response.url), 304, b"", dict(response.headers), True,
                                        (time.monotonic() - started) * 1000)
                if response.status_code != 200:
                    raise SourceError(f"HTTP {response.status_code} from {url}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SourceError(f"response from {url} exceeds {self.max_bytes} bytes")
                    chunks.append(chunk)
                content = b"".join(chunks)
                response_headers = dict(response.headers)
                final_url = str(response.url)
        except httpx.HTTPError as exc:
            raise SourceError(f"{type(exc).__name__}: {exc}") from exc
        if conditional:
            self._validators[key] = (response_headers.get("etag"), response_headers.get("last-modified"))
        return HttpResponse(final_url, 200, content, response_headers, False, (time.monotonic() - started) * 1000)
