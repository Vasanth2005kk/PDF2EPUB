# PDF2EPUB

PDF2EPUB is a Flask single-page web application that converts PDF documents into reflowable **EPUB 3** eBooks. It does not just repackage pages. It extracts the text, fonts, images, tables and bookmarks, rebuilds the document's logical structure (chapters, headings, paragraphs, lists), and packages the result as a validated EPUB 3 that works in common reading apps.

---

## 1. Features

- **Single-page UI**: drag-and-drop upload with real upload progress, a document preview, conversion settings, live progress for each stage, then download. It is responsive, works with a keyboard and screen readers, and supports light and dark modes.
- **PDF validation**: checks the extension, the declared MIME type, the `%PDF-` file signature, file size and page count, whether the file can be parsed, and whether it is password-protected or empty.
- **Text extraction** (PyMuPDF): works page by page and records font size, weight and style for each line. It handles two-column reading order, rejoins words hyphenated across lines, rebuilds paragraphs across line, column and page breaks, and keeps bold, italic, superscript and monospace inside paragraphs.
- **Structure reconstruction**:
  - The **PDF outline (bookmarks)** is used when present.
  - Otherwise, headings are detected from font size relative to body text, bold weight and patterns such as `Chapter 3`, `Part II`, `அத்தியாயம்`.
  - Chapters are split at the most significant heading level that gives a *plausible, balanced* division. If no reliable headings exist, the book uses **page-range sections** ("Pages 1–10") rather than invented titles.
  - Bulleted and numbered lists, ruled tables, image placement and print page-break anchors are kept.
  - Running headers, footers and page numbers are removed.
- **Images**: embedded JPEG and PNG files are kept byte-for-byte by default. JPX, JBIG2, CCITT and CMYK images are converted to PNG, and transparency (soft masks) is preserved. Images are de-duplicated by object and by content hash, and tiny decorative images are skipped. Optional resizing and recompression (High, Medium, Low).
- **OCR for scanned pages** (Tesseract): pages without a text layer are detected, rendered at 300 DPI and recognised. The recognised text goes through the same structure pipeline. If OCR is disabled or unavailable, scanned pages are kept as page images and the result says so.
- **EPUB 3 output** (EbookLib):
  - Package contents: `mimetype`, `container.xml`, OPF package, `nav.xhtml` with nested contents, an NCX for older readers, XHTML chapters, CSS, images, a cover rendered from page 1, and full metadata.
  - Very long chapters are split across several XHTML files.
- **Validation**:
  - Built-in structural checks run on every EPUB: archive layout, OPF, manifest, spine, nav, XHTML well-formedness, image, CSS and link targets, and anchors.
  - **EPUBCheck** also runs when it is installed.
  - The result always states which kind of validation was performed.
- **Background processing**: each conversion runs in its own subprocess, from a bounded worker pool, with a hard timeout and cancellation. Progress is reported from real pipeline stages and page counters.
- **Security and cleanup**: server-generated UUID job IDs, no use of user-supplied paths, a strict Content-Security-Policy and other security headers, safe error messages (no tracebacks), hardened XML parsing, and automatic removal of expired job files.

## 2. Technology stack

| Area | Technology |
|---|---|
| Backend | Python 3.11+, Flask 3 (application factory), Werkzeug |
| PDF processing | PyMuPDF 2.x (text, fonts, images, tables, rendering), Pillow |
| OCR | Tesseract via pytesseract (optional at runtime) |
| EPUB | EbookLib (EPUB 3), lxml (validation) |
| Frontend | HTML5, CSS3, vanilla JavaScript (no framework, no CDN) |
| Tooling | uv, pytest, Ruff; gunicorn for production |

**Deviations from the original brief:**
- **pdfplumber is not used.** PyMuPDF's own table finder works on the same document object and coordinate system, so the PDF doesn't have to be parsed a second time by a slower library.
- **BeautifulSoup is installed but not needed.** The XHTML is generated directly with escaping and validated with lxml.

## 3. Architecture

