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
    url: str = Field(
        ..., 
        description="L'URL publique à scraper. Doit commencer par http:// ou https://.",
        examples=["https://example.com", "https://fr.wikipedia.org/wiki/Intelligence_artificielle"],
        max_length=2048
    )
    format: Literal["markdown", "json"] = Field(
        "markdown", 
        description="Format de sortie. 'markdown' est idéal pour la lecture, 'json' pour l'extraction de données structurées."
    )
    render_js: bool = Field(
        True, 
        description="True pour les sites modernes (React, Vue) nécessitant JavaScript. False pour les sites HTML statiques (réponse plus rapide)."
    )
    include_links: bool = Field(
        False, 
        description="Conserver les hyperliens dans la sortie Markdown."
    )
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = Field(
        "domcontentloaded",
        description="Condition d'attente du chargement de la page."
    )
    timeout: int = Field(
        30, ge=5, le=60, 
        description="Délai d'attente maximum en secondes (entre 5 et 60)."
    )
    bypass_cache: bool = Field(
        False, 
        description="Forcer le scraping même si la page est dans le cache."
    )


class ScrapeResponse(BaseModel):
    title: Optional[str] = None
    content: str
    data: Optional[dict[str, Any]] = None

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


@app.post(
    "/scrape_web_page_to_markdown", 
    response_model=ScrapeResponse, 
    response_model_exclude_none=True,
    operation_id="scrape_web_page_to_markdown",
    summary="Extract clean text from a public URL",
    description="Extracts clean, LLM-ready Markdown from any public URL. Use this when an agent needs to read a webpage's content, including JavaScript-rendered pages.",
    openapi_extra={
        "x-mcp-annotations": {
            "readOnlyHint": True,
            "openWorldHint": True,
            "destructiveHint": False
        }
    }
)
async def scrape_web_page_to_markdown(req: ScrapeRequest):
    return await scraper.scrape(
        url=req.url,
        fmt=req.format,
        render_js=req.render_js,
        include_links=req.include_links,
        wait_until=req.wait_until,
        timeout=req.timeout,
        bypass_cache=req.bypass_cache,
    )