"""
DFTracer documentation search and retrieval service.

Covers dftracer, dftracer-utils, pydftracer, and dfanalyzer documentation
hosted on ReadTheDocs.  Uses the RTD v2 search API as the primary search
mechanism, with fallback to direct page fetching and content extraction.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory


# ---------------------------------------------------------------------------
# Known documentation sources
# ---------------------------------------------------------------------------

DOCS_SOURCES: Dict[str, Dict[str, Any]] = {
    "dftracer": {
        "name": "DFTracer (core)",
        "description": "C/C++/Python tracing library — macros, init/fini, configuration",
        "base": "https://dftracer.readthedocs.io/en/latest",
        "search_api": "https://dftracer.readthedocs.io/_/api/v2/search/",
        "rtd_project": "dftracer",
        "rtd_version": "latest",
        "key_pages": [
            "index.html",
            "quickstart.html",
            "c-api.html",
            "python-api.html",
            "configuration.html",
            "installation.html",
            "environment.html",
        ],
    },
    "dftracer-utils": {
        "name": "DFTracer Utils",
        "description": "CLI tools — reader, split, merge, stats, aggregator, replay, etc.",
        "base": "https://dftracer.readthedocs.io/projects/utils/en/latest",
        "search_api": "https://dftracer.readthedocs.io/projects/utils/_/api/v2/search/",
        "rtd_project": "dftracer-utils",
        "rtd_version": "latest",
        "key_pages": [
            "index.html",
            "cli.html",
            "installation.html",
            "api.html",
        ],
    },
    "pydftracer": {
        "name": "PyDFTracer",
        "description": "Python bindings and decorator API for dftracer",
        "base": "https://dftracer.readthedocs.io/projects/pydftracer/en/latest",
        "search_api": "https://dftracer.readthedocs.io/projects/pydftracer/_/api/v2/search/",
        "rtd_project": "pydftracer",
        "rtd_version": "latest",
        "key_pages": [
            "index.html",
            "api.html",
            "quickstart.html",
        ],
    },
    "dfanalyzer": {
        "name": "DFAnalyzer",
        "description": "Trace analysis engine — DFAnalyzer Python API and query interface",
        "base": "https://dftracer.readthedocs.io/projects/dfanalyzer/en/latest",
        "search_api": "https://dftracer.readthedocs.io/projects/dfanalyzer/_/api/v2/search/",
        "rtd_project": "dfanalyzer",
        "rtd_version": "latest",
        "key_pages": [
            "index.html",
            "api.html",
            "quickstart.html",
        ],
    },
}

_SOURCE_ALIASES: Dict[str, str] = {
    "utils": "dftracer-utils",
    "py": "pydftracer",
    "python": "pydftracer",
    "analyzer": "dfanalyzer",
    "core": "dftracer",
    "main": "dftracer",
}

_USER_AGENT = "dftracer-agents/1.0 (MCP docs tool; +https://github.com/llnl/dftracer)"
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = _TIMEOUT) -> Optional[str]:
    """Fetch URL and return body text, or None on error."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = "utf-8"
            ct = resp.headers.get_content_charset()
            if ct:
                charset = ct
            return resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


# ---------------------------------------------------------------------------
# HTML content extractor
# ---------------------------------------------------------------------------

