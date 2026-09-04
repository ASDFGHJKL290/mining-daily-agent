"""
mineral-pdf-mcp: NI 43-101 mineral resource PDF extraction MCP server.

Tools:
  - extract_resources(pdf_url) -> parse Indicated / Inferred resource tables
    from an NI 43-101 technical report PDF: tonnage (Mt), grade (g/t Au or % Cu)
    and contained metal (oz or t).

Strategy:
  1. If pdf_url matches a bundled sample report (see data/pdf_samples.json),
     return the pre-extracted ground-truth rows directly - this is the offline
     demo path and is deterministic.
  2. Otherwise download the PDF, extract text with pypdf and run a heuristic
     table scanner that hunts for rows containing tonnage/grade/metal patterns.
  3. If nothing reliable is found, return an empty resource list with a clear
     "needs_human_review" flag - the server NEVER fabricates numbers.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mineral-pdf-mcp")

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "pdf_samples.json")

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_PDF_BYTES = 25_000_000  # 25 MB cap


# ---------------------------------------------------------------------------
# Bundled sample reports (offline demo path)
# ---------------------------------------------------------------------------


def _load_samples() -> list[dict[str, Any]]:
    path = os.path.normpath(SAMPLE_DIR)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("reports", [])
    except (OSError, ValueError):
        return []


def _find_sample(pdf_url: str) -> dict[str, Any] | None:
    for report in _load_samples():
        if report.get("url") == pdf_url:
            return report
        # tolerate a trailing mismatch on query params
        if pdf_url and report.get("url", "").split("?")[0] == pdf_url.split("?")[0]:
            return report
    return None


# ---------------------------------------------------------------------------
# Heuristic table scanner for live PDFs
# ---------------------------------------------------------------------------

# A resource-table row typically looks like:
#   "Indicated    154.3    1.87    9.3"
#   or has column headers: Tonnage (Mt) | Grade (g/t Au) | Contained Metal (Moz)
_NUMBER = r"[-+]?\d{1,4}(?:,\d{3})*(?:\.\d+)?"
_TONNAGE_RE = re.compile(rf"({_NUMBER})\s*(Mt|kt|t)\b", re.IGNORECASE)
_GRADE_AU_RE = re.compile(rf"({_NUMBER})\s*g/t", re.IGNORECASE)
_GRADE_CU_RE = re.compile(rf"({_NUMBER})\s*%", re.IGNORECASE)
_METAL_RE = re.compile(
    rf"({_NUMBER})\s*(Moz|koz|oz|kt|t)\b(?!\s*(g/t|%))", re.IGNORECASE
)


def _normalise_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _classify_row(text: str) -> str | None:
    """Return 'Indicated' | 'Inferred' | 'Measured' | 'Proven' | 'Probable' | None."""
    lowered = text.lower()
    for label in (
        "indicated",
        "inferred",
        "measured",
        "proven",
        "probable",
        "reserves",
    ):
        if re.search(rf"\b{label}\b", lowered):
            return label.capitalize()
    return None


def _scan_pdf_text(pages_text: list[str]) -> list[dict[str, Any]]:
    """Scan plain-text pages for Indicated/Inferred resource rows."""
    resources: list[dict[str, Any]] = []
    for page_text in pages_text:
        # resource tables are usually dense; split into candidate lines
        for raw_line in page_text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            category = _classify_row(line)
            if category not in ("Indicated", "Inferred"):
                continue

            tonnage_match = _TONNAGE_RE.search(line)
            grade_au = _GRADE_AU_RE.search(line)
            grade_cu = _GRADE_CU_RE.search(line)
            metal_match = _METAL_RE.search(line)
            if not (tonnage_match and metal_match):
                continue

            grade: dict[str, float] = {}
            if grade_au:
                grade["g/t_au"] = _normalise_number(grade_au.group(1))
            if grade_cu:
                grade["pct_cu"] = _normalise_number(grade_cu.group(1))

            tonnage_raw = tonnage_match.group(1)
            tonnage_unit = tonnage_match.group(2).lower()
            tonnage = _normalise_number(tonnage_raw)
            if tonnage_unit == "kt":
                tonnage = tonnage / 1000.0  # to Mt

            metal_raw = metal_match.group(1)
            metal_unit = metal_match.group(2).lower()
            metal = _normalise_number(metal_raw)
            if metal_unit == "koz":
                metal = metal / 1000.0  # to Moz
            if metal_unit == "t" and grade.get("g/t_au"):
                # contained metal in tonnes from g/t: Mt * g/t / 31.1035 -> Moz
                metal = metal * 1_000_000 / 31.1035 / 1_000_000  # t -> Moz approx
                metal_unit = "Moz"

            resources.append(
                {
                    "category": category,
                    "tonnage_mt": round(tonnage, 2),
                    "grade": grade,
                    "contained_metal": round(metal, 2),
                    "metal_unit": "Moz" if metal_unit in ("moz",) else metal_unit,
                    "confidence": "heuristic",
                    "raw_line": line[:200],
                }
            )
    return resources


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def extract_resources(pdf_url: str) -> dict[str, Any]:
    """Extract Indicated / Inferred resource estimates from an NI 43-101 PDF.

    Args:
        pdf_url: Direct URL to an NI 43-101 technical report PDF. Bundled sample
            URLs (see list_samples) are served deterministically from local data.

    Returns:
        {"report_url": ..., "resources": [{category, tonnage_mt, grade,
         contained_metal, metal_unit, confidence}], "needs_human_review": bool,
         "notes": "..."}
    """
    # 1. Bundled sample path (offline / deterministic demo)
    sample = _find_sample(pdf_url)
    if sample is not None:
        return {
            "report_url": pdf_url,
            "report_title": sample.get("title", ""),
            "resources": sample.get("resources", []),
            "needs_human_review": False,
            "notes": "来自内置样例报告库（确定性离线演示数据，非实时抓取）。",
        }

    # 2. Live download + heuristic scan
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MiningDailyAgent/1.0)"},
        ) as client:
            response = client.get(pdf_url)
            response.raise_for_status()
            if len(response.content) > MAX_PDF_BYTES:
                raise ValueError("PDF exceeds 25 MB safety cap")

        from pypdf import PdfReader
        from io import BytesIO

        reader = PdfReader(BytesIO(response.content))
        pages_text: list[str] = []
        for page in reader.pages[:300]:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - skip unreadable pages
                continue

        resources = _scan_pdf_text(pages_text)
        return {
            "report_url": pdf_url,
            "report_title": None,
            "resources": resources,
            "needs_human_review": not resources,
            "notes": (
                "Heuristic scan completed over %d pages; %d resource rows found."
                % (len(pages_text), len(resources))
            ),
        }
    except Exception as exc:  # noqa: BLE001 - never crash a tool call
        return {
            "report_url": pdf_url,
            "report_title": None,
            "resources": [],
            "needs_human_review": True,
            "notes": f"Extraction failed ({type(exc).__name__}): {exc}",
        }


@mcp.tool()
def list_samples() -> dict[str, Any]:
    """List bundled NI 43-101 sample report URLs usable offline for demos/tests."""
    return {
        "reports": [
            {
                "title": report.get("title", ""),
                "url": report.get("url", ""),
                "company": report.get("company", ""),
            }
            for report in _load_samples()
        ]
    }


if __name__ == "__main__":
    mcp.run()
