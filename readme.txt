JenDL - Batch Academic PDF Downloader
======================================

Downloads academic papers as PDFs from Google Scholar search URLs.
Works in two stages:
  1. find_pdfs.py  - Looks up PDF links via the Semantic Scholar API
  2. download.py   - Downloads the PDFs with throttling and resume support

Works on Mac and Windows.


SETUP
-----

1. Install Python 3.8+ if not already installed:
   - Mac: brew install python3  (or download from https://python.org)
   - Windows: Download from https://python.org (check "Add to PATH" during install)

2. Open a terminal (Mac: Terminal app, Windows: Command Prompt or PowerShell)

3. Navigate to this folder:
   cd path/to/JenDL

4. Create a virtual environment:
   python3 -m venv venv          (Mac)
   python -m venv venv           (Windows)

5. Activate the virtual environment:
   source venv/bin/activate      (Mac)
   venv\Scripts\activate         (Windows)

6. Install dependencies:
   pip install -r requirements.txt


STEP 1: FIND PDF LINKS
-----------------------

Add your Google Scholar search URLs to pdf_urls.txt, one per line.
Example format:
  https://scholar.google.com/scholar?q=Paper+Title+Here

Then run:
  python find_pdfs.py

This searches the Semantic Scholar API for each paper and writes direct
PDF URLs to pdf_links.txt. Each entry includes a numbered comment with
the paper title and original Scholar URL for traceability.

Options:
  -i, --input FILE      Input file with Scholar URLs (default: pdf_urls.txt)
  -o, --output FILE     Output file for PDF URLs (default: pdf_links.txt)
  --delay SECONDS       Delay between API requests (default: 1.5)
  --reset               Ignore previous progress and start over

Progress is saved to find_pdfs_state.json. If interrupted, re-run to
resume. Papers without open-access PDFs and papers not found are listed
in the summary and written to failed_papers.txt.


STEP 2: DOWNLOAD PDFS
----------------------

Run the downloader on the PDF links:
  python download.py -l pdf_links.txt

Files are saved to ./downloads/ with a number prefix matching the
original paper number (e.g., 042_filename.pdf).

If interrupted (Ctrl+C), just re-run the same command. It will skip
already-completed files and resume where it left off.

Options:
  -l, --links FILE        URL list file (default: links.txt)
  -o, --output DIR        Output directory (default: ./downloads)
  --max-retries N         Retries per file on failure (default: 3)
  --min-delay SECONDS     Minimum wait between downloads (default: 3)
  --max-delay SECONDS     Maximum wait between downloads (default: 7)
  --concurrency {1,2}     Parallel downloads, 1 or 2 (default: 1)

Examples:
  python download.py -l pdf_links.txt -o pdfs/
  python download.py -l pdf_links.txt --min-delay 5 --max-delay 10
  python download.py -l pdf_links.txt --concurrency 2


LOOKING UP PAPERS
-----------------

Use lookup.py to trace any downloaded file back to its source:

  python lookup.py "keyword"        Search by title, URL, or DOI
  python lookup.py "springer.com"   Search by URL fragment
  python lookup.py --failed         Show all failed downloads with source info


STARTING OVER
-------------

To start completely fresh (both lookup and downloads):

  Mac:     rm -f download_state.json find_pdfs_state.json
           rm -rf downloads/
  Windows: del download_state.json find_pdfs_state.json
           rmdir /s downloads

Then re-run both steps:
  python find_pdfs.py
  python download.py -l pdf_links.txt

To re-run only the PDF link lookup (keeps existing downloads):
  python find_pdfs.py --reset

To re-run only the downloads (keeps existing lookup results):
  Mac:     rm -f download_state.json && rm -rf downloads/
  Windows: del download_state.json && rmdir /s downloads
  Then:    python download.py -l pdf_links.txt


TROUBLESHOOTING
---------------

"All files already downloaded!" but files are missing:
  Delete download_state.json (see STARTING OVER above).

403/404 errors during download:
  Some publishers block automated downloads. The script will skip these
  and record them as failed. Use "python lookup.py --failed" to see the
  list with original Scholar URLs, then download manually in a browser.

429 Too Many Requests:
  The script will automatically wait and retry with increasing delays.
  If it keeps happening, increase --min-delay and --max-delay.

Papers not found by find_pdfs.py:
  Semantic Scholar may not have the paper, or the title may differ
  slightly. Check failed_papers.txt for the list and search manually.

Slow downloads:
  This is intentional. The delays between downloads prevent publishers
  from blocking your IP.
