"""Extraction logic: SSRF guard, fetching (Playwright / httpx), parsing, caching."""
import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Any, Optional, Union
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura
from bs4 import BeautifulSoup
from cachetools import TTLCache
from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

log = logging.getLogger("scraper")

# ----------------------------- Config ---------------------------------------
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "5"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # seconds (1 hour)
CACHE_MAX_MB = int(os.getenv("CACHE_MAX_MB", "100"))
MAX_BYTES = int(os.getenv("MAX_BYTES", str(5 * 1024 * 1024)))
MAX_REDIRECTS = 5
ALLOWED_PORTS = {80, 443, 8080, 8443}
BLOCKED_RESOURCES = {"image", "media", "font"}  # speed: skip heavy assets
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IPAddr = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


# ----------------------------- Errors ---------------------------------------
class ScrapeError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class SSRFError(ScrapeError):
    def __init__(self, message: str):
        super().__init__(message, 403)


# ----------------------------- SSRF protection ------------------------------
def _is_public(ip: IPAddr) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[IPAddr]:
    bare = host.split("%")[0]
    try:
        return [ipaddress.ip_address(bare)]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ScrapeError(f"Cannot resolve host: {host}", 400)
    return list({ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos})


async def validate_url(url: str) -> str:
    """Raise SSRFError/ScrapeError unless URL is http(s) to a public IP."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise ScrapeError("Invalid URL", 400)
    if parts.scheme not in ("http", "https"):
        raise SSRFError("Only http and https URLs are allowed")
    if not parts.hostname:
        raise ScrapeError("URL has no hostname", 400)
    if parts.username or parts.password:
        raise SSRFError("Credentials in URL are not allowed")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise SSRFError(f"Port {port} is not allowed")
    for ip in await _resolve(parts.hostname, port):
        if not _is_public(ip):
            raise SSRFError("Target resolves to a non-public address")
    return parts.geturl()


# ----------------------------- Parsing --------------------------------------
def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer"]):
        t.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def extract_content(html: str, url: str, fmt: str, include_links: bool) -> dict[str, Any]:
    """Blocking (CPU-bound) - call via asyncio.to_thread."""
    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=include_links,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
    )
    if not markdown or len(markdown.strip()) < 50:
        markdown = _clean_text(html)  # BeautifulSoup fallback

    meta = trafilatura.extract_metadata(html, default_url=url)
    soup = BeautifulSoup(html, "lxml")
    title = (meta.title if meta and meta.title else None) or (
        soup.title.get_text(strip=True) if soup.title else None
    )
    result: dict[str, Any] = {"title": title, "content": markdown.strip()}

    if fmt == "json":
        headings = [
            {"level": int(h.name[1]), "text": h.get_text(" ", strip=True)}
            for h in soup.find_all(re.compile(r"^h[1-3]$"))
            if h.get_text(strip=True)
        ][:50]
        links, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            if href.startswith(("http://", "https://")) and href not in seen:
                seen.add(href)
                links.append({"text": a.get_text(" ", strip=True)[:200], "href": href})
            if len(links) >= 200:
                break
        result["data"] = {
            "title": title,
            "description": getattr(meta, "description", None),
            "author": getattr(meta, "author", None),
            "date": getattr(meta, "date", None),
            "sitename": getattr(meta, "sitename", None),
            "language": getattr(meta, "language", None),
            "headings": headings,
            "links": links,
            "text": markdown.strip(),
            "word_count": len(markdown.split()),
        }
    return result


# ----------------------------- Scraper --------------------------------------
class Scraper:
    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        # Cache of RAW fetched pages, keyed by (url, render_js), bounded by bytes.
        self._cache: TTLCache = TTLCache(
            maxsize=CACHE_MAX_MB * 1024 * 1024,
            ttl=CACHE_TTL,
            getsizeof=lambda v: len(v["html"]) + 1024,
        )
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._stealth = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            from playwright_stealth import Stealth

            self._stealth = Stealth()
        except Exception as e:  # stealth is best-effort
            log.warning("playwright-stealth unavailable: %s", e)
        log.info("Browser started")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ---- public API ----
    async def scrape(
        self,
        url: str,
        fmt: str = "markdown",
        render_js: bool = True,
        include_links: bool = False,
        wait_until: str = "domcontentloaded",
        timeout: int = 30,
        bypass_cache: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        url = await validate_url(url)
        key = (url, render_js)

        page = None if bypass_cache else self._cache.get(key)
        cached = page is not None
        if page is None:
            # De-duplicate identical concurrent fetches
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._fetch(url, render_js, wait_until, timeout))
                self._inflight[key] = task
                task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
            page = await asyncio.shield(task)
            if page["status_code"] < 400:  # never cache error pages
                try:
                    self._cache[key] = page
                except ValueError:  # item larger than the whole cache
                    pass

        extracted = await asyncio.to_thread(
            extract_content, page["html"], page["final_url"], fmt, include_links
        )
        return {
            "url": url,
            "final_url": page["final_url"],
            "status_code": page["status_code"],
            "format": fmt,
            "rendered": render_js,
            **extracted,
            "cached": cached,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    # ---- internals ----
    async def _fetch(self, url: str, render_js: bool, wait_until: str, timeout: int) -> dict[str, Any]:
        try:
            if render_js:
                final_url, status, html = await self._fetch_rendered(url, wait_until, timeout)
            else:
                final_url, status, html = await asyncio.wait_for(
                    self._fetch_static(url, timeout), timeout + 5
                )
        except asyncio.TimeoutError:
            raise ScrapeError("Timed out fetching the page", 504)
        return {"final_url": final_url, "status_code": status, "html": html}

    async def _fetch_static(self, url: str, timeout: int) -> tuple[str, int, str]:
        current = url
        # trust_env=False: ignore proxy env vars so DNS validation matches the real target
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                current = await validate_url(current)  # re-validate every hop
                try:
                    async with client.stream("GET", current) as r:
                        if r.is_redirect:
                            loc = r.headers.get("location")
                            if not loc:
                                raise ScrapeError("Redirect without Location", 502)
                            current = urljoin(current, loc)
                            continue
                        ctype = r.headers.get("content-type", "").lower()
                        if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
                            raise ScrapeError(f"Unsupported content type: {ctype}", 415)
                        buf = bytearray()
                        async for chunk in r.aiter_bytes():
                            buf += chunk
                            if len(buf) > MAX_BYTES:
                                break
                        html = bytes(buf).decode(r.encoding or "utf-8", errors="replace")
                        return current, r.status_code, html
                except httpx.HTTPError as e:
                    raise ScrapeError(f"Fetch failed: {type(e).__name__}", 502)
        raise ScrapeError("Too many redirects", 502)

    async def _fetch_rendered(self, url: str, wait_until: str, timeout: int) -> tuple[str, int, str]:
        assert self._browser, "Browser not started"
        blocked = {"hit": False}

        async def guard(route: Route) -> None:
            req = route.request
            u = req.url
            if u.startswith(("data:", "blob:", "about:")):
                return await route.continue_()
            try:
                await validate_url(u)  # covers redirects and sub-requests
            except ScrapeError:
                blocked["hit"] = True
                return await route.abort("blockedbyclient")
            if req.resource_type in BLOCKED_RESOURCES:
                return await route.abort()
            await route.continue_()

        async def block_ws(ws) -> None:
            # Not connecting to the server = socket is mocked, never leaves the browser.
            await ws.close()

        async with self._sem:
            ctx = await self._browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                service_workers="block",
            )
            try:
                if self._stealth:
                    try:
                        await self._stealth.apply_stealth_async(ctx)
                    except Exception as e:
                        log.warning("Stealth failed: %s", e)
                await ctx.route("**/*", guard)
                await ctx.route_web_socket("**/*", block_ws)
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
                    try:  # give SPAs a moment to hydrate, but don't block long
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except PlaywrightTimeout:
                        pass
                    html = await page.content()
                    final_url = page.url
                    status = resp.status if resp else 0
                except PlaywrightTimeout:
                    raise ScrapeError("Page load timed out", 504)
                except PlaywrightError as e:
                    if blocked["hit"]:
                        raise SSRFError("Request blocked by SSRF protection")
                    raise ScrapeError(f"Browser error: {str(e).splitlines()[0][:200]}", 502)
                if blocked["hit"] and not html:
                    raise SSRFError("Request blocked by SSRF protection")
                return final_url, status, html[:MAX_BYTES]
            finally:
                await ctx.close()