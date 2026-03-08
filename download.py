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
    return url, suggested


def extract_filename(response, suggested_filename):
    """Extract filename from Content-Disposition header, falling back to suggested name."""
    cd = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd)
    if match:
        return unquote(match.group(1)).strip()
    return suggested_filename


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

    # Determine filename
    suggested = Path(dest_path).name
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


def download_with_retry(url, output_dir, session, max_retries=3, base_delay=5.0):
    """Download with exponential backoff retry logic.
    Returns (success: bool, filename: str)."""
    download_url, suggested_filename = normalize_google_url(url)
    dest_path = output_dir / suggested_filename

    for attempt in range(max_retries + 1):
        try:
            success = download_file(download_url, dest_path, session)
            return success, dest_path.name
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 403:
                print(f"  403 Forbidden — skipping (may require authentication)")
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
            return json.load(f)
    return {"completed": [], "failed": []}


def save_state(state, state_path):
    """Save download state to JSON file."""
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def read_links(links_file):
    """Read URLs from a text file, one per line, skipping blanks and comments."""
    urls = []
    with open(links_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download files from a list of URLs with throttling and resume support."
    )
    parser.add_argument("-l", "--links", default="links.txt", help="Path to URL list file (default: links.txt)")
    parser.add_argument("-o", "--output", default="./downloads", help="Output directory (default: ./downloads)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per file (default: 3)")
    parser.add_argument("--min-delay", type=float, default=3.0, help="Min seconds between downloads (default: 3)")
    parser.add_argument("--max-delay", type=float, default=7.0, help="Max seconds between downloads (default: 7)")
    parser.add_argument("--concurrency", type=int, default=1, choices=[1, 2],
                        help="Number of concurrent downloads (default: 1)")
    return parser.parse_args()


def run_sequential(pending, output_dir, session, args, state, state_path):
    """Download files one at a time."""
    for i, url in enumerate(pending):
        print(f"\n[{i + 1}/{len(pending)}] {url[:80]}")
        success, filename = download_with_retry(url, output_dir, session, args.max_retries)

        if success:
            state["completed"].append(url)
            print(f"  Done: {filename}")
        else:
            state["failed"].append(url)
            print(f"  FAILED: {filename}")
        save_state(state, state_path)

        if i < len(pending) - 1:
            delay = random.uniform(args.min_delay, args.max_delay)
            print(f"  Waiting {delay:.1f}s...")
            time.sleep(delay)


def run_concurrent(pending, output_dir, session, args, state, state_path):
    """Download files with concurrency=2, throttled."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        idx = 0

        for url in pending:
            idx += 1
            # Wait if we already have max concurrent tasks
            while len(futures) >= 2:
                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
                for f in done:
                    f_url = futures.pop(f)
                    try:
                        success, filename = f.result()
                    except Exception as e:
                        success, filename = False, f_url
                        print(f"  Error: {e}")

                    if success:
                        state["completed"].append(f_url)
                        print(f"  Done: {filename}")
                    else:
                        state["failed"].append(f_url)
                        print(f"  FAILED: {filename}")
                    save_state(state, state_path)
                    time.sleep(random.uniform(args.min_delay, args.max_delay))

            print(f"\n[{idx}/{len(pending)}] {url[:80]}")
            future = pool.submit(download_with_retry, url, output_dir, session, args.max_retries)
            futures[future] = url

        # Wait for remaining
        for f in as_completed(futures):
            f_url = futures[f]
            try:
                success, filename = f.result()
            except Exception as e:
                success, filename = False, f_url
                print(f"  Error: {e}")

            if success:
                state["completed"].append(f_url)
                print(f"  Done: {filename}")
            else:
                state["failed"].append(f_url)
                print(f"  FAILED: {filename}")
            save_state(state, state_path)


def main():
    args = parse_args()
    links_file = Path(args.links)
    output_dir = Path(args.output)
    state_path = Path(STATE_FILE)

    if not links_file.exists():
        print(f"Error: {links_file} not found")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    urls = read_links(links_file)
    if not urls:
        print("No URLs found in links file")
        return 1

    state = load_state(state_path)
    pending = [u for u in urls if u not in state["completed"]]

    if not pending:
        print(f"All {len(urls)} files already downloaded!")
        return 0

    print(f"Found {len(urls)} URLs, {len(pending)} remaining to download")
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
        return 1

    failed_count = len(state["failed"])
    completed_count = len(state["completed"])
    print(f"\nComplete: {completed_count} succeeded, {failed_count} failed")

    if failed_count:
        print("Failed URLs:")
        for u in state["failed"]:
            print(f"  {u}")

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
