/* PDF to EPUB Converter - single-page client. */
(() => {
  "use strict";

  const main = document.getElementById("main");
  const MAX_UPLOAD_BYTES = Number(main.dataset.maxUploadMb) * 1024 * 1024;
  const OCR_AVAILABLE = main.dataset.ocrAvailable === "true";
  const POLL_INTERVAL_MS = 800;
  const MAX_POLL_FAILURES = 15;
  const STORAGE_KEY = "pdf2epub.job";

  const $ = (id) => document.getElementById(id);
  const el = {
    live: $("live-status"),
    dropzone: $("dropzone"),
    fileInput: $("file-input"),
    browse: $("browse-button"),
    fileSummary: $("file-summary"),
    fileName: $("file-name"),
    fileSize: $("file-size"),
    removeFile: $("remove-file"),
    uploadProgress: $("upload-progress"),
    uploadLabel: $("upload-label"),
    uploadBar: $("upload-bar"),
    uploadError: $("upload-error"),
    previewSection: $("preview-section"),
    previewNote: $("preview-note"),
    previewFacts: $("preview-facts"),
    previewNotice: $("preview-notice"),
    settingsSection: $("settings-section"),
    form: $("settings-form"),
    outputFilename: $("output-filename"),
    bookTitle: $("book-title"),
    bookAuthor: $("book-author"),
    enableOcr: $("enable-ocr"),
    ocrHint: $("ocr-hint"),
    extractImages: $("extract-images"),
    preserveImages: $("preserve-images"),
    imageQuality: $("image-quality"),
    convertButton: $("convert-button"),
    progressSection: $("progress-section"),
    statusLine: $("status-line"),
    stageList: $("stage-list"),
    connectionNotice: $("connection-notice"),
    cancelButton: $("cancel-button"),
    resultSection: $("result-section"),
    resultFacts: $("result-facts"),
    validationSummary: $("validation-summary"),
    warningsBox: $("warnings-box"),
    warningsSummary: $("warnings-summary"),
    warningsList: $("warnings-list"),
    downloadButton: $("download-button"),
    anotherButton: $("another-button"),
    errorSection: $("error-section"),
    errorMessage: $("error-message"),
    retryButton: $("retry-button"),
    restartButton: $("restart-button"),
  };

  const state = {
    jobId: null,
    file: null,
    uploadXhr: null,
    converting: false,
    pollTimer: null,
    pollFailures: 0,
    canRetry: false,
  };

  // ----- helpers ---------------------------------------------------------------------------

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "";
    const units = ["bytes", "KB", "MB", "GB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${unit === 0 ? value : value.toFixed(value < 10 ? 2 : 1)} ${units[unit]}`;
  }

  function announce(message) {
    el.live.textContent = message;
  }

  function show(section, visible = true) {
    section.hidden = !visible;
  }

  function saveSession() {
    try {
      if (state.jobId) sessionStorage.setItem(STORAGE_KEY, state.jobId);
      else sessionStorage.removeItem(STORAGE_KEY);
    } catch (_) {
      /* storage unavailable: resuming after reload is simply not offered */
    }
  }

  function loadSession() {
    try {
      return sessionStorage.getItem(STORAGE_KEY);
    } catch (_) {
      return null;
    }
  }

  class ApiError extends Error {
    constructor(message, status, network = false) {
      super(message);
      this.status = status;
      this.network = network;
    }
  }

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, { headers: { Accept: "application/json" }, ...options });
    } catch (_) {
      throw new ApiError("Could not reach the server. Check your connection.", 0, true);
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      /* non-JSON error page (e.g. a proxy) */
    }
    if (!response.ok || !data || data.success === false) {
      const message = (data && data.message) || `The server returned an error (${response.status}).`;
      throw new ApiError(message, response.status);
    }
    return data;
  }

  function fact(dl, label, value) {
    if (value === undefined || value === null || value === "") return;
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = String(value);
    wrap.append(dt, dd);
    dl.appendChild(wrap);
  }

  function badge(text, warning = false) {
    const span = document.createElement("span");
    span.className = warning ? "badge badge-warning" : "badge";
    span.textContent = text;
    return span;
  }

  function formatDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { dateStyle: "medium" });
  }

  // ----- file selection and upload ---------------------------------------------------------

  async function hasPdfSignature(file) {
    try {
      const head = new Uint8Array(await file.slice(0, 1024).arrayBuffer());
      const text = String.fromCharCode(...head);
      return text.includes("%PDF-");
    } catch (_) {
      return true; // let the server decide
    }
  }

  async function validateFile(file) {
    if (!file) return "No file selected.";
    if (!/\.pdf$/i.test(file.name)) return "Only PDF files (.pdf) can be converted.";
    if (file.size === 0) return "The selected file is empty.";
    if (file.size > MAX_UPLOAD_BYTES) {
      return `The file is ${formatBytes(file.size)}; the maximum is ${formatBytes(MAX_UPLOAD_BYTES)}.`;
    }
    if (!(await hasPdfSignature(file))) return "The selected file is not a valid PDF document.";
    return null;
  }

  function setUploadError(message) {
    el.uploadError.textContent = message || "";
    show(el.uploadError, Boolean(message));
  }

  function setDropzoneEnabled(enabled) {
    el.dropzone.classList.toggle("is-disabled", !enabled);
    el.dropzone.setAttribute("aria-disabled", String(!enabled));
    el.dropzone.tabIndex = enabled ? 0 : -1;
    el.browse.disabled = !enabled;
  }

  async function selectFile(file) {
    setUploadError("");
    const problem = await validateFile(file);
    if (problem) {
      setUploadError(problem);
      announce(problem);
      return;
    }
    await discardJob();
    state.file = file;
    el.fileName.textContent = file.name;
    el.fileSize.textContent = formatBytes(file.size);
    show(el.fileSummary);
    show(el.dropzone, false);
    upload(file);
  }

  function upload(file) {
    const form = new FormData();
    form.append("file", file, file.name);
    const xhr = new XMLHttpRequest(); // XHR is used here only because fetch() lacks upload progress
    state.uploadXhr = xhr;

    el.uploadBar.value = 0;
    el.uploadLabel.textContent = "Uploading… 0%";
    show(el.uploadProgress);
    resetDownstream();

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      el.uploadBar.value = percent;
      el.uploadLabel.textContent =
        percent < 100 ? `Uploading… ${percent}%` : "Upload complete. Analyzing PDF…";
    });

    xhr.addEventListener("load", () => {
      state.uploadXhr = null;
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (_) {
        /* handled below */
      }
      show(el.uploadProgress, false);
      if (xhr.status >= 200 && xhr.status < 300 && data && data.success) {
        state.jobId = data.job_id;
        saveSession();
        renderPreview(data);
        announce("PDF uploaded and analyzed. Review the settings and start the conversion.");
      } else {
        const message =
          (data && data.message) ||
          (xhr.status === 413 ? "The file is too large." : `Upload failed (${xhr.status || "network error"}).`);
        uploadFailed(message);
      }
    });
    xhr.addEventListener("error", () => {
      state.uploadXhr = null;
      uploadFailed("The upload was interrupted. Check your connection and try again.");
    });
    xhr.addEventListener("abort", () => {
      state.uploadXhr = null;
      show(el.uploadProgress, false);
    });

    xhr.open("POST", "/api/upload");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.send(form);
  }

  function uploadFailed(message) {
    show(el.uploadProgress, false);
    setUploadError(message);
    announce(message);
    state.file = null;
    show(el.fileSummary, false);
    show(el.dropzone);
    el.fileInput.value = "";
  }

  // ----- preview and settings --------------------------------------------------------------

  function renderPreview(data) {
    const preview = data.preview || {};
    const meta = preview.metadata || {};
    el.previewFacts.replaceChildren();

    const kinds = {
      text: ["Text-based PDF", false],
      mixed: ["Text with scanned pages", true],
      scanned: ["Scanned PDF", true],
      "no-text": ["No text layer found", true],
    };
    const [kindLabel, kindWarn] = kinds[preview.document_kind] || ["Unknown", true];

    fact(el.previewFacts, "File", data.filename);
    fact(el.previewFacts, "Size", formatBytes(data.file_size));
    fact(el.previewFacts, "Pages", preview.page_count);
    fact(el.previewFacts, "Document type", badge(kindLabel, kindWarn));
    fact(el.previewFacts, "Title", preview.title || meta.title || "Not set in PDF");
    fact(el.previewFacts, "Author", meta.author || "Not set in PDF");
    fact(el.previewFacts, "Subject", meta.subject);
    fact(el.previewFacts, "Created", formatDate(meta.created));
    fact(el.previewFacts, "Producer", meta.producer || meta.creator);
    fact(el.previewFacts, "PDF version", meta.pdf_version);
    fact(el.previewFacts, "Bookmarks", preview.has_outline ? `${preview.outline_entries} entries` : "None");
    fact(el.previewFacts, "Text extraction", preview.text_extraction_available ? "Available" : "Not available");
    fact(
      el.previewFacts,
      "OCR",
      preview.ocr_recommended
        ? preview.ocr_available
          ? "Recommended (available)"
          : "Recommended (not installed)"
        : "Not required",
    );

    el.previewNote.textContent =
      preview.sampled_pages < preview.page_count
        ? `Text availability estimated from ${preview.sampled_pages} of ${preview.page_count} pages. Nothing has been converted yet.`
        : "Analysis of the uploaded file. Nothing has been converted yet.";

    let notice = "";
    let noticeClass = "alert";
    if (preview.ocr_recommended && !preview.ocr_available) {
      notice =
        "Some pages have no text layer and OCR is not installed on this server. Those pages will be included as images.";
      noticeClass = "alert alert-warning";
    } else if (preview.ocr_recommended) {
      notice = "Some pages appear to be scanned. OCR will be used to recover their text (slower).";
    } else if (preview.was_repaired) {
      notice = "The PDF had structural errors and was repaired while opening. Some content may be missing.";
      noticeClass = "alert alert-warning";
    }
    el.previewNotice.textContent = notice;
    el.previewNotice.className = noticeClass;
    show(el.previewNotice, Boolean(notice));

    el.outputFilename.value = data.default_output_filename || "converted-book.epub";
    el.bookTitle.placeholder = preview.title || "Detected automatically";
    el.bookAuthor.placeholder = meta.author || "From PDF metadata";
    el.bookTitle.value = "";
    el.bookAuthor.value = "";

    show(el.previewSection);
    show(el.settingsSection);
    el.convertButton.disabled = false;
  }

  function syncSettings() {
    el.preserveImages.disabled = !el.extractImages.checked;
    el.imageQuality.disabled = !el.extractImages.checked || el.preserveImages.checked;
  }

  function collectSettings() {
    const data = new FormData(el.form);
    const settings = {
      output_filename: String(data.get("output_filename") || "").trim(),
      epub_version: String(data.get("epub_version") || "3"),
      title: String(data.get("title") || "").trim(),
      author: String(data.get("author") || "").trim(),
      language: String(data.get("language") || "en"),
      image_quality: el.imageQuality.value,
    };
    for (const input of el.form.querySelectorAll("input[type=checkbox]")) {
      settings[input.name] = input.checked && !input.disabled;
    }
    settings.preserve_original_images = el.preserveImages.checked;
    return settings;
  }

  // ----- conversion ------------------------------------------------------------------------

  async function startConversion() {
    if (state.converting || !state.jobId) return; // prevents duplicate requests
    state.converting = true;
    const settings = collectSettings(); // before the form is disabled
    el.convertButton.disabled = true;
    setFormEnabled(false);
    show(el.errorSection, false);
    show(el.resultSection, false);
    try {
      await api(`/api/convert/${encodeURIComponent(state.jobId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(settings),
      });
      showProgress();
      announce("Conversion started.");
      schedulePoll(0);
    } catch (error) {
      if (error.status === 409) {
        // Already running (e.g. a duplicate request from another tab): follow its progress.
        showProgress();
        schedulePoll(0);
        return;
      }
      state.converting = false;
      setFormEnabled(true);
      el.convertButton.disabled = false;
      showError(error.message, error.status !== 404);
    }
  }

  function setFormEnabled(enabled) {
    for (const control of el.form.elements) {
      if (control.id !== "epub-version") control.disabled = !enabled;
    }
    if (enabled) {
      el.enableOcr.disabled = !OCR_AVAILABLE;
      syncSettings();
    }
    el.removeFile.disabled = !enabled;
  }

  function showProgress() {
    el.stageList.replaceChildren();
    el.statusLine.textContent = "Waiting for the server…";
    show(el.connectionNotice, false);
    el.cancelButton.disabled = false;
    show(el.progressSection);
    el.progressSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function schedulePoll(delay = POLL_INTERVAL_MS) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(poll, delay);
  }

  async function poll() {
    if (!state.jobId) return;
    try {
      const data = await api(`/api/status/${encodeURIComponent(state.jobId)}`);
      state.pollFailures = 0;
      show(el.connectionNotice, false);
      renderStages(data);
      if (data.status === "completed") return conversionDone(data);
      if (data.status === "failed" || data.status === "cancelled") return conversionFailed(data);
      schedulePoll();
    } catch (error) {
      if (error.network || error.status >= 500) {
        state.pollFailures += 1;
        if (state.pollFailures <= MAX_POLL_FAILURES) {
          el.connectionNotice.textContent = "Connection to the server was lost. Retrying…";
          show(el.connectionNotice);
          schedulePoll(Math.min(10000, POLL_INTERVAL_MS * 2 ** Math.min(state.pollFailures, 4)));
          return;
        }
      }
      state.converting = false;
      showError(
        error.status === 404 ? "This conversion job no longer exists. Please upload the PDF again." : error.message,
        false,
      );
    }
  }

  const ICONS = {
    done: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5L20 7"/></svg>',
    failed: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7 7 17"/></svg>',
  };
  const STATUS_TEXT = { pending: "Pending", running: "In progress", done: "Done", skipped: "Skipped", failed: "Failed" };

  function renderStages(data) {
    const stages = data.stages || [];
    const running = stages.find((stage) => stage.status === "running");
    if (data.status === "queued") el.statusLine.textContent = data.message || "Waiting for a free conversion slot…";
    else if (running) el.statusLine.textContent = `${running.label}…`;

    const fragment = document.createDocumentFragment();
    for (const stage of stages) {
      const li = document.createElement("li");
      li.className = "stage";
      li.dataset.status = stage.status;

      const icon = document.createElement("span");
      icon.className = "stage-icon";
      icon.innerHTML = ICONS[stage.status] || ""; // static markup only
      const label = document.createElement("span");
      label.className = "stage-label";
      label.textContent = stage.label;
      const sr = document.createElement("span");
      sr.className = "visually-hidden";
      sr.textContent = ` (${STATUS_TEXT[stage.status] || stage.status})`;
      label.appendChild(sr);
      li.append(icon, label);

      if (stage.status === "running" && stage.total > 0) {
        const bar = document.createElement("progress");
        bar.max = stage.total;
        bar.value = Math.min(stage.current, stage.total);
        bar.setAttribute("aria-label", `${stage.label}: ${stage.current} of ${stage.total}`);
        li.appendChild(bar);
      }
      if (stage.detail) {
        const detail = document.createElement("span");
        detail.className = "stage-detail";
        detail.textContent = stage.detail;
        li.appendChild(detail);
      }
      fragment.appendChild(li);
    }
    el.stageList.replaceChildren(fragment);
  }

  function conversionDone(data) {
    state.converting = false;
    show(el.progressSection, false);
    setFormEnabled(true); // allow re-converting the same PDF with different settings
    el.convertButton.disabled = false;
    const result = data.result || {};
    el.resultFacts.replaceChildren();
    fact(el.resultFacts, "EPUB file", data.filename);
    fact(el.resultFacts, "File size", formatBytes(data.epub_size));
    fact(el.resultFacts, "Title", result.title);
    fact(el.resultFacts, "Sections", result.chapters);
    fact(el.resultFacts, "Images", result.images);
    fact(el.resultFacts, "Contents entries", result.toc_entries);

    const validation = data.validation || {};
    el.validationSummary.textContent = validation.summary || "";
    el.validationSummary.classList.toggle(
      "is-valid",
      Boolean(validation.epubcheck_valid || (!validation.epubcheck_ran && validation.structural_valid)),
    );

    const warnings = data.warnings || [];
    el.warningsList.replaceChildren(
      ...warnings.map((text) => {
        const li = document.createElement("li");
        li.textContent = text;
        return li;
      }),
    );
    el.warningsSummary.textContent = `${warnings.length} note${warnings.length === 1 ? "" : "s"} about this conversion`;
    show(el.warningsBox, warnings.length > 0);

    el.downloadButton.href = data.download_url;
    el.downloadButton.setAttribute("download", data.filename || "converted-book.epub");
    show(el.resultSection);
    el.resultSection.scrollIntoView({ behavior: "smooth", block: "start" });
    el.downloadButton.focus({ preventScroll: true });
    announce("Conversion completed. Your EPUB is ready to download.");
  }

  function conversionFailed(data) {
    state.converting = false;
    show(el.progressSection, false);
    showError(data.message || "The conversion failed.", true); // the uploaded PDF is kept, so retry is safe
  }

  function showError(message, canRetry) {
    state.canRetry = Boolean(canRetry && state.jobId);
    el.errorMessage.textContent = message;
    show(el.retryButton, state.canRetry);
    show(el.errorSection);
    setFormEnabled(true);
    el.convertButton.disabled = !state.jobId;
    el.errorSection.scrollIntoView({ behavior: "smooth", block: "start" });
    announce(message);
  }

  async function cancelConversion() {
    if (!state.jobId) return;
    el.cancelButton.disabled = true;
    const jobId = state.jobId;
    clearTimeout(state.pollTimer);
    try {
      await api(`/api/job/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    } catch (_) {
      /* the job may already be gone */
    }
    resetAll();
    announce("Conversion cancelled.");
  }

  // ----- reset -----------------------------------------------------------------------------

  async function discardJob() {
    if (state.uploadXhr) state.uploadXhr.abort();
    clearTimeout(state.pollTimer);
    const jobId = state.jobId;
    state.jobId = null;
    saveSession();
    if (jobId) {
      try {
        await api(`/api/job/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      } catch (_) {
        /* best effort; expired jobs are cleaned up by the server */
      }
    }
  }

  function resetDownstream() {
    for (const section of [el.previewSection, el.settingsSection, el.progressSection, el.resultSection, el.errorSection]) {
      show(section, false);
    }
  }

  function resetAll() {
    clearTimeout(state.pollTimer);
    state.jobId = null;
    state.file = null;
    state.converting = false;
    state.pollFailures = 0;
    saveSession();
    resetDownstream();
    show(el.fileSummary, false);
    show(el.uploadProgress, false);
    setUploadError("");
    show(el.dropzone);
    setDropzoneEnabled(true);
    setFormEnabled(true);
    el.fileInput.value = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ----- resume after reload ---------------------------------------------------------------

  async function resume() {
    const jobId = loadSession();
    if (!jobId) return;
    try {
      const preview = await api(`/api/preview/${encodeURIComponent(jobId)}`);
      state.jobId = jobId;
      el.fileName.textContent = preview.filename;
      el.fileSize.textContent = formatBytes(preview.file_size);
      show(el.fileSummary);
      show(el.dropzone, false);
      renderPreview(preview);
      if (["queued", "processing", "completed", "failed", "cancelled"].includes(preview.status)) {
        state.converting = preview.status === "queued" || preview.status === "processing";
        if (state.converting) setFormEnabled(false);
        showProgress();
        schedulePoll(0);
      }
    } catch (_) {
      state.jobId = null;
      saveSession();
    }
  }

  // ----- events ----------------------------------------------------------------------------

  el.browse.addEventListener("click", (event) => {
    event.stopPropagation();
    el.fileInput.click();
  });
  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      el.fileInput.click();
    }
  });
  el.fileInput.addEventListener("change", () => {
    if (el.fileInput.files && el.fileInput.files[0]) selectFile(el.fileInput.files[0]);
  });

  for (const type of ["dragenter", "dragover"]) {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.add("is-dragover");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    el.dropzone.addEventListener(type, () => el.dropzone.classList.remove("is-dragover"));
  }
  el.dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    const files = event.dataTransfer && event.dataTransfer.files;
    if (!files || files.length === 0) return;
    if (files.length > 1) {
      setUploadError("Please drop a single PDF file.");
      return;
    }
    selectFile(files[0]);
  });
  // Prevent the browser from opening files dropped outside the drop zone.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  el.removeFile.addEventListener("click", async () => {
    await discardJob();
    resetAll();
    announce("File removed.");
  });

  el.extractImages.addEventListener("change", syncSettings);
  el.preserveImages.addEventListener("change", syncSettings);
  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    startConversion();
  });
  el.cancelButton.addEventListener("click", cancelConversion);
  el.retryButton.addEventListener("click", () => {
    show(el.errorSection, false);
    startConversion();
  });
  el.restartButton.addEventListener("click", async () => {
    await discardJob();
    resetAll();
  });
  el.anotherButton.addEventListener("click", () => {
    // The finished job is kept until it expires so the download link stays valid.
    resetAll();
    el.browse.focus();
  });

  // ----- init ------------------------------------------------------------------------------

  if (!OCR_AVAILABLE) {
    el.enableOcr.checked = false;
    el.enableOcr.disabled = true;
    el.ocrHint.textContent = "OCR is unavailable: Tesseract is not installed on the server.";
  } else {
    el.ocrHint.textContent = "Only pages without a text layer are processed with OCR.";
  }
  syncSettings();
  resume();
})();
