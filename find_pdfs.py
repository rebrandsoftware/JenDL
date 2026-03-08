#!/usr/bin/env python3
"""Find PDF URLs for papers listed in pdf_urls.txt using Semantic Scholar API.

Reads Google Scholar query URLs, extracts paper titles, and looks up
open-access PDF links via the Semantic Scholar API.

Output: pdf_links.txt with one direct PDF URL per line.
"""

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote

import requests

STATE_FILE = "find_pdfs_state.json"
DEFAULT_DELAY = 1.5  # seconds between API requests (S2 allows 1 req/sec without key)


def extract_query(url):
    """Extract the search query text from a Google Scholar URL."""
    match = re.search(r'[?&]q=(.+?)(?:&|$)', url)
    if match:
        return unquote(match.group(1).replace('+', ' ').replace('%2B', '+'))
    return None


def search_paper(query, session):
    """Search Semantic Scholar for a paper by title. Returns (title, pdf_url) or (None, None)."""
    resp = session.get(
        "https://api.semanticscholar.org/graph/v1/paper/search/match",
        params={"query": query, "fields": "title,openAccessPdf,externalIds"},
    )

    if resp.status_code == 429:
        # Rate limited — wait and retry once
        retry_after = int(resp.headers.get("Retry-After", 5))
        print(f"    Rate limited, waiting {retry_after}s...")
        time.sleep(retry_after)
        resp = session.get(
            "https://api.semanticscholar.org/graph/v1/paper/search/match",
            params={"query": query, "fields": "title,openAccessPdf,externalIds"},
        )

    if resp.status_code == 404:
        return None, None, None

    resp.raise_for_status()
    data = resp.json()

    if data.get("data"):
        paper = data["data"][0]
        title = paper.get("title", "")
        pdf_info = paper.get("openAccessPdf")
        pdf_url = pdf_info["url"] if pdf_info else None
        doi = paper.get("externalIds", {}).get("DOI")
        return title, pdf_url, doi

    return None, None, None


def load_state(state_path):
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"results": {}}


def save_state(state, state_path):
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Find PDF URLs for papers via Semantic Scholar")
    parser.add_argument("-i", "--input", default="pdf_urls.txt", help="Input file with Google Scholar URLs")
    parser.add_argument("-o", "--output", default="pdf_links.txt", help="Output file for PDF URLs")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Delay between requests (default: {DEFAULT_DELAY}s)")
    parser.add_argument("--reset", action="store_true", help="Ignore previous progress and start over")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    state_path = Path(STATE_FILE)

    if not input_path.exists():
        print(f"Error: {input_path} not found")
        return 1

    if args.reset and state_path.exists():
        state_path.unlink()
        print("State reset.")

    # Read URLs
    with open(input_path) as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    state = load_state(state_path)
    results = state["results"]

    # Process each URL
    session = requests.Session()
    session.headers.update({"User-Agent": "JenDL/1.0 (Academic PDF Downloader)"})

    pending = [(i, url) for i, url in enumerate(urls) if url not in results]

    if not pending:
        print("All papers already looked up!")
    else:
        print(f"Found {len(urls)} URLs, {len(pending)} remaining to look up\n")

        for idx, (i, url) in enumerate(pending):
            query = extract_query(url)
            if not query:
                print(f"[{idx+1}/{len(pending)}] Could not parse query from URL")
                results[url] = {"status": "parse_error"}
                save_state(state, state_path)
                continue

            print(f"[{idx+1}/{len(pending)}] {query[:75]}...")

            try:
                title, pdf_url, doi = search_paper(query, session)

                if title and pdf_url:
                    print(f"    Found: {title[:65]}")
                    print(f"    PDF: {pdf_url}")
                    results[url] = {"status": "found", "title": title, "pdf_url": pdf_url, "doi": doi}
                elif title:
                    print(f"    Found: {title[:65]}")
                    print(f"    No open-access PDF available")
                    results[url] = {"status": "no_pdf", "title": title, "doi": doi}
                else:
                    print(f"    Not found in Semantic Scholar")
                    results[url] = {"status": "not_found"}

            except Exception as e:
                print(f"    Error: {e}")
                results[url] = {"status": "error", "error": str(e)}

            save_state(state, state_path)

            if idx < len(pending) - 1:
                time.sleep(args.delay)

    # Write output file with PDF URLs
    pdf_urls = []
    no_pdf = []
    not_found = []

    for url in urls:
        info = results.get(url, {})
        if info.get("status") == "found":
            pdf_urls.append(info["pdf_url"])
        elif info.get("status") == "no_pdf":
            no_pdf.append(info.get("title", url))
        elif info.get("status") in ("not_found", "error", "parse_error"):
            not_found.append(url)

    with open(output_path, "w") as f:
        for pdf_url in pdf_urls:
            f.write(pdf_url + "\n")

    print(f"\nSummary:")
    print(f"  {len(pdf_urls)} PDF URLs written to {output_path}")
    print(f"  {len(no_pdf)} papers found but no open-access PDF")
    print(f"  {len(not_found)} papers not found")

    if no_pdf:
        print(f"\nPapers without open-access PDF:")
        for title in no_pdf:
            print(f"  - {title[:80]}")

    if not_found:
        print(f"\nPapers not found:")
        for url in not_found:
            query = extract_query(url) or url
            print(f"  - {query[:80]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
