"""Scrape SHL's product catalog (Individual Test Solutions only) into JSON.

Usage:
    python scripts/scrape_catalog.py [--out PATH] [--concurrency N]

Strategy:
1. Walk paginated catalog pages: ?start=N&type=1
2. Identify the "Individual Test Solutions" section by its <h4> heading
   (the catalog page also contains "Pre-packaged Job Solutions"; we must
   skip that section).
3. From each row, extract the product name + detail URL + test-type letters
   + Remote-Testing/Adaptive flags.
4. For every detail URL, fetch the page and pull description, job levels,
   languages, assessment length.
5. Stop pagination when a page yields zero new rows for the Individual section.

Run repeatedly is safe (idempotent: writes catalog.json atomically).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

# Allow running as a script without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings
from src.retrieval.catalog import Assessment, save_catalog

LOG = logging.getLogger("scraper")

INDIVIDUAL_TYPE = 1  # ?type=1 paginates Individual Test Solutions table
PAGE_STEP = 12       # SHL paginates 12 rows at a time
TYPE_PARAM = 1       # SHL canonical: type=1 == Individual; type=2 == Pre-packaged

INDIVIDUAL_HEADING = "individual test solutions"
PREPACK_HEADING = "pre-packaged job solutions"

# Defensive limits
MAX_PAGES = 60       # Hard cap (SHL catalog ~32 pages)
REQUEST_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def _extract_section_table(soup: BeautifulSoup, heading: str) -> Tag | None:
    """Return the <table> whose first row contains a <th> matching `heading`.

    SHL renders both 'Pre-packaged Job Solutions' and 'Individual Test
    Solutions' as <table>s where the first <tr>'s first <th> has the section
    title; there is no preceding <h2>/<h4>. So we scan tables, not headings.
    """
    target = heading.lower()
    for t in soup.find_all("table"):
        first_row = t.find("tr")
        if first_row is None:
            continue
        for th in first_row.find_all("th"):
            if target in th.get_text(strip=True).lower():
                return t
    return None


def _row_test_types(row: Tag) -> list[str]:
    """Read SHL letter codes from the row's product-catalogue__key spans.

    The catalog only renders ACTIVE codes for each row (e.g. just K for a
    knowledge test). We collect them in DOM order and de-dup.
    """
    cells = row.find_all("td")
    if not cells:
        return []
    last_cell = cells[-1]
    letters: list[str] = []
    # Specific selector for the SHL DOM
    spans = last_cell.find_all("span", class_="product-catalogue__key")
    if spans:
        for s in spans:
            txt = s.get_text(strip=True)
            if len(txt) == 1 and txt.isalpha() and txt.isupper():
                letters.append(txt)
    else:
        # Fallback: any single uppercase letter token
        for s in last_cell.stripped_strings:
            token = s.strip()
            if len(token) == 1 and token.isalpha() and token.isupper():
                letters.append(token)
    # De-dup while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for letter in letters:
        if letter not in seen:
            seen.add(letter)
            out.append(letter)
    return out


def _row_flags(row: Tag) -> tuple[bool, bool]:
    """Return (remote_testing, adaptive_irt) flags.

    SHL marks 'yes' with `<span class="catalogue__circle -yes">`. The cell is
    empty otherwise. The flag cells are td[1] and td[2] respectively.
    """
    cells = row.find_all("td")
    if len(cells) < 3:
        return False, False

    def _is_yes(cell: Tag) -> bool:
        # The 'yes' marker is the modifier class '-yes' on a circle span.
        circle = cell.find("span", class_="catalogue__circle")
        if circle is None:
            return False
        klass = " ".join(circle.get("class", []))
        return "-yes" in klass

    return _is_yes(cells[1]), _is_yes(cells[2])


def _row_link(row: Tag, base_url: str) -> tuple[str, str] | None:
    """Return (name, absolute_url) from the first cell, or None if missing."""
    cells = row.find_all("td")
    if not cells:
        return None
    a = cells[0].find("a", href=True)
    if not a:
        return None
    name = a.get_text(strip=True)
    href = a["href"]
    if not name or not href:
        return None
    return name, urljoin(base_url, href)


def parse_listing(html: str, base_url: str) -> list[Assessment]:
    """Parse a single listing page; return the Individual rows only."""
    soup = BeautifulSoup(html, "lxml")
    table = _extract_section_table(soup, INDIVIDUAL_HEADING)
    if table is None:
        return []
    rows: list[Assessment] = []
    for tr in table.find_all("tr"):
        # Skip header rows (no <td>s, only <th>)
        if not tr.find("td"):
            continue
        link = _row_link(tr, base_url)
        if not link:
            continue
        name, url = link
        remote, adaptive = _row_flags(tr)
        types = _row_test_types(tr)
        rows.append(
            Assessment(
                name=name,
                url=url,
                test_types=types,
                remote_testing=remote,
                adaptive_irt=adaptive,
            )
        )
    return rows


def _detail_text(soup: BeautifulSoup, label: str) -> str:
    """Find the text node following a heading/label like 'Description'."""
    for h in soup.find_all(["h2", "h3", "h4", "p", "strong", "b"]):
        text = h.get_text(strip=True).lower()
        if text.startswith(label.lower()):
            # Try the next sibling <p>
            for sib in h.find_all_next():
                if sib.name == "p":
                    return sib.get_text(" ", strip=True)
                if sib.name in ("h2", "h3", "h4"):
                    break
    return ""


def parse_detail(html: str) -> dict[str, object]:
    """Parse an Individual Test Solution detail page.

    SHL detail pages use a "product-catalogue-training-calendar__row" pattern
    with bold labels: Description, Job levels, Languages, Assessment length.
    We do a tolerant scan across multiple selectors.
    """
    soup = BeautifulSoup(html, "lxml")

    # Approach: scan for <h4> labels and grab the following <p> text.
    description = _detail_text(soup, "Description")
    job_levels_raw = _detail_text(soup, "Job levels")
    languages_raw = _detail_text(soup, "Languages")
    length_raw = _detail_text(soup, "Assessment length")

    # If the label-scan misses, fall back to the first paragraph after <h1>.
    if not description:
        h1 = soup.find("h1")
        if h1:
            for sib in h1.find_all_next("p"):
                txt = sib.get_text(" ", strip=True)
                if txt and len(txt) > 60:
                    description = txt
                    break

    def _split_csv(s: str) -> list[str]:
        return [t.strip() for t in s.split(",") if t.strip()]

    return {
        "description": description,
        "job_levels": _split_csv(job_levels_raw),
        "languages": _split_csv(languages_raw),
        "assessment_length": length_raw,
    }


async def fetch(client: httpx.AsyncClient, url: str) -> str:
    """GET with retries on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = await client.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.NetworkError) as e:
            last_exc = e
            await asyncio.sleep(1.0 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def crawl(out_path: Path, concurrency: int = 6) -> int:
    """Walk the catalog. Returns the count of assessments scraped."""
    settings = get_settings()
    base = settings.shl_catalog_base_url

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    seen_urls: set[str] = set()
    catalog: dict[str, Assessment] = {}

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, limits=limits) as client:
        # 1) Walk listing pages
        LOG.info("Walking listing pages...")
        for page_idx in range(MAX_PAGES):
            start = page_idx * PAGE_STEP
            url = f"{base}?start={start}&type={TYPE_PARAM}"
            try:
                html = await fetch(client, url)
            except Exception as e:
                LOG.warning("Listing page %s failed: %s", url, e)
                break
            rows = parse_listing(html, base)
            new_count = sum(1 for r in rows if r.url not in seen_urls)
            LOG.info(
                "page=%d start=%d rows=%d new=%d", page_idx, start, len(rows), new_count
            )
            if new_count == 0:
                # End of pagination
                break
            for r in rows:
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                catalog[r.url] = r
            await asyncio.sleep(0.3)  # be polite

        LOG.info("Listing complete: %d unique assessments. Fetching details...", len(catalog))

        # 2) Hydrate each detail page concurrently
        sem = asyncio.Semaphore(concurrency)

        async def hydrate(asmt: Assessment) -> None:
            async with sem:
                try:
                    html = await fetch(client, asmt.url)
                except Exception as e:
                    LOG.warning("Detail %s failed: %s", asmt.url, e)
                    return
                detail = parse_detail(html)
                asmt.description = detail.get("description", "")  # type: ignore[assignment]
                asmt.job_levels = detail.get("job_levels", [])  # type: ignore[assignment]
                asmt.languages = detail.get("languages", [])  # type: ignore[assignment]
                asmt.assessment_length = detail.get("assessment_length", "")  # type: ignore[assignment]

        t0 = time.time()
        await asyncio.gather(*(hydrate(a) for a in catalog.values()))
        LOG.info("Detail fetch complete in %.1fs", time.time() - t0)

    # Validate before writing
    final = [a for a in catalog.values() if a.name and a.url]
    save_catalog(final, out_path)
    return len(final)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(settings.catalog_file))
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    out_path = Path(args.out)
    LOG.info("Scraping into %s", out_path)
    n = asyncio.run(crawl(out_path, args.concurrency))
    LOG.info("Wrote %d assessments to %s", n, out_path)
    if n == 0:
        LOG.error("No assessments scraped. Check network access and selectors.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
