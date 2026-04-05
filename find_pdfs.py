#!/usr/bin/env python3
"""Find PDF URLs for papers using Semantic Scholar + Unpaywall APIs.

Reads Google Scholar query URLs from either a plain text file (pdf_urls.txt)
or a CSV file with a Google_Scholar_URL column, extracts paper titles, and
looks up open-access PDF links via Semantic Scholar and Unpaywall APIs.

Output: pdf_links.txt with one direct PDF URL per line, annotated with
source CSV row number, study ID, and article title when available.
"""

import argparse
import csv
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


def search_unpaywall(doi, email, session):
    """Look up open-access PDF via Unpaywall API. Returns pdf_url or None."""
    if not doi or not email:
        return None

    resp = session.get(
        f"https://api.unpaywall.org/v2/{doi}",
        params={"email": email},
    )

    if resp.status_code == 429:
        time.sleep(2)
        resp = session.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email},
        )

    if resp.status_code != 200:
        return None

    data = resp.json()
    best = data.get("best_oa_location")
    if best:
        return best.get("url_for_pdf") or best.get("url")
    return None


def read_csv_urls(csv_path):
    """Read Google Scholar URLs from a CSV file with metadata.

    Returns list of dicts with keys: url, csv_row, studyid, articletitle.
    csv_row is the 1-based row number in the CSV (excluding header).
    """
    entries = []
    # Try UTF-8 first, fall back to latin-1 for non-UTF-8 files
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(csv_path, newline="", encoding=encoding) as f:
                f.read()  # test full read
            break
        except UnicodeDecodeError:
            continue
    with open(csv_path, newline="", encoding=encoding) as f:
        reader = csv.DictReader(f)
        for row_num, row in enumerate(reader, start=1):
            url = row.get("Google_Scholar_URL", "").strip()
            if url:
                entries.append({
                    "url": url,
                    "csv_row": row_num,
                    "studyid": row.get("studyid", "").strip(),
                    "articletitle": row.get("articletitle", "").strip(),
                })
    return entries


def read_txt_urls(txt_path):
    """Read URLs from a plain text file (one per line).

    Returns list of dicts with keys: url, csv_row, studyid, articletitle.
    csv_row is the 1-based line number; studyid/articletitle are empty.
    """
    entries = []
    line_num = 0
    with open(txt_path) as f:
        for line in f:
            line_num += 1
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                entries.append({
                    "url": stripped,
                    "csv_row": line_num,
                    "studyid": "",
                    "articletitle": "",
                })
    return entries


def load_state(state_path):
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"results": {}}


