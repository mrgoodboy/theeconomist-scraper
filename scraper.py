#!/usr/bin/env python3

import argparse
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from os.path import isfile, join
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pypdf import PdfWriter
from xhtml2pdf import pisa

BASE_URL = "https://www.economist.com"
PRINT_EDITION_URL = BASE_URL + "/printedition/"
TEMP_DIR = Path("temp")
LOCATION_FILE = Path("location.txt")
MAX_WORKERS = 4
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_css_tag(soup: BeautifulSoup, href: str):
    css = soup.new_tag("link")
    css["href"] = href
    css["rel"] = "stylesheet"
    css["type"] = "text/css"
    return css


def fetch(url: str, retries: int = MAX_RETRIES) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; economist-scraper/2.0)"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            log.warning("Request failed (%s), retrying in %ds…", e, wait)
            time.sleep(wait)


def generate_pdf(html: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        result = pisa.CreatePDF(io.StringIO(html), f)
    if result.err:
        log.error("PDF generation failed for %s", dest)
        return False
    return True


def merge_pdfs(title: str, location: Path) -> Path:
    writer = PdfWriter()
    pdfs = sorted(
        (f for f in TEMP_DIR.iterdir() if f.suffix == ".pdf"),
        key=lambda p: int(p.stem.split(".")[0]),
    )
    if not pdfs:
        raise RuntimeError("No PDFs found to merge.")
    for pdf_path in pdfs:
        writer.append(str(pdf_path))
        pdf_path.unlink()

    out_path = location / (title + ".pdf")
    with out_path.open("wb") as f:
        writer.write(f)
    return out_path


def scrape_article(url: str, header_tag=None) -> str:
    full_url = BASE_URL + url if url.startswith("/") else url
    doc = fetch(full_url)
    soup = BeautifulSoup(doc, "html5lib")

    hgroup = soup.select("hgroup")
    content_nodes = soup.select(".main-content")
    if not content_nodes:
        log.warning("No .main-content found at %s", url)
        return ""

    content = content_nodes[0]
    if content.aside:
        content.aside.decompose()

    out = BeautifulSoup("<html><head></head><body></body></html>", "html5lib")
    if header_tag:
        out.body.append(header_tag)
    if hgroup:
        hgroup[0].name = "span"
        out.body.append(hgroup[0])
    out.body.append(content)
    out.head.append(get_css_tag(out, "style.css"))
    return str(out)


def content_page() -> str:
    doc = fetch(PRINT_EDITION_URL)
    soup = BeautifulSoup(doc, "html5lib")

    title_img = soup.select("#cover-image img")
    title = title_img[0]["title"] if title_img else "The Economist"
    content_nodes = soup.select(".view-content")
    if not content_nodes:
        return ""
    content = content_nodes[0]
    for icon in content.select(".comment-icon"):
        icon.extract()

    out = BeautifulSoup("<html><head></head><body></body></html>", "html5lib")
    h1 = out.new_tag("h1")
    h1["class"] = "edition-title"
    h1.string = title
    out.body.append(h1)
    out.body.append(content)
    out.head.append(get_css_tag(out, "content-page.css"))
    return str(out)


def cover_page(img_url: str) -> str:
    out = BeautifulSoup("<html><head></head><body></body></html>", "html5lib")
    img = out.new_tag("img", src=img_url)
    img["id"] = "cover-img"
    out.body.append(img)
    out.head.append(get_css_tag(out, "style.css"))
    return str(out)


def get_saved_location() -> str:
    if LOCATION_FILE.exists():
        return LOCATION_FILE.read_text().strip()
    return ""


def save_location(path: str):
    LOCATION_FILE.write_text(path)


def prompt_location() -> Path:
    while True:
        raw = input("Enter directory to save The Economist PDF: ").strip()
        path = Path(raw.replace("~", os.environ.get("HOME", "~"))).expanduser()
        if path.is_dir():
            return path
        print(f"  Directory not found: {path}")


def collect_articles(soup: BeautifulSoup):
    """Return list of (url, header_tag_or_None) for every article."""
    articles = []
    sections = soup.select(".view-content .section")
    for section in sections:
        h4_nodes = section.select("h4")
        if not h4_nodes:
            continue
        section_name = h4_nodes[0].get_text(strip=True)
        if section_name == "Economic and financial indicators":
            break

        header_tag = soup.new_tag("h4")
        header_tag.string = section_name
        header_tag["class"] = "header"

        for idx, article in enumerate(section.select(".article")):
            node = article.select(".node-link")
            if not node:
                continue
            href = node[0].get("href", "")
            articles.append((href, header_tag if idx == 0 else None, section_name))

    return articles


def scrape_all_articles(articles) -> dict:
    """Scrape articles concurrently. Returns {index: html_string}."""
    results = {}

    def worker(args):
        idx, href, header_tag, section = args
        log.info("  Scraping [%d] %s — %s", idx, section, href)
        try:
            html = scrape_article(href, header_tag)
            return idx, html
        except Exception as e:
            log.error("  Failed to scrape %s: %s", href, e)
            return idx, None

    indexed = [(i + 2, href, htag, sec) for i, (href, htag, sec) in enumerate(articles)]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(worker, item): item for item in indexed}
        for future in as_completed(futures):
            idx, html = future.result()
            if html:
                results[idx] = html

    return results


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape The Economist print edition to PDF.")
    parser.add_argument(
        "-o", "--output",
        help="Directory to save the output PDF (skips the interactive prompt).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Number of parallel article scrapers (default: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the saved output location and always prompt.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    global MAX_WORKERS
    MAX_WORKERS = args.workers

    TEMP_DIR.mkdir(exist_ok=True)

    # Resolve output location
    if args.output:
        location = Path(args.output).expanduser()
        if not location.is_dir():
            sys.exit(f"Output directory does not exist: {location}")
    else:
        saved = "" if args.no_cache else get_saved_location()
        if saved and Path(saved).is_dir():
            print(f"Using saved location: {saved}")
            print("Press Enter to confirm, or type a new path:")
            user_input = input().strip()
            location = Path(user_input.replace("~", os.environ.get("HOME", "~"))).expanduser() if user_input else Path(saved)
            if not location.is_dir():
                location = prompt_location()
        else:
            location = prompt_location()
        save_location(str(location))

    log.info("Fetching print edition index…")
    doc = fetch(PRINT_EDITION_URL)
    soup = BeautifulSoup(doc, "html5lib")

    cover_img_nodes = soup.select("#cover-image img")
    if not cover_img_nodes:
        sys.exit("Could not find cover image — the site layout may have changed.")
    cover_img_url = cover_img_nodes[0]["src"]
    issue_date = soup.select("span.issue-date")
    title = cover_img_nodes[0].get("title", "The Economist")
    if issue_date:
        log.info("Issue: %s", issue_date[0].get_text(strip=True))

    # Build pages: 0 = cover, 1 = contents
    pages: dict[int, str] = {}
    log.info("Building cover page…")
    pages[0] = cover_page(cover_img_url)
    log.info("Building contents page…")
    pages[1] = content_page()

    # Collect and scrape articles concurrently
    log.info("Collecting article links…")
    articles = collect_articles(soup)
    log.info("Scraping %d articles with %d workers…", len(articles), MAX_WORKERS)
    article_pages = scrape_all_articles(articles)
    pages.update(article_pages)

    # Generate individual PDFs
    log.info("Generating PDFs…")
    sorted_indices = sorted(pages)
    for idx in sorted_indices:
        html = pages[idx]
        dest = TEMP_DIR / f"{idx}.pdf"
        if not generate_pdf(html, dest):
            log.warning("Skipping page %d due to PDF error.", idx)

    # Merge
    log.info("Merging PDFs…")
    out_path = merge_pdfs(title, location)
    print(f"\nSaved: {out_path}\nEnjoy!")


if __name__ == "__main__":
    main()
