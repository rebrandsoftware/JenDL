#!/usr/bin/env python3
"""Cross-platform batch file downloader with throttling and resume support."""

import argparse
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import requests
from tqdm import tqdm

STATE_FILE = "download_state.json"


def normalize_google_url(url):
    """Convert Google Drive/Docs URLs to direct download URLs.
    Returns (download_url, suggested_filename)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # Google Drive: /file/d/{FILE_ID}/view
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
    if match and "drive.google.com" in host:
        file_id = match.group(1)
        return (
            f"https://drive.google.com/uc?export=download&id={file_id}",
            f"{file_id}.pdf",
        )

    # Google Drive: /open?id={FILE_ID}
    if "drive.google.com" in host and "open" in parsed.path:
        qs = parse_qs(parsed.query)
        if "id" in qs:
            file_id = qs["id"][0]
            return (
                f"https://drive.google.com/uc?export=download&id={file_id}",
                f"{file_id}.pdf",
            )

    # Google Docs: /document/d/{FILE_ID}/...
    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", parsed.path)
    if match and "docs.google.com" in host:
        file_id = match.group(1)
        return (
            f"https://docs.google.com/document/d/{file_id}/export?format=pdf",
            f"{file_id}.pdf",
        )

    # Google Sheets: /spreadsheets/d/{FILE_ID}/...
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", parsed.path)
    if match and "docs.google.com" in host:
        file_id = match.group(1)
        return (
            f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=pdf",
            f"{file_id}.pdf",
        )

    # Google Slides: /presentation/d/{FILE_ID}/...
    match = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", parsed.path)
    if match and "docs.google.com" in host:
        file_id = match.group(1)
        return (
            f"https://docs.google.com/presentation/d/{file_id}/export?format=pdf",
            f"{file_id}.pdf",
        )

    # Not a recognized Google URL — use as-is
    path_name = Path(parsed.path).name
    suggested = unquote(path_name) if path_name else "download"
    # Ensure it has a file extension; if not, add .pdf
    if "." not in suggested:
        suggested = suggested + ".pdf"
    return url, suggested


def sanitize_filename(name):
    """Remove or replace characters that are invalid in filenames."""
    # Replace path separators and other problematic chars
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*]', '_', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    return name.strip(". _")


def extract_filename(response, suggested_filename):
    """Extract filename from Content-Disposition header, falling back to suggested name."""
    cd = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd)
    if match:
        return sanitize_filename(unquote(match.group(1)).strip())
    return sanitize_filename(suggested_filename)


def handle_gdrive_confirmation(session, url, response):
    """Handle Google Drive virus-scan confirmation page for large files."""
    if "text/html" not in response.headers.get("Content-Type", ""):
        return url, response

    # Check cookies for download_warning token
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            confirm_url = f"{url}&confirm={value}"
            response.close()
            new_resp = session.get(confirm_url, stream=True, allow_redirects=True)
            return confirm_url, new_resp

    # Try to find confirm token in HTML body
    content = response.content.decode("utf-8", errors="ignore")
    match = re.search(r'confirm=([a-zA-Z0-9_-]+)', content)
    if match:
        confirm_url = f"{url}&confirm={match.group(1)}"
        new_resp = session.get(confirm_url, stream=True, allow_redirects=True)
        return confirm_url, new_resp

    # Also check for a uuid-based confirmation pattern
    match = re.search(r'id="download-form" action="(.+?)"', content)
    if match:
        from html import unescape
        action_url = unescape(match.group(1))
        new_resp = session.get(action_url, stream=True, allow_redirects=True)
        return action_url, new_resp

    return url, response


def download_file(url, dest_path, session):
    """Download a single file with progress bar and .part file support.
    Returns True on success, False on failure."""
    part_path = Path(str(dest_path) + ".part")
    headers = {}
    existing_size = 0

    # Resume support
    if part_path.exists():
        existing_size = part_path.stat().st_size
        headers["Range"] = f"bytes={existing_size}-"

    response = session.get(url, stream=True, headers=headers, allow_redirects=True)

    # Handle Google Drive confirmation pages
    if "drive.google.com" in url or "docs.google.com" in url:
        url, response = handle_gdrive_confirmation(session, url, response)

    response.raise_for_status()

    # Only use server-suggested filename if we don't already have a prefix-based name
    suggested = Path(dest_path).name
    if not re.match(r'^\d{3}\.pdf$', suggested):
        actual_filename = extract_filename(response, suggested)
        if actual_filename != suggested:
            dest_path = dest_path.parent / actual_filename
            part_path = Path(str(dest_path) + ".part")

    # If already fully downloaded
    if dest_path.exists():
        response.close()
        return True

    total_size = response.headers.get("Content-Length")
    if total_size is not None:
        total_size = int(total_size) + existing_size

    # Check if server supports range (206) or is sending full file (200)
    mode = "ab" if response.status_code == 206 else "wb"
    if response.status_code == 200:
        existing_size = 0  # Server sent full file, overwrite

    with open(part_path, mode) as f:
        with tqdm(
            total=total_size,
            initial=existing_size,
            unit="B",
            unit_scale=True,
            desc=dest_path.name[:40],
            leave=True,
        ) as pbar:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))

    # Rename .part to final filename
    part_path.rename(dest_path)
    return True


def download_with_retry(url, output_dir, session, max_retries=3, base_delay=5.0, prefix=""):
    """Download with exponential backoff retry logic.
    Returns (success: bool, filename: str)."""
    download_url, suggested_filename = normalize_google_url(url)
    if prefix:
        suggested_filename = f"{prefix}.pdf"
    else:
        suggested_filename = sanitize_filename(suggested_filename)
    dest_path = output_dir / suggested_filename

    for attempt in range(max_retries + 1):
        try:
            success = download_file(download_url, dest_path, session)
            return success, dest_path.name
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (403, 404, 410):
                print(f"  HTTP {status} — skipping")
                return False, suggested_filename
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 3)
                print(f"  HTTP {status} — retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                continue
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 3)
                print(f"  Connection error — retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                continue
            print(f"  Failed after {max_retries} retries: {e}")
            return False, suggested_filename
        except Exception as e:
            print(f"  Unexpected error: {e}")
            return False, suggested_filename

    return False, suggested_filename


def load_state(state_path):
    """Load download state from JSON file."""
    if state_path.exists():
        with open(state_path) as f:
            data = json.load(f)
        # Migrate old list-based format to dict-based
        if isinstance(data.get("completed"), list):
            data["completed"] = {url: {"filename": ""} for url in data["completed"]}
        if isinstance(data.get("failed"), list):
            data["failed"] = {url: {"filename": ""} for url in data["failed"]}
        return data
    return {"completed": {}, "failed": {}}


def save_state(state, state_path):
    """Save download state to JSON file."""
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def read_links(links_file):
    """Read the pdf_links.txt file, parsing metadata blocks.

    Each block starts with '# csv_row:NNN | studyid:XXX | Article Title'
    and may contain a STATUS line and a URL line.

    Returns list of dicts with keys:
      url, csv_row, studyid, articletitle, status, scholar_url
    Entries without a PDF URL have url=None.
    """
    entries = []
    current = None

    with open(links_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if line.startswith("#"):
                # Start of a new block?
                header = re.match(
                    r'^#\s*csv_row:(\d+)\s*\|\s*studyid:(\S*)\s*\|\s*(.*)$', line
                )
                if header:
                    # Save previous block
                    if current is not None:
                        entries.append(current)
                    current = {
                        "csv_row": header.group(1),
                        "studyid": header.group(2),
                        "articletitle": header.group(3).strip(),
                        "url": None,
                        "status": "FOUND",
                        "scholar_url": "",
                    }
                    continue

                if current is None:
                    continue

                # Parse status
                status_match = re.match(r'^#\s*STATUS:\s*(\S+)', line)
                if status_match:
                    current["status"] = status_match.group(1)
                    continue

                # Parse scholar/manual URL
                scholar_match = re.match(r'^#\s*(?:Scholar|Search manually):\s*(.+)$', line)
                if scholar_match:
                    current["scholar_url"] = scholar_match.group(1).strip()
                    continue

                # Legacy format: '# 042 - Title'
                legacy = re.match(r'^#\s*(\d{2,4})\s*-\s*(.*)$', line)
                if legacy and current is None:
                    current = {
                        "csv_row": legacy.group(1),
                        "studyid": "",
                        "articletitle": legacy.group(2).strip(),
                        "url": None,
                        "status": "FOUND",
                        "scholar_url": "",
                    }

                continue

            # Non-comment line = URL
            if current is not None:
                current["url"] = line
            else:
                # Bare URL with no metadata
                entries.append({
                    "csv_row": "",
                    "studyid": "",
                    "articletitle": "",
                    "url": line,
                    "status": "FOUND",
                    "scholar_url": "",
                })

    # Don't forget the last block
    if current is not None:
        entries.append(current)

    return entries


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download files from a list of URLs with throttling and resume support."
    )
    parser.add_argument("-l", "--links", default="links.txt", help="Path to URL list file (default: links.txt)")
    parser.add_argument("-o", "--output", default="./downloads", help="Output directory (default: ./downloads)")
    parser.add_argument("--log", default="download_log.txt", help="Download log file (default: download_log.txt)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per file (default: 3)")
    parser.add_argument("--min-delay", type=float, default=3.0, help="Min seconds between downloads (default: 3)")
    parser.add_argument("--max-delay", type=float, default=7.0, help="Max seconds between downloads (default: 7)")
    parser.add_argument("--concurrency", type=int, default=1, choices=[1, 2],
                        help="Number of concurrent downloads (default: 1)")
    return parser.parse_args()


def build_prefix(entry):
    """Build a filename prefix from csv_row number."""
    row = entry.get("csv_row", "")
    if row:
        return f"{int(row):03d}"
    return ""


def run_sequential(pending, output_dir, session, args, state, state_path):
    """Download files one at a time."""
    for i, entry in enumerate(pending):
        url = entry["url"]
        prefix = build_prefix(entry)
        label = entry.get("studyid") or entry.get("csv_row") or ""
        title_short = entry.get("articletitle", "")[:50]
        print(f"\n[{i + 1}/{len(pending)}] ({label}) {title_short}")
        print(f"  URL: {url}")
        success, filename = download_with_retry(url, output_dir, session, args.max_retries, prefix=prefix)

        if success:
            state["completed"][url] = {"filename": filename, **entry}
            print(f"  Saved: {filename}")
        else:
            state["failed"][url] = {"filename": filename, **entry}
            print(f"  FAILED: {filename}")
        save_state(state, state_path)

        if i < len(pending) - 1:
            delay = random.uniform(args.min_delay, args.max_delay)
            print(f"  Waiting {delay:.1f}s...")
            time.sleep(delay)


def run_concurrent(pending, output_dir, session, args, state, state_path):
    """Download files with concurrency=2, throttled."""
    entry_by_url = {e["url"]: e for e in pending}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        idx = 0

        for entry in pending:
            url = entry["url"]
            prefix = build_prefix(entry)
            idx += 1
            # Wait if we already have max concurrent tasks
            while len(futures) >= 2:
                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
                for f in done:
                    f_url = futures.pop(f)
                    f_entry = entry_by_url[f_url]
                    try:
                        success, filename = f.result()
                    except Exception as e:
                        success, filename = False, f_url
                        print(f"  Error: {e}")

                    if success:
                        state["completed"][f_url] = {"filename": filename, **f_entry}
                        print(f"  Saved: {filename}")
                    else:
                        state["failed"][f_url] = {"filename": filename, **f_entry}
                        print(f"  FAILED: {filename}")
                    save_state(state, state_path)
                    time.sleep(random.uniform(args.min_delay, args.max_delay))

            label = entry.get("studyid") or entry.get("csv_row") or ""
            title_short = entry.get("articletitle", "")[:50]
            print(f"\n[{idx}/{len(pending)}] ({label}) {title_short}")
            print(f"  URL: {url}")
            future = pool.submit(download_with_retry, url, output_dir, session, args.max_retries, prefix=prefix)
            futures[future] = url

        # Wait for remaining
        for f in as_completed(futures):
            f_url = futures[f]
            f_entry = entry_by_url[f_url]
            try:
                success, filename = f.result()
            except Exception as e:
                success, filename = False, f_url
                print(f"  Error: {e}")

            if success:
                state["completed"][f_url] = {"filename": filename, **f_entry}
                print(f"  Saved: {filename}")
            else:
                state["failed"][f_url] = {"filename": filename, **f_entry}
                print(f"  FAILED: {filename}")
            save_state(state, state_path)


def write_log(log_path, all_entries, state):
    """Write a comprehensive download log file."""
    completed = state["completed"]
    failed = state["failed"]

    # Separate entries by outcome
    downloaded = []
    dl_failed = []
    no_link = []

    for entry in all_entries:
        url = entry.get("url")
        if url is None:
            no_link.append(entry)
        elif url in completed:
            info = completed[url]
            downloaded.append((entry, info.get("filename", "")))
        elif url in failed:
            info = failed[url]
            dl_failed.append((entry, info.get("filename", "")))
        else:
            no_link.append(entry)

    with open(log_path, "w") as f:
        from datetime import datetime
        f.write(f"DOWNLOAD LOG — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 70 + "\n\n")

        # --- Successfully downloaded ---
        f.write(f"DOWNLOADED ({len(downloaded)} files)\n")
        f.write("-" * 40 + "\n")
        if downloaded:
            for entry, filename in downloaded:
                row = entry.get("csv_row", "?")
                sid = entry.get("studyid", "")
                title = entry.get("articletitle", "")
                f.write(f"  Row {row} | {sid} | {title}\n")
                f.write(f"    File: {filename}\n")
                f.write(f"    URL:  {entry['url']}\n\n")
        else:
            f.write("  (none)\n\n")

        # --- Failed downloads ---
        f.write(f"DOWNLOAD FAILED ({len(dl_failed)} files)\n")
        f.write("-" * 40 + "\n")
        if dl_failed:
            for entry, filename in dl_failed:
                row = entry.get("csv_row", "?")
                sid = entry.get("studyid", "")
                title = entry.get("articletitle", "")
                scholar = entry.get("scholar_url", "")
                f.write(f"  Row {row} | {sid} | {title}\n")
                f.write(f"    URL:  {entry['url']}\n")
                if scholar:
                    f.write(f"    Search manually: {scholar}\n")
                f.write("\n")
        else:
            f.write("  (none)\n\n")

        # --- No PDF link available ---
        f.write(f"NO PDF LINK AVAILABLE ({len(no_link)} papers)\n")
        f.write("-" * 40 + "\n")
        if no_link:
            for entry in no_link:
                row = entry.get("csv_row", "?")
                sid = entry.get("studyid", "")
                title = entry.get("articletitle", "")
                status = entry.get("status", "")
                scholar = entry.get("scholar_url", "")
                f.write(f"  Row {row} | {sid} | {title}\n")
                f.write(f"    Reason: {status}\n")
                if scholar:
                    f.write(f"    Search manually: {scholar}\n")
                f.write("\n")
        else:
            f.write("  (none)\n\n")

    return len(downloaded), len(dl_failed), len(no_link)


def main():
    args = parse_args()
    links_file = Path(args.links)
    output_dir = Path(args.output)
    state_path = Path(STATE_FILE)
    log_path = Path(args.log)

    if not links_file.exists():
        print(f"Error: {links_file} not found")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    all_entries = read_links(links_file)
    if not all_entries:
        print("No entries found in links file")
        return 1

    # Separate downloadable (have a URL) from not-available
    downloadable = [e for e in all_entries if e.get("url")]
    no_link = [e for e in all_entries if not e.get("url")]

    if no_link:
        print(f"{len(no_link)} papers have no PDF link (see log for details)")

    state = load_state(state_path)
    pending = [e for e in downloadable if e["url"] not in state["completed"]]

    if not pending:
        print(f"All {len(downloadable)} downloadable files already downloaded!")
    else:
        print(f"Found {len(downloadable)} downloadable URLs, {len(pending)} remaining")
        print(f"Output: {output_dir.resolve()}")
        print(f"Concurrency: {args.concurrency}, Delay: {args.min_delay}-{args.max_delay}s")

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        try:
            if args.concurrency == 1:
                run_sequential(pending, output_dir, session, args, state, state_path)
            else:
                run_concurrent(pending, output_dir, session, args, state, state_path)
        except KeyboardInterrupt:
            print("\n\nInterrupted! Progress saved. Re-run to resume.")
            save_state(state, state_path)

    # Always write the log
    ok_count, fail_count, nolink_count = write_log(log_path, all_entries, state)

    print(f"\n{'=' * 50}")
    print(f"RESULTS: {ok_count} downloaded, {fail_count} failed, {nolink_count} no link")
    print(f"Full log: {log_path.resolve()}")

    if fail_count:
        print(f"\nFailed downloads (search manually):")
        for e in downloadable:
            if e["url"] in state["failed"]:
                row = e.get("csv_row", "?")
                sid = e.get("studyid", "")
                scholar = e.get("scholar_url", e["url"])
                print(f"  Row {row} ({sid}): {scholar}")

    if nolink_count:
        print(f"\nNo PDF link — search manually:")
        for e in no_link:
            row = e.get("csv_row", "?")
            sid = e.get("studyid", "")
            scholar = e.get("scholar_url", "")
            print(f"  Row {row} ({sid}): {scholar}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
