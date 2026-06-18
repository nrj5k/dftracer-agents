"""Academic Papers MCP service — arXiv and Semantic Scholar search.

Exposes tools for fetching and searching academic papers so that AI agents
can find relevant literature for I/O performance optimizations identified by
the dftracer pipeline.

Tools
-----
* ``search_arxiv``              — keyword search on arXiv
* ``get_arxiv_paper``           — fetch one arXiv paper by ID
* ``search_semantic_scholar``   — keyword search on Semantic Scholar
* ``get_semantic_scholar_paper``— fetch one S2 paper by ID
* ``get_author_papers``         — retrieve an author's paper list from S2
* ``search_papers_combined``    — parallel search on both sources at once
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Optional

import httpx
from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory

ARXIV_BASE = "https://export.arxiv.org/api/query"
S2_BASE    = "https://api.semanticscholar.org/graph/v1"

S2_PAPER_FIELDS  = (
    "title,authors,year,abstract,citationCount,referenceCount,"
    "url,externalIds,publicationDate,journal,openAccessPdf"
)
S2_AUTHOR_FIELDS = "name,affiliations,paperCount,citationCount,hIndex"


# ── Module-level helpers (pure, reusable by session tools) ────────────────────

def _parse_arxiv_entry(entry: ET.Element, ns: dict) -> dict:
    def txt(tag):
        el = entry.find(tag, ns)
        return el.text.strip() if el is not None and el.text else ""

    authors = [
        a.find("atom:name", ns).text.strip()
        for a in entry.findall("atom:author", ns)
        if a.find("atom:name", ns) is not None
    ]
    categories = [
        c.attrib.get("term", "")
        for c in entry.findall("arxiv:primary_category", ns)
        + entry.findall("atom:category", ns)
    ]
    arxiv_id = txt("atom:id").split("/abs/")[-1]
    return {
        "id":         arxiv_id,
        "title":      txt("atom:title").replace("\n", " "),
        "authors":    authors,
        "abstract":   txt("atom:summary").replace("\n", " "),
        "published":  txt("atom:published")[:10],
        "updated":    txt("atom:updated")[:10],
        "categories": list(dict.fromkeys(categories)),
        "pdf_url":    f"https://arxiv.org/pdf/{arxiv_id}",
        "abs_url":    f"https://arxiv.org/abs/{arxiv_id}",
        "source":     "arXiv",
    }


def _fmt_paper(p: dict) -> str:
    lines = [
        f"**{p.get('title', 'Untitled')}**",
        f"Authors: {', '.join(p.get('authors', [])) or 'N/A'}",
        f"Year/Date: {p.get('year') or p.get('published', 'N/A')}",
    ]
    if p.get("journal"):
        lines.append(f"Journal: {p['journal']}")
    if p.get("citationCount") is not None:
        lines.append(f"Citations: {p['citationCount']}")
    if p.get("abstract"):
        abstract = p["abstract"]
        lines.append(f"Abstract: {abstract[:300]}{'…' if len(abstract) > 300 else ''}")
    for key in ("pdf_url", "abs_url", "url"):
        if p.get(key):
            lines.append(f"URL: {p[key]}")
            break
    lines.append(f"Source: {p.get('source', 'N/A')}")
    return "\n".join(lines)


async def _arxiv_search(query: str, max_results: int = 5, sort_by: str = "relevance",
                        category: Optional[str] = None) -> list[dict]:
    """Low-level arXiv search; returns list of paper dicts."""
    sort_by = {"relevance": "relevance", "lastUpdatedDate": "lastUpdatedDate",
                "submittedDate": "submittedDate"}.get(sort_by, "relevance")
    search_query = f"all:{query}"
    if category:
        search_query += f" AND cat:{category}"
    params = {"search_query": search_query, "max_results": max_results,
              "sortBy": sort_by, "sortOrder": "descending"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ARXIV_BASE, params=params)
        resp.raise_for_status()
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(resp.text)
    return [_parse_arxiv_entry(e, ns) for e in root.findall("atom:entry", ns)]


# ── Service class ─────────────────────────────────────────────────────────────

class AcademicPapersService(MCPService):
    """MCP service for fetching academic papers from arXiv and Semantic Scholar.

    Attributes:
        papers_subservice (FastMCP): Sub-server named ``"AcademicPapers"``
            hosting all search and fetch tools.
    """

    def __init__(self) -> None:
        self.papers_subservice = FastMCP("AcademicPapers")
        self._register_tools()

    def _register_tools(self) -> None:  # noqa: C901

        @self.papers_subservice.tool()
        async def search_arxiv(
            query: str,
            max_results: int = 5,
            sort_by: str = "relevance",
            category: Optional[str] = None,
        ) -> str:
            """Search arXiv for academic papers.

            Args:
                query: Search query string (e.g. "parallel I/O optimization HDF5").
                max_results: Number of results to return (1-25, default 5).
                sort_by: Sort order — "relevance", "lastUpdatedDate", or "submittedDate".
                category: Optional arXiv category filter (e.g. "cs.DC", "cs.PF").

            Returns:
                Formatted list of matching papers with abstracts and URLs.
            """
            max_results = max(1, min(25, max_results))
            papers = await _arxiv_search(query, max_results, sort_by, category)
            if not papers:
                return f"No arXiv papers found for query: '{query}'"
            output = [f"arXiv search results for '{query}' ({len(papers)} papers):\n"]
            for i, paper in enumerate(papers, 1):
                output.append(f"[{i}] {_fmt_paper(paper)}\n")
            return "\n".join(output)

        @self.papers_subservice.tool()
        async def get_arxiv_paper(arxiv_id: str) -> str:
            """Fetch a single arXiv paper by its ID.

            Args:
                arxiv_id: The arXiv paper ID (e.g. "2310.06825" or "cs/0301001").

            Returns:
                Full details of the paper including abstract and PDF link.
            """
            arxiv_id = arxiv_id.strip().lstrip("arxiv:").lstrip("arXiv:")
            params = {"id_list": arxiv_id, "max_results": 1}
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(ARXIV_BASE, params=params)
                resp.raise_for_status()
            ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
            root = ET.fromstring(resp.text)
            entries = root.findall("atom:entry", ns)
            if not entries:
                return f"No arXiv paper found with ID: {arxiv_id}"
            return f"arXiv Paper Details:\n\n{_fmt_paper(_parse_arxiv_entry(entries[0], ns))}"

        @self.papers_subservice.tool()
        async def search_semantic_scholar(
            query: str,
            max_results: int = 5,
            year_range: Optional[str] = None,
            fields_of_study: Optional[str] = None,
            min_citations: Optional[int] = None,
        ) -> str:
            """Search Semantic Scholar for academic papers.

            Args:
                query: Search query string.
                max_results: Number of results (1-20, default 5).
                year_range: Optional year filter like "2020-2024" or "2023".
                fields_of_study: Comma-separated fields, e.g. "Computer Science".
                min_citations: Optional minimum citation count filter.

            Returns:
                Formatted list of matching papers.
            """
            max_results = max(1, min(20, max_results))
            params: dict = {"query": query, "limit": max_results, "fields": S2_PAPER_FIELDS}
            if year_range:
                params["year"] = year_range
            if fields_of_study:
                params["fieldsOfStudy"] = fields_of_study

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{S2_BASE}/paper/search", params=params)
                resp.raise_for_status()
                data = resp.json()

            papers = data.get("data", [])
            if min_citations is not None:
                papers = [p for p in papers if (p.get("citationCount") or 0) >= min_citations]
            if not papers:
                return f"No Semantic Scholar papers found for query: '{query}'"

            def _s2_common(p):
                pdf_url = p.get("openAccessPdf", {}).get("url") if p.get("openAccessPdf") else None
                return {
                    "title": p.get("title", ""),
                    "authors": [a.get("name", "") for a in p.get("authors", [])],
                    "year": p.get("year"),
                    "abstract": p.get("abstract", ""),
                    "citationCount": p.get("citationCount"),
                    "url": p.get("url", ""),
                    "pdf_url": pdf_url,
                    "journal": p.get("journal", {}).get("name") if p.get("journal") else None,
                    "source": "Semantic Scholar",
                }

            output = [f"Semantic Scholar results for '{query}' ({len(papers)} papers):\n"]
            for i, paper in enumerate(papers, 1):
                output.append(f"[{i}] {_fmt_paper(_s2_common(paper))}\n")
            return "\n".join(output)

        @self.papers_subservice.tool()
        async def get_semantic_scholar_paper(paper_id: str) -> str:
            """Fetch full details for a paper from Semantic Scholar.

            Args:
                paper_id: Semantic Scholar ID, DOI, or arXiv ID prefixed with "arXiv:"
                    (e.g. "arXiv:2310.06825" or "DOI:10.18653/v1/2020.acl-main.196").

            Returns:
                Detailed paper information including references.
            """
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{S2_BASE}/paper/{paper_id}",
                    params={"fields": S2_PAPER_FIELDS + ",references,citations"},
                )
                if resp.status_code == 404:
                    return f"Paper not found: {paper_id}"
                resp.raise_for_status()
                p = resp.json()

            authors  = [a.get("name", "") for a in p.get("authors", [])]
            pdf_url  = p.get("openAccessPdf", {}).get("url") if p.get("openAccessPdf") else None
            lines = [
                f"**{p.get('title', 'Untitled')}**",
                f"Authors: {', '.join(authors) or 'N/A'}",
                f"Year: {p.get('year', 'N/A')}",
                f"Citations: {p.get('citationCount', 'N/A')}",
                f"References: {p.get('referenceCount', 'N/A')}",
            ]
            if p.get("journal"):
                lines.append(f"Journal: {p['journal'].get('name', '')}")
            if p.get("abstract"):
                lines.append(f"\nAbstract:\n{p['abstract']}")
            if pdf_url:
                lines.append(f"\nPDF: {pdf_url}")
            if p.get("url"):
                lines.append(f"Semantic Scholar URL: {p['url']}")
            refs = p.get("references", [])
            if refs:
                lines.append(f"\nTop References ({min(5, len(refs))} of {len(refs)}):")
                for r in refs[:5]:
                    rp = r.get("citedPaper", {})
                    lines.append(f"  - {rp.get('title', 'N/A')} ({rp.get('year', '')})")
            return "\n".join(lines)

        @self.papers_subservice.tool()
        async def get_author_papers(author_name: str, max_results: int = 5) -> str:
            """Search for an author on Semantic Scholar and retrieve their papers.

            Args:
                author_name: Full name of the author (e.g. "John Bent").
                max_results: Number of papers to return (1-20, default 5).

            Returns:
                Author profile and list of their papers sorted by citation count.
            """
            max_results = max(1, min(20, max_results))
            async with httpx.AsyncClient(timeout=30) as client:
                search_resp = await client.get(
                    f"{S2_BASE}/author/search",
                    params={"query": author_name, "limit": 1, "fields": S2_AUTHOR_FIELDS},
                )
                search_resp.raise_for_status()
                authors = search_resp.json().get("data", [])
                if not authors:
                    return f"No author found for: '{author_name}'"
                author    = authors[0]
                author_id = author["authorId"]
                papers_resp = await client.get(
                    f"{S2_BASE}/author/{author_id}/papers",
                    params={"limit": max_results, "fields": "title,year,citationCount,authors,url",
                            "sort": "citationCount"},
                )
                papers_resp.raise_for_status()
                papers = papers_resp.json().get("data", [])

            lines = [
                f"**Author: {author.get('name', author_name)}**",
                f"Affiliations: {', '.join(author.get('affiliations', [])) or 'N/A'}",
                f"Total papers: {author.get('paperCount', 'N/A')}",
                f"Total citations: {author.get('citationCount', 'N/A')}",
                f"h-index: {author.get('hIndex', 'N/A')}",
                f"\nTop {len(papers)} papers (by citations):",
            ]
            for i, p in enumerate(papers, 1):
                lines.append(
                    f"  [{i}] {p.get('title', 'N/A')} ({p.get('year', '?')}) "
                    f"— {p.get('citationCount', 0)} citations"
                )
            return "\n".join(lines)

        @self.papers_subservice.tool()
        async def search_papers_combined(query: str, max_results_each: int = 3) -> str:
            """Search both arXiv and Semantic Scholar simultaneously.

            Args:
                query: Search query string.
                max_results_each: Papers to fetch from each source (1-10, default 3).

            Returns:
                Combined results from both sources.
            """
            max_results_each = max(1, min(10, max_results_each))
            arxiv_task = search_arxiv(query, max_results=max_results_each)
            s2_task    = search_semantic_scholar(query, max_results=max_results_each)
            arxiv_result, s2_result = await asyncio.gather(
                arxiv_task, s2_task, return_exceptions=True
            )
            output = [f"Combined academic paper search for: '{query}'\n{'='*60}\n"]
            output.append("## arXiv Results")
            output.append(str(arxiv_result) if not isinstance(arxiv_result, Exception)
                          else f"arXiv error: {arxiv_result}")
            output.append("\n## Semantic Scholar Results")
            output.append(str(s2_result) if not isinstance(s2_result, Exception)
                          else f"Semantic Scholar error: {s2_result}")
            return "\n".join(output)

    def execute(self, data: dict) -> str:
        return "Use the search tools to find academic papers on arXiv and Semantic Scholar."

    @property
    def name(self) -> str:
        return "academic_papers"


MCPServiceFactory.register("academic_papers", AcademicPapersService())