class _DocParser(HTMLParser):
    """Extract title, headings, paragraphs, and code blocks from an RTD page."""

    # Only non-void elements here — void elements (meta, link) have no closing
    # tag so they would permanently activate skip mode and eat the rest of the page.
    _SKIP_TAGS = frozenset({
        "script", "style", "nav", "footer", "header", "noscript",
    })
    # Void elements we silently ignore (no text content possible anyway)
    _VOID_IGNORE = frozenset({
        "meta", "link", "br", "hr", "img", "input", "area",
        "base", "col", "embed", "param", "source", "track", "wbr",
    })
    _BLOCK_TAGS = frozenset({
        "p", "li", "td", "th", "dt", "dd",
        "h1", "h2", "h3", "h4", "h5", "h6",
    })
    _CODE_TAGS = frozenset({"code", "pre"})
    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self):
        super().__init__()
        self.title: str = ""
        self.sections: List[Dict[str, Any]] = []  # [{heading, content, code_blocks}]
        self._current_heading: str = ""
        self._current_text: List[str] = []
        self._current_code: List[str] = []
        self._in_title = False
        self._in_code = False
        self._in_heading = False
        self._heading_buf: List[str] = []
        self._skip_depth = 0
        self._skip_tag: Optional[str] = None
        self._current_code_buf: List[str] = []

    def handle_starttag(self, tag: str, attrs):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in self._SKIP_TAGS:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag == "title":
            self._in_title = True
        if tag in self._HEADING_TAGS:
            self._in_heading = True
            self._heading_buf = []
        if tag in self._CODE_TAGS:
            self._in_code = True
            self._current_code_buf = []

    def handle_endtag(self, tag: str):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
        if tag in self._CODE_TAGS:
            self._in_code = False
            snippet = "".join(self._current_code_buf).strip()
            if snippet:
                self._current_code.append(snippet)
        if tag in self._HEADING_TAGS:
            self._in_heading = False
            heading_text = "".join(self._heading_buf).strip()
            self._flush_section()
            self._current_heading = heading_text
        if tag in self._BLOCK_TAGS and tag not in self._HEADING_TAGS:
            t = "".join(self._current_text).strip()
            if t:
                self._current_text.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        cleaned = html.unescape(data)
        if self._in_title:
            self.title += cleaned
        elif self._in_heading:
            self._heading_buf.append(cleaned)
        elif self._in_code:
            self._current_code_buf.append(cleaned)
        else:
            self._current_text.append(cleaned)

    def _flush_section(self):
        body = re.sub(r"\n{3,}", "\n\n", "".join(self._current_text)).strip()
        if body or self._current_code:
            self.sections.append({
                "heading": self._current_heading,
                "content": body,
                "code_blocks": list(self._current_code),
            })
        self._current_text = []
        self._current_code = []

    def close(self):
        super().close()
        self._flush_section()


def _parse_page(html_text: str) -> Dict[str, Any]:
    """Return {title, sections} extracted from an HTML doc page."""
    p = _DocParser()
    p.feed(html_text)
    p.close()
    return {"title": p.title.strip(), "sections": p.sections}


# ---------------------------------------------------------------------------
# Scoring / relevance
# ---------------------------------------------------------------------------

def _score_section(section: Dict[str, Any], terms: List[str]) -> float:
    """Score a section by term frequency across heading + content + code."""
    text = " ".join([
        section.get("heading", "") * 3,  # weight headings more
        section.get("content", ""),
        " ".join(section.get("code_blocks", [])),
    ]).lower()
    return sum(text.count(t.lower()) for t in terms)


