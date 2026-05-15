#!/usr/bin/env python3
"""Scrape ISI school reports and detect the phrase 'significant strength' in latest PDFs."""

from __future__ import annotations

import argparse
import csv
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

LISTING_URL = "https://www.isi.net/reports/"
PHRASE = "significant strength"


@dataclass
class SchoolEntry:
    name: str
    url: str


@dataclass
class ReportCandidate:
    report_url: str
    label: str
    parsed_date: Optional[tuple[int, int, int]] = None


class ISIReportAnalyzer:
    def __init__(
        self,
        output_csv: Path,
        reports_root: Path,
        delay_seconds: float = 0.5,
        timeout_seconds: int = 25,
        max_retries: int = 4,
    ) -> None:
        self.output_csv = output_csv
        self.reports_root = reports_root
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "ISI-Report-Analyzer/1.0 (+https://github.com/local/isi-report-analyzer)",
                "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8",
            }
        )

    def _sleep(self) -> None:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

    def fetch_url(self, url: str, stream: bool = False) -> requests.Response:
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout_seconds, stream=stream)
                response.raise_for_status()
                self._sleep()
                return response
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status and 400 <= status < 500 and status != 429:
                    raise
                if attempt == self.max_retries:
                    raise
                backoff = 2 ** (attempt - 1)
                logging.warning(
                    "Request failed for %s on attempt %s/%s: %s. Retrying in %ss",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                backoff = 2 ** (attempt - 1)
                logging.warning(
                    "Request failed for %s on attempt %s/%s: %s. Retrying in %ss",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        raise RuntimeError(f"Unexpected retry loop termination for URL: {url}")

    def collect_school_entries(self, start_url: str = LISTING_URL) -> list[SchoolEntry]:
        schools: list[SchoolEntry] = []
        seen_urls: set[str] = set()
        page_url = start_url
        page_idx = 1

        while page_url:
            logging.info("Fetching listing page %s: %s", page_idx, page_url)
            soup = BeautifulSoup(self.fetch_url(page_url).text, "html.parser")

            page_schools = self._parse_school_list_page(soup, page_url)
            new_items = [s for s in page_schools if s.url not in seen_urls]
            if not new_items:
                logging.info("No new schools found on listing page %s; stopping pagination.", page_idx)
                break

            for school in new_items:
                seen_urls.add(school.url)
            schools.extend(new_items)
            logging.info("Found %s new schools on page %s", len(new_items), page_idx)

            next_page = self._find_next_page(soup, page_url)
            if not next_page:
                break
            page_url = next_page
            page_idx += 1

        logging.info("Collected %s school entries total", len(schools))
        return schools

    def _parse_school_list_page(self, soup: BeautifulSoup, base_url: str) -> list[SchoolEntry]:
        schools: list[SchoolEntry] = []

        # Primary strategy: links that look like explicit "View" actions.
        for a in soup.select("a[href]"):
            text = a.get_text(" ", strip=True)
            href = a.get("href")
            if not href:
                continue

            attrs_blob = " ".join(
                [
                    text,
                    a.get("title", ""),
                    a.get("aria-label", ""),
                    " ".join(a.get("class", [])),
                ]
            ).lower()
            if "view" not in attrs_blob:
                continue

            school_url = self._resolve_url(base_url, href)
            container = a.find_parent(["tr", "article", "li", "div", "section"])
            school_name = self._extract_school_name(container, fallback=text) if container else (text or "Unknown School")
            schools.append(SchoolEntry(name=school_name, url=school_url))

        # Fallback strategy: parse URL patterns if "View" text is absent in markup.
        if not schools:
            logging.info("No explicit 'View' links found; falling back to school URL pattern detection")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                lower_href = href.lower()
                # Typical detail links often include /reports/<school-slug>/ or /school/... .
                if not ("/reports/" in lower_href or "/school/" in lower_href):
                    continue
                if any(token in lower_href for token in ["?p=", "/page/", "/reports/feed", "#"]):
                    continue

                school_url = self._resolve_url(base_url, href)
                # Skip obvious non-detail links.
                if school_url.rstrip("/") == LISTING_URL.rstrip("/"):
                    continue
                label = a.get_text(" ", strip=True)
                if not label:
                    label = Path(urlparse(school_url).path.rstrip("/")).name.replace("-", " ").title() or "Unknown School"
                schools.append(SchoolEntry(name=label, url=school_url))

        # Deduplicate in page order
        deduped: list[SchoolEntry] = []
        seen = set()
        for school in schools:
            if school.url in seen:
                continue
            seen.add(school.url)
            deduped.append(school)

        logging.info("Parsed %s school candidates from listing page", len(deduped))
        return deduped

    @staticmethod
    def _resolve_url(base_url: str, href: str) -> str:
        """Resolve href robustly against ISI pages that sometimes emit path-like relatives."""
        href = (href or "").strip()
        if href.startswith(("http://", "https://")):
            return href

        parsed_base = urlparse(base_url)
        site_root = urlunparse((parsed_base.scheme, parsed_base.netloc, "/", "", "", ""))

        if href.startswith("/"):
            return urljoin(site_root, href)

        if href.startswith("reports/") or href.startswith("school/"):
            return urljoin(site_root, href)

        return urljoin(base_url, href)

    @staticmethod
    def _extract_school_name(container, fallback: str) -> str:
        for selector in ["h1", "h2", "h3", "h4", ".school-name", "strong", "td", "p"]:
            node = container.select_one(selector)
            if not node:
                continue
            txt = node.get_text(" ", strip=True)
            if txt and "view" not in txt.lower():
                return txt
        text = container.get_text(" ", strip=True)
        text = re.sub(r"\bview\b", "", text, flags=re.IGNORECASE).strip(" -:\n\t")
        return text or fallback

    @staticmethod
    def _find_next_page(soup: BeautifulSoup, base_url: str) -> Optional[str]:
        for a in soup.select("a"):
            label = a.get_text(" ", strip=True).lower()
            rel = " ".join(a.get("rel", [])).lower()
            href = a.get("href")
            if not href:
                continue
            if "next" in label or rel == "next" or label in {">", "»"}:
                return ISIReportAnalyzer._resolve_url(base_url, href)
        return None

    def find_latest_report(self, school_url: str) -> Optional[ReportCandidate]:
        soup = BeautifulSoup(self.fetch_url(school_url).text, "html.parser")
        candidates: list[ReportCandidate] = []

        for a in soup.select("a[href]"):
            href = a.get("href", "")
            text = a.get_text(" ", strip=True)
            lower_blob = f"{href} {text}".lower()
            if "report" not in lower_blob and not href.lower().endswith(".pdf"):
                continue

            full_url = self._resolve_url(school_url, href)
            label = text or Path(urlparse(full_url).path).name
            parsed_date = self._extract_date_tuple(label) or self._extract_date_tuple(full_url)
            candidates.append(ReportCandidate(report_url=full_url, label=label, parsed_date=parsed_date))

        if not candidates:
            return None

        dated = [c for c in candidates if c.parsed_date is not None]
        if dated:
            dated.sort(key=lambda c: c.parsed_date, reverse=True)
            return dated[0]

        return candidates[0]

    @staticmethod
    def _extract_date_tuple(value: str) -> Optional[tuple[int, int, int]]:
        patterns = [
            re.compile(r"(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})"),
            re.compile(r"(\d{1,2})[-_/](\d{1,2})[-_/](20\d{2})"),
        ]
        for pattern in patterns:
            match = pattern.search(value)
            if not match:
                continue
            parts = [int(p) for p in match.groups()]
            if len(str(parts[0])) == 4:
                y, m, d = parts
            else:
                m, d, y = parts
            if 1 <= m <= 12 and 1 <= d <= 31:
                return y, m, d
        return None

    def download_report(self, school_slug: str, report_url: str) -> Path:
        school_dir = self.reports_root / school_slug
        school_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(urlparse(report_url).path).name or "report.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        output_path = school_dir / filename

        response = self.fetch_url(report_url, stream=True)
        with output_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        logging.info("Saved report %s", output_path)
        return output_path

    @staticmethod
    def extract_pdf_text(pdf_path: Path) -> str:
        reader = PdfReader(str(pdf_path))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)

    @staticmethod
    def slugify(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
        return slug or "unknown-school"

    def run(self) -> None:
        self.output_csv.parent.mkdir(parents=True, exist_ok=True)
        self.reports_root.mkdir(parents=True, exist_ok=True)

        schools = self.collect_school_entries()

        with self.output_csv.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=[
                    "school_name",
                    "school_url",
                    "report_url",
                    "report_filename",
                    "phrase_found",
                    "match_count",
                ],
            )
            writer.writeheader()

            for school in schools:
                logging.info("Processing school: %s", school.name)
                try:
                    latest = self.find_latest_report(school.url)
                    if not latest:
                        logging.warning("No report found for school: %s (%s)", school.name, school.url)
                        writer.writerow(
                            {
                                "school_name": school.name,
                                "school_url": school.url,
                                "report_url": "",
                                "report_filename": "",
                                "phrase_found": False,
                                "match_count": 0,
                            }
                        )
                        continue

                    school_slug = self.slugify(school.name)
                    pdf_path = self.download_report(school_slug, latest.report_url)
                    text = self.extract_pdf_text(pdf_path)
                    matches = re.findall(re.escape(PHRASE), text, flags=re.IGNORECASE)
                    writer.writerow(
                        {
                            "school_name": school.name,
                            "school_url": school.url,
                            "report_url": latest.report_url,
                            "report_filename": pdf_path.name,
                            "phrase_found": bool(matches),
                            "match_count": len(matches),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logging.error("Failed processing %s (%s): %s", school.name, school.url, exc)
                    writer.writerow(
                        {
                            "school_name": school.name,
                            "school_url": school.url,
                            "report_url": "",
                            "report_filename": "",
                            "phrase_found": False,
                            "match_count": 0,
                        }
                    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-csv", type=Path, default=Path("output/significant_strength_results.csv"))
    parser.add_argument("--reports-root", type=Path, default=Path("data/reports"))
    parser.add_argument("--delay", type=float, default=0.5, help="Delay in seconds between requests")
    parser.add_argument("--timeout", type=int, default=25, help="Per-request timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=4, help="Retry attempts for transient failures")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    analyzer = ISIReportAnalyzer(
        output_csv=args.output_csv,
        reports_root=args.reports_root,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
    )
    analyzer.run()
    logging.info("Done. Results written to %s", args.output_csv)


if __name__ == "__main__":
    main()
