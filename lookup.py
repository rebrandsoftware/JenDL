#!/usr/bin/env python3
"""Look up the original paper info for a PDF URL or keyword.

Usage:
  python lookup.py <search_term>

Examples:
  python lookup.py "springer.com/content/pdf/10.1007"
  python lookup.py "Lebanese"
  python lookup.py --failed     # show all failed downloads
"""

import json
import re
import sys
from urllib.parse import unquote


def load_mapping():
    """Build a mapping from pdf_url -> original info."""
    with open("find_pdfs_state.json") as f:
        state = json.load(f)

    entries = []
    for url, info in state["results"].items():
        match = re.search(r"[?&]q=(.+?)(?:&|$)", url)
        query = unquote(match.group(1).replace("+", " ")) if match else url
        entries.append({
            "scholar_url": url,
            "query": query,
            "title": info.get("title", ""),
            "pdf_url": info.get("pdf_url", ""),
            "doi": info.get("doi", ""),
            "status": info.get("status", ""),
        })
    return entries


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    entries = load_mapping()

    if sys.argv[1] == "--failed":
        # Cross-reference with download_state.json if it exists
        try:
            with open("download_state.json") as f:
                dl_state = json.load(f)
            failed_urls = set(dl_state.get("failed", []))
            if not failed_urls:
                print("No failed downloads recorded.")
                return
            print(f"Failed downloads ({len(failed_urls)}):\n")
            for entry in entries:
                if entry["pdf_url"] in failed_urls:
                    print(f"  Title:      {entry['title']}")
                    print(f"  PDF URL:    {entry['pdf_url']}")
                    print(f"  Scholar:    {entry['scholar_url']}")
                    if entry["doi"]:
                        print(f"  DOI:        {entry['doi']}")
                    print()
        except FileNotFoundError:
            print("No download_state.json found. Run download.py first.")
        return

    search = " ".join(sys.argv[1:]).lower()
    found = False

    for entry in entries:
        searchable = f"{entry['title']} {entry['pdf_url']} {entry['query']} {entry['doi']}".lower()
        if search in searchable:
            found = True
            print(f"  Title:      {entry['title']}")
            print(f"  Status:     {entry['status']}")
            if entry["pdf_url"]:
                print(f"  PDF URL:    {entry['pdf_url']}")
            if entry["doi"]:
                print(f"  DOI:        {entry['doi']}")
            print(f"  Scholar:    {entry['scholar_url']}")
            print()

    if not found:
        print(f"No matches for '{search}'")


if __name__ == "__main__":
    main()