def _best_sections(
    parsed: Dict[str, Any],
    terms: List[str],
    top_n: int = 3,
    max_chars: int = 800,
) -> List[Dict[str, Any]]:
    """Return top_n most relevant sections, truncated to max_chars each."""
    scored = [
        (s, _score_section(s, terms))
        for s in parsed.get("sections", [])
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    results = []
    for sec, score in scored[:top_n]:
        if score == 0 and results:
            break
        content = sec.get("content", "")[:max_chars]
        if len(sec.get("content", "")) > max_chars:
            content += " …"
        entry: Dict[str, Any] = {"heading": sec.get("heading", ""), "content": content}
        if sec.get("code_blocks"):
            entry["code_example"] = sec["code_blocks"][0][:600]
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# RTD search API
# ---------------------------------------------------------------------------

def _rtd_search(
    source_key: str,
    query: str,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Query the ReadTheDocs v2 search API for a project.

    Returns list of {title, url, highlights} dicts.
    """
    src = DOCS_SOURCES.get(source_key, {})
    search_api = src.get("search_api", "")
    if not search_api:
        return []

    params = urllib.parse.urlencode({
        "q": query,
        "project": src.get("rtd_project", source_key),
        "version": src.get("rtd_version", "latest"),
    })
    url = f"{search_api}?{params}"
    body = _fetch(url)
    if not body:
        return []

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []

    results = []
    for hit in data.get("results", [])[:max_results]:
        blocks = hit.get("blocks", [])
        highlights: List[str] = []
        for blk in blocks[:2]:
            hl = blk.get("highlights", {})
            for field in ("content", "title"):
                for snippet in hl.get(field, [])[:2]:
                    # Strip RTD highlight markers
                    clean = re.sub(r"<[^>]+>", "", snippet).strip()
                    if clean:
                        highlights.append(clean)

        page_url = hit.get("domain", src.get("base", "")) + hit.get("path", "")
        results.append({
            "title": hit.get("title", ""),
            "url": page_url,
            "highlights": highlights,
        })
    return results


# ---------------------------------------------------------------------------
# Page-level fetch + relevant content extraction
# ---------------------------------------------------------------------------

def _fetch_and_extract(
    url: str,
    terms: List[str],
    top_n: int = 3,
) -> Dict[str, Any]:
    """Fetch `url`, parse it, return title + best sections for `terms`."""
    body = _fetch(url)
    if body is None:
        return {"url": url, "error": "could not fetch page"}
    parsed = _parse_page(body)
    sections = _best_sections(parsed, terms, top_n=top_n)
    return {
        "url": url,
        "title": parsed.get("title", ""),
        "sections": sections,
    }


def _resolve_source_keys(source: str) -> List[str]:
    """Turn 'all', a comma-separated list, or a single name into a list of keys."""
    if source.strip().lower() == "all":
        return list(DOCS_SOURCES.keys())
    keys: List[str] = []
    for part in re.split(r"[,\s]+", source.strip()):
        part = part.lower().strip()
        part = _SOURCE_ALIASES.get(part, part)
        if part in DOCS_SOURCES:
            keys.append(part)
    return keys or list(DOCS_SOURCES.keys())


# ---------------------------------------------------------------------------
# MCP service
# ---------------------------------------------------------------------------

class DFTracerDocsService(MCPService):
    """MCP service providing dftracer documentation search and retrieval."""

    def __init__(self) -> None:
        self.docs_subservice = FastMCP("DFTracerDocs")
        _register_docs_tools(self.docs_subservice)

    def execute(self, data: dict) -> Optional[str]:
        return "Use docs_search / docs_fetch_page / docs_list_sources to query dftracer docs."

    @property
    def name(self) -> str:
        return "dftracer-docs"


def _register_docs_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def docs_list_sources() -> str:
        """List all available dftracer documentation sources with their descriptions
        and base URLs.

        Returns JSON with a list of sources, each containing:
          - key:         identifier used in docs_search (e.g. "dftracer-utils")
          - name:        human-readable name
          - description: what this source covers
          - base_url:    root URL of the documentation
        """
        sources = [
            {
                "key": k,
                "name": v["name"],
                "description": v["description"],
                "base_url": v["base"],
            }
            for k, v in DOCS_SOURCES.items()
        ]
        return json.dumps({
            "status": "ok",
            "sources": sources,
            "usage": (
                "Pass any key or comma-separated list as the `source` parameter "
                "to docs_search, or use 'all' to search everywhere."
            ),
        }, indent=2)

    @mcp.tool()
    def docs_search(
        query: str,
        source: str = "all",
        max_results: int = 5,
        fetch_content: bool = True,
    ) -> str:
        """Search dftracer documentation and return relevant sections.

        Uses the ReadTheDocs search API to find matching pages, then fetches
        and extracts the most relevant sections from each page.  Results
        include the page title, URL, search highlights, and (when
        fetch_content=True) the best matching content sections with any code
        examples.

        Args:
            query:         Free-text search query.  Examples:
                             "how to annotate C functions"
                             "dftracer_split cli options"
                             "DFTRACER_C_FUNCTION_START"
                             "python decorator dft_fn"
                             "comp types io mem cpu comm"
            source:        Which documentation to search.  One of:
                             "all"             — all four sources (default)
                             "dftracer"        — core C/C++/Python library
                             "dftracer-utils"  — CLI tools (split, merge, stats…)
                             "pydftracer"      — Python bindings
                             "dfanalyzer"      — trace analysis API
                           Aliases: "utils", "py", "python", "analyzer", "core"
                           Multiple sources: "dftracer,dftracer-utils"
            max_results:   Maximum number of pages to return per source (default 5).
            fetch_content: If True (default), fetch each matching page and extract
                           the best matching sections.  Set to False to get only
                           titles and URLs (faster when you just need links).

        Returns:
            JSON with:
              - query:    the search query used
              - sources:  list of source keys searched
              - results:  per-source list of matching pages, each with:
                  - title, url, highlights (from RTD search)
                  - sections[]: [{heading, content, code_example?}] (when fetch_content=True)
              - total_pages_found: count across all sources
        """
        terms = re.split(r"\W+", query.lower())
        terms = [t for t in terms if len(t) > 2]

        source_keys = _resolve_source_keys(source)
        all_results: Dict[str, Any] = {}
        total = 0

        for key in source_keys:
            hits = _rtd_search(key, query, max_results=max_results)

            if not hits:
                # RTD API failed or returned nothing — try key pages directly
                src = DOCS_SOURCES[key]
                hits = []
                for page in src.get("key_pages", []):
                    page_url = f"{src['base']}/{page}"
                    hits.append({"title": page, "url": page_url, "highlights": []})
                hits = hits[:max_results]

            pages = []
            for hit in hits:
                entry: Dict[str, Any] = {
                    "title": hit["title"],
                    "url": hit["url"],
                    "highlights": hit.get("highlights", []),
                }
                if fetch_content and hit.get("url"):
                    extracted = _fetch_and_extract(hit["url"], terms, top_n=3)
                    if "error" not in extracted:
                        entry["page_title"] = extracted["title"]
                        entry["sections"] = extracted["sections"]
                pages.append(entry)

            if pages:
                all_results[key] = {
                    "source_name": DOCS_SOURCES[key]["name"],
                    "pages": pages,
                }
                total += len(pages)

        return json.dumps({
            "status": "ok",
            "query": query,
            "sources_searched": source_keys,
            "total_pages_found": total,
            "results": all_results,
        }, indent=2)

    @mcp.tool()
    def docs_fetch_page(
        url: str,
        query: Optional[str] = None,
        top_sections: int = 5,
    ) -> str:
        """Fetch a specific dftracer documentation page and return its content.

        Retrieves the page at `url`, parses the HTML, and returns the title
        and content sections.  When `query` is provided, returns only the most
        relevant sections (scored by term frequency); otherwise returns all
        sections in order.

        Args:
            url:          Full URL of the page to fetch.  Examples:
                            "https://dftracer.readthedocs.io/en/latest/c-api.html"
                            "https://dftracer.readthedocs.io/projects/utils/en/latest/cli.html"
            query:        Optional search query to filter/rank sections by
                          relevance.  If omitted, all sections are returned.
            top_sections: Maximum number of sections to return (default 5).

        Returns:
            JSON with:
              - url:      the fetched URL
              - title:    page title
              - sections: list of {heading, content, code_example?}
        """
        body = _fetch(url)
        if body is None:
            return json.dumps({
                "status": "error",
                "message": f"Could not fetch page: {url}",
                "url": url,
            }, indent=2)

        parsed = _parse_page(body)

        if query:
            terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
            sections = _best_sections(parsed, terms, top_n=top_sections, max_chars=1200)
        else:
            sections = [
                {
                    "heading": s.get("heading", ""),
                    "content": s.get("content", "")[:1200],
                    **({"code_example": s["code_blocks"][0][:600]}
                       if s.get("code_blocks") else {}),
                }
                for s in parsed.get("sections", [])[:top_sections]
            ]

        return json.dumps({
            "status": "ok",
            "url": url,
            "title": parsed.get("title", ""),
            "sections": sections,
        }, indent=2)

    @mcp.tool()
    def docs_search_key_pages(
        source: str,
        query: Optional[str] = None,
    ) -> str:
        """Fetch and summarise all key pages for a documentation source.

        Useful for a broad overview of what a source covers, or for
        discovering available pages before calling docs_search.

        Args:
            source:  Documentation source key: "dftracer", "dftracer-utils",
                     "pydftracer", or "dfanalyzer".
            query:   Optional query to rank sections by relevance within
                     each page.

        Returns:
            JSON with a list of pages, each containing title, url, and
            a short content summary.
        """
        source = _SOURCE_ALIASES.get(source.lower(), source.lower())
        src = DOCS_SOURCES.get(source)
        if src is None:
            available = ", ".join(DOCS_SOURCES)
            return json.dumps({
                "status": "error",
                "message": f"Unknown source '{source}'. Available: {available}",
            }, indent=2)

        terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
        pages = []
        for page_name in src.get("key_pages", []):
            url = f"{src['base']}/{page_name}"
            body = _fetch(url)
            if body is None:
                pages.append({"url": url, "error": "could not fetch"})
                continue
            parsed = _parse_page(body)
            if terms:
                sections = _best_sections(parsed, terms, top_n=2, max_chars=600)
            else:
                # Just first section as a summary
                sections = [
                    {"heading": s.get("heading", ""), "content": s.get("content", "")[:400]}
                    for s in parsed.get("sections", [])[:2]
                ]
            pages.append({
                "url": url,
                "title": parsed.get("title", page_name),
                "sections": sections,
            })

        return json.dumps({
            "status": "ok",
            "source": source,
            "source_name": src["name"],
            "base_url": src["base"],
            "pages": pages,
        }, indent=2)


MCPServiceFactory.register("dftracer-docs", DFTracerDocsService())