```text
app/
├── __init__.py              # create_app(): config, logging, error handlers, security headers, CLI
├── config.py                # environment-driven configuration classes
├── exceptions.py            # user-facing exception hierarchy (safe messages + HTTP status)
├── routes/converter.py      # SPA route + JSON API
├── services/
│   ├── pdf_analyzer.py      # validation (open_pdf) and preview analysis
│   ├── pdf_extractor.py     # text blocks/lines/runs with fonts, images, tables, page renders
│   ├── ocr_service.py       # Tesseract detection, language mapping, page OCR
│   ├── content_processor.py # structure reconstruction (headings, paragraphs, lists, chapters)
│   ├── epub_generator.py    # EPUB 3 packaging with EbookLib
│   ├── epub_validator.py    # structural checks + optional EPUBCheck
│   ├── conversion_service.py# the pipeline + progress reporting (runs in the subprocess)
│   ├── job_manager.py       # bounded queue, one spawned process per job, timeout/cancel
│   └── models.py            # intermediate document model
├── utils/
│   ├── file_handler.py      # per-job storage, atomic job.json state
│   ├── validators.py        # job IDs, filenames, upload checks, conversion options
│   └── cleanup.py           # TTL-based removal of expired jobs (thread + CLI)
├── templates/index.html
└── static/{css,js,images}
storage/jobs/<job_id>/       # source.pdf, job.json, work/, output.epub  (git-ignored)
tests/                       # pytest suite + pdf_factory.py (generates all sample PDFs)
run.py                       # development server entry point
```

**Why the storage layout differs from the brief:** it uses one directory per job instead of separate `uploads/` and `converted/` folders. Deleting or expiring a job is then a single `rmtree`, paths are always derived from a validated UUID, and `job.json` sits beside the files it describes.

**Why each conversion runs in a separate process:**
- PyMuPDF is not thread-safe.
- A process can be terminated, which gives a hard timeout.
- A crash on a malformed PDF cannot take the web server down.
- All memory is returned to the OS when the process ends.

Progress is written atomically to `job.json`, so any web worker can answer status requests.

