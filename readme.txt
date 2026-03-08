JenDL - Batch File Downloader
=============================

Downloads files from a list of URLs with throttling to avoid rate limits.
Supports Google Drive and Google Docs links. Works on Mac and Windows.


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


USAGE
-----

1. Add your URLs to links.txt, one per line. Lines starting with # are ignored.

2. Run the downloader:
   python download.py

   Files are saved to the ./downloads/ folder.

3. If interrupted (Ctrl+C), just re-run the same command. It will skip
   already-completed files and resume where it left off.


OPTIONS
-------

  -l, --links FILE        URL list file (default: links.txt)
  -o, --output DIR        Output directory (default: ./downloads)
  --max-retries N         Retries per file on failure (default: 3)
  --min-delay SECONDS     Minimum wait between downloads (default: 3)
  --max-delay SECONDS     Maximum wait between downloads (default: 7)
  --concurrency {1,2}     Parallel downloads, 1 or 2 (default: 1)

Examples:

  python download.py -l mylinks.txt -o pdfs/
  python download.py --min-delay 5 --max-delay 10
  python download.py --concurrency 2


STARTING OVER
-------------

To re-download everything from scratch, delete the state file:

  Mac:     rm download_state.json
  Windows: del download_state.json

Then run python download.py again.


TROUBLESHOOTING
---------------

"All files already downloaded!" but files are missing:
  Delete download_state.json (see STARTING OVER above).

403 Forbidden errors:
  The file may require a Google login. Make sure the link is set to
  "Anyone with the link can view" in Google Drive sharing settings.

429 Too Many Requests:
  The script will automatically wait and retry with increasing delays.
  If it keeps happening, increase --min-delay and --max-delay.

Slow downloads:
  This is intentional. The delays between downloads prevent Google from
  blocking your IP. For 100 files, expect roughly 10-15 minutes of
  wait time between downloads.