def save_state(state, state_path):
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Find PDF URLs for papers via Semantic Scholar + Unpaywall")
    parser.add_argument("-i", "--input", default="pdf_urls.txt",
                        help="Input file with Google Scholar URLs (txt or csv)")
    parser.add_argument("--csv", default=None,
                        help="CSV file with a Google_Scholar_URL column (overrides -i)")
    parser.add_argument("-o", "--output", default="pdf_links.txt", help="Output file for PDF URLs")
    parser.add_argument("--email", default=None,
                        help="Email for Unpaywall API (enables fallback PDF lookup via DOI)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Delay between requests (default: {DEFAULT_DELAY}s)")
    parser.add_argument("--reset", action="store_true", help="Ignore previous progress and start over")
    args = parser.parse_args()

    output_path = Path(args.output)
    state_path = Path(STATE_FILE)

    # Determine input source
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"Error: {csv_path} not found")
            return 1
        entries = read_csv_urls(csv_path)
        print(f"Read {len(entries)} URLs from CSV: {csv_path}")
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: {input_path} not found")
            return 1
        entries = read_txt_urls(input_path)
        print(f"Read {len(entries)} URLs from: {input_path}")

    if not entries:
        print("No URLs found in input file")
        return 1

    if args.reset and state_path.exists():
        state_path.unlink()
        print("State reset.")

    state = load_state(state_path)
    results = state["results"]

    # Process each URL
    session = requests.Session()
    session.headers.update({"User-Agent": "JenDL/1.0 (Academic PDF Downloader)"})

    pending = [e for e in entries if e["url"] not in results]

    if not pending:
        print("All papers already looked up!")
    else:
        print(f"Found {len(entries)} URLs, {len(pending)} remaining to look up\n")

        for idx, entry in enumerate(pending):
            url = entry["url"]
            label = entry["studyid"] or f"row {entry['csv_row']}"
            # Prefer article title from CSV (cleaner match) over URL query
            # (which includes journal name + year and confuses the API)
            query = entry.get("articletitle") or extract_query(url)
            if not query:
                print(f"[{idx+1}/{len(pending)}] ({label}) Could not parse query from URL")
                results[url] = {"status": "parse_error"}
                save_state(state, state_path)
                continue

            print(f"[{idx+1}/{len(pending)}] ({label}) {query[:70]}...")

            try:
                title, pdf_url, doi = search_paper(query, session)

                if title and pdf_url:
                    print(f"    S2 found: {title[:65]}")
                    print(f"    PDF: {pdf_url}")
                    results[url] = {"status": "found", "title": title, "pdf_url": pdf_url, "doi": doi, "source": "semantic_scholar"}
                elif title:
                    print(f"    S2 found: {title[:65]}")
                    print(f"    No open-access PDF via Semantic Scholar")
                    # Try Unpaywall fallback if we have a DOI and email
                    if doi and args.email:
                        time.sleep(0.3)
                        uw_url = search_unpaywall(doi, args.email, session)
                        if uw_url:
                            print(f"    Unpaywall found PDF: {uw_url}")
                            results[url] = {"status": "found", "title": title, "pdf_url": uw_url, "doi": doi, "source": "unpaywall"}
                        else:
                            print(f"    Unpaywall: no PDF either")
                            results[url] = {"status": "no_pdf", "title": title, "doi": doi}
                    else:
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

    # Write output file — every CSV row, regardless of status
    found_count = 0
    no_pdf_count = 0
    not_found_count = 0

    with open(output_path, "w") as f:
        f.write("# PDF links log — every row from the source CSV\n")
        f.write("# csv_row = row number in source CSV (excluding header)\n")
        f.write("# studyid = study identifier from CSV\n")
        f.write("# STATUS: FOUND | NO_PDF | NOT_FOUND | ERROR\n\n")

        for entry in entries:
            url = entry["url"]
            info = results.get(url, {})
            row = entry["csv_row"]
            studyid = entry["studyid"]
            article = entry["articletitle"]
            status = info.get("status", "unknown")

            f.write(f"# csv_row:{row:03d} | studyid:{studyid} | {article}\n")

            if status == "found":
                found_count += 1
                f.write(f"# STATUS: FOUND\n")
                if info.get("doi"):
                    f.write(f"# DOI: {info['doi']}\n")
                f.write(f"# Scholar: {url}\n")
                f.write(f"{info['pdf_url']}\n\n")
            elif status == "no_pdf":
                no_pdf_count += 1
                title = info.get("title", "")
                f.write(f"# STATUS: NO_PDF — paper found in Semantic Scholar but no open-access PDF\n")
                if info.get("doi"):
                    f.write(f"# DOI: {info['doi']}\n")
                f.write(f"# Semantic Scholar title: {title}\n")
                f.write(f"# Search manually: {url}\n\n")
            elif status in ("not_found", "parse_error"):
                not_found_count += 1
                f.write(f"# STATUS: NOT_FOUND — not found in Semantic Scholar\n")
                f.write(f"# Search manually: {url}\n\n")
            else:
                not_found_count += 1
                error_msg = info.get("error", "unknown error")
                f.write(f"# STATUS: ERROR — {error_msg}\n")
                f.write(f"# Search manually: {url}\n\n")

    print(f"\nSummary:")
    print(f"  {found_count} PDF URLs found (written to {output_path})")
    print(f"  {no_pdf_count} papers found but no open-access PDF")
    print(f"  {not_found_count} papers not found or errored")
    print(f"  {len(entries)} total rows logged to {output_path}")

    if no_pdf_count or not_found_count:
        print(f"\nPapers needing manual lookup:")
        for entry in entries:
            info = results.get(entry["url"], {})
            status = info.get("status", "")
            if status in ("no_pdf", "not_found", "error", "parse_error"):
                label = entry["studyid"] or f"row {entry['csv_row']}"
                reason = "no OA PDF" if status == "no_pdf" else "not found"
                print(f"  - ({label}) [{reason}] {entry['articletitle'][:60]}")
                print(f"    {entry['url']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