## 4. Installation

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repository> && cd PDF2EPUB
uv sync                       # creates .venv and installs dependencies
cp .env.example .env          # then edit SECRET_KEY etc.
```

The project was created with:

```bash
uv init --bare
uv add flask pymupdf pillow ebooklib beautifulsoup4 lxml python-dotenv pytesseract
uv add --dev pytest ruff
uv add --optional production gunicorn
```

## 5. Environment setup

All settings are environment variables (see [.env.example](.env.example)); `.env` is loaded automatically.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `production` | `development`, `production` or `testing` |
| `SECRET_KEY` | random per start | Set a fixed long random value in production |
| `STORAGE_DIR` | `./storage` | Job files location |
| `MAX_UPLOAD_MB` | `100` | Upload size limit (enforced while streaming) |
| `MAX_PAGES` | `2000` | Page limit |
| `CONVERSION_TIMEOUT_SECONDS` | `900` | Hard limit per conversion |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel conversions per web worker process |
| `MAX_IMAGE_PIXELS` | `60000000` | Larger embedded images are skipped (memory guard) |
| `OCR_DPI` / `OCR_PAGE_TIMEOUT_SECONDS` | `300` / `120` | OCR rendering resolution and per-page timeout |
| `TESSERACT_CMD` | (PATH) | Explicit Tesseract binary |
| `EPUBCHECK_PATH` | (PATH) | `epubcheck.jar` (needs Java) or an `epubcheck` executable |
| `JOB_TTL_SECONDS` | `3600` | Jobs older than this are deleted |
| `CLEANUP_INTERVAL_SECONDS` | `300` | Cleanup thread interval |

## 6. Tesseract installation (optional, for scanned PDFs)

```bash
# Debian/Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-eng
sudo apt install tesseract-ocr-tam          # Tamil; other languages: tesseract-ocr-<code>
# macOS
brew install tesseract tesseract-lang
# Windows: install from https://github.com/UB-Mannheim/tesseract/wiki and set TESSERACT_CMD
```

Check with `tesseract --list-langs`. The book language you choose in the UI is mapped to Tesseract data (for example `ta` → `tam+eng`). If that language isn't installed, OCR falls back to English and the result includes a warning.

### Optional: EPUBCheck

```bash
# needs Java 11+
curl -LO https://github.com/w3c/epubcheck/releases/download/v5.4.0/epubcheck-5.4.0.zip
unzip epubcheck-5.4.0.zip -d tools/
echo "EPUBCHECK_PATH=$PWD/tools/epubcheck-5.4.0/epubcheck.jar" >> .env
```

## 7. Running the application

Development:

```bash
uv run python run.py            # http://127.0.0.1:5000  (HOST/PORT env vars to change)
```

Remove expired jobs manually: `uv run flask --app "app:create_app()" cleanup [--ttl 0]`.

## 8. API

All API responses are JSON with `success` and `message`. Errors also carry a machine-readable `code`.

| Method | Endpoint | Purpose | Success |
|---|---|---|---|
| GET | `/` | Single-page UI | 200 |
| GET | `/api/health` | OCR / EPUBCheck availability, limits | 200 |
| POST | `/api/upload` | Multipart field `file`. Validates and analyzes the PDF, creates a job | 201 |
| GET | `/api/preview/<job_id>` | Metadata and analysis (page count, title, text/scan detection) | 200 |
| POST | `/api/convert/<job_id>` | JSON settings (below). Queues the conversion | 202 |
| GET | `/api/status/<job_id>` | Status, per-stage progress, warnings, validation, `download_url` when complete | 200 |
| GET | `/api/download/<job_id>` | The EPUB (`application/epub+zip`, attachment) | 200 |
| DELETE | `/api/job/<job_id>` | Cancel if running; delete all job files | 200 |

Conversion settings (all optional):

```json
{
  "output_filename": "my-book.epub", "epub_version": "3", "language": "en",
  "title": "", "author": "",
  "enable_ocr": true, "extract_images": true, "preserve_original_images": true,
  "image_quality": "high", "detect_headings": true, "generate_toc": true,
  "remove_headers_footers": true, "extract_tables": true, "generate_cover": true
}
```

Status codes:

| Code | Meaning |
|---|---|
| 400 | Invalid file or settings |
| 404 | Unknown or expired job |
| 409 | Already converting, or the EPUB isn't ready yet |
| 413 | Upload too large |
| 507 | Storage failure |
| 500 | Unexpected error (generic message; details are only logged) |

Example status response:

```json
{
  "success": true, "job_id": "…", "status": "processing", "stage": "text",
  "stages": [{"key": "text", "label": "Extracting text", "status": "running", "current": 12, "total": 40,
              "detail": "Page 12 of 40"}, "…"],
  "warnings": []
}
```

## 9. Conversion workflow

```text
upload → validate (signature, parse, encryption, limits) → preview
convert → [subprocess] analyze → extract text per page (+ tables) → detect scanned pages
        → OCR (if needed, enabled and installed) → extract images → remove running headers/footers
        → reading order → paragraphs/lists → headings (outline > fonts > patterns)
        → chapter segmentation → EPUB 3 packaging → structural validation (+ EPUBCheck)
        → output.epub stored → download
