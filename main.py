"""FastAPI app exposing the scraping microservice."""
import logging
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scraper import ScrapeError, Scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
scraper = Scraper()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scraper.start()
    yield
    await scraper.stop()


app = FastAPI(
    title="Web Scraper API",
    description="Scrape any public URL into clean Markdown or structured JSON. JS-rendered pages supported.",
    version="1.0.0",
    lifespan=lifespan,
)


class ScrapeRequest(BaseModel):
    url: str = Field(..., examples=["https://example.com"], max_length=2048)
    format: Literal["markdown", "json"] = "markdown"
    render_js: bool = Field(True, description="Use headless Chromium (stealth). False = fast plain HTTP fetch.")
    include_links: bool = Field(False, description="Keep hyperlinks in Markdown output.")
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    timeout: int = Field(30, ge=5, le=60, description="Seconds.")
    bypass_cache: bool = False


class ScrapeResponse(BaseModel):
    url: str
    final_url: str
    status_code: int
    format: str
    rendered: bool
    title: Optional[str] = None
    content: str
    data: Optional[dict[str, Any]] = None
    cached: bool
    elapsed_ms: int


@app.exception_handler(ScrapeError)
async def scrape_error_handler(_: Request, exc: ScrapeError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception):
    logging.getLogger("api").exception("Unhandled error")
    return JSONResponse(status_code=500, content={"error": "Internal error"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse, response_model_exclude_none=True)
async def scrape(req: ScrapeRequest):
    return await scraper.scrape(
        url=req.url,
        fmt=req.format,
        render_js=req.render_js,
        include_links=req.include_links,
        wait_until=req.wait_until,
        timeout=req.timeout,
        bypass_cache=req.bypass_cache,
    )