```

### Text-based PDFs vs scanned PDFs

- **Text-based PDFs** (exported from Word, LaTeX and so on) contain real characters with font information. Extraction is exact, and headings are detected from fonts. This is fast: about 5 s for a 500-page text PDF.
- **Scanned PDFs** contain only page images. Their text must be *recognised* by OCR:
  - It is slower (seconds per page).
  - Accuracy depends on scan quality and installed language data.
  - Bold and italic are not available.
  - Headings are only inferred from text height, with a stricter threshold.

  Always proofread OCR output. Without OCR, scanned pages are kept as images, so they still display but don't reflow.

## 10. Testing

```bash
uv run pytest                                  # full suite
EPUBCHECK_PATH=/path/to/epubcheck.jar uv run pytest   # also run EPUBCheck on generated books
uv run ruff check . && uv run ruff format --check .
```

The test PDFs are generated by [tests/pdf_factory.py](tests/pdf_factory.py), so no binary fixtures are committed. They cover:
- simple text, a multi-chapter book with lists and running headers, and a page-spanning paragraph
- images: PNG, JPEG, alpha, duplicates, tiny images and CMYK
- scanned pages, encrypted, corrupted, empty and blank files
- Tamil + English, two-column layout, ruled tables, bookmarks, and 25- and 500-page documents

The suite also covers the API end to end through the real subprocess job manager, with the download checked by reading it back with EbookLib. Tests that need Tesseract or EPUBCheck are skipped automatically when those tools are missing.

## 11. Production deployment

Do not use `run.py` (Flask's development server) in production.

```bash
uv sync --extra production --no-dev
APP_ENV=production SECRET_KEY=... uv run gunicorn "app:create_app()" \
    --workers 2 --threads 4 --timeout 120 --bind 127.0.0.1:8000
```

- Put a reverse proxy in front, such as nginx. Set `client_max_body_size` to at least `MAX_UPLOAD_MB` and terminate TLS there.
- Job state is stored on disk, so any worker can answer status and download requests. Each worker runs up to `MAX_CONCURRENT_JOBS` conversions, so total concurrency is `workers × MAX_CONCURRENT_JOBS`. Size this to your CPU and RAM: allow roughly 300–500 MB per running conversion of a large book.
- All workers and hosts must share `STORAGE_DIR`. For multiple hosts or high volume, replace the in-process queue ([app/services/job_manager.py](app/services/job_manager.py)) with Celery + Redis. The pipeline entry point `run_conversion(job_dir, options, settings)` is already process-independent.
- Run the service as an unprivileged user with write access only to `STORAGE_DIR`.

## 12. Known limitations

- PDF is a fixed-layout format, so **perfect reproduction of the original layout is not possible**. The goal is a correct, readable, reflowable book.
- Multi-column detection handles common one- and two-column layouts. Magazine-style layouts, sidebars and text flowing around images may be read in a different order.
- Only **ruled tables** (drawn with lines) are detected. Tables built from whitespace alignment come out as paragraphs, and merged cells are flattened.
- Vector graphics such as charts and diagrams drawn as paths are not converted to images. Only embedded raster images are extracted.
- Footnotes, sidebars, mathematical formulas and complex typography are converted as plain text.
- Heading detection is heuristic. When a document's typography is unusual, chapters may fall back to page ranges. Bookmarked PDFs give the best structure.
- Some PDFs, often older Indic-language ones, use fonts with broken or missing Unicode maps. Their text extracts as wrong or duplicated characters, and no extractor can repair it. OCR (with the right language installed) is the only alternative.
- OCR quality depends on the scan, and decorative or infographic pages produce noisy text.
- The default in-process queue loses queued jobs if the server restarts. Affected jobs are reported as interrupted.
- Practical limits (defaults): 100 MB, 2000 pages, 15 minutes per conversion.

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| "OCR is unavailable" | Install Tesseract (section 6) or set `TESSERACT_CMD`; restart the app. |
| OCR text is in the wrong language | Install `tesseract-ocr-<lang>` and pick the language in settings. |
| Validation says "EPUBCheck not installed" | Install Java + EPUBCheck and set `EPUBCHECK_PATH`. |
| "The file exceeds the … MB upload limit" | Raise `MAX_UPLOAD_MB` (and the proxy's body size limit). |
| Conversion times out | Raise `CONVERSION_TIMEOUT_SECONDS`, disable OCR, or split the PDF. |
| Storage error (507) | Check that `STORAGE_DIR` exists, is writable and has free space. |
| Chapters are "Pages 1–10" | No reliable headings were found; add bookmarks to the PDF or set heading styles. |
| Garbled characters | The PDF's fonts lack Unicode maps; try OCR on the document. |

## License

MIT, see [LICENSE](LICENSE).
