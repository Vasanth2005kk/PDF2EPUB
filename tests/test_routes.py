"""HTTP API tests, including an end-to-end run through the background job manager."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
from ebooklib import epub

FAKE_ID = "0123456789abcdef0123456789abcdef"


def _upload(client, path: Path, name: str | None = None, mimetype: str = "application/pdf"):
    with open(path, "rb") as handle:
        data = {"file": (io.BytesIO(handle.read()), name or path.name, mimetype)}
    return client.post("/api/upload", data=data, content_type="multipart/form-data")


def _wait_for(client, job_id: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/status/{job_id}").get_json()
        if data["status"] in {"completed", "failed", "cancelled"}:
            return data
        time.sleep(0.3)
    raise AssertionError("conversion did not finish in time")


def test_index_renders_spa(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "PDF to EPUB Converter" in html and 'id="dropzone"' in html
    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_static_assets(client):
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/css/style.css").status_code == 200


def test_health(client):
    data = client.get("/api/health").get_json()
    assert data["success"] is True
    assert "ocr" in data and "epubcheck_available" in data


def test_upload_and_preview(client, make_pdf):
    response = _upload(client, make_pdf("book_pdf"), "My Book.pdf")
    assert response.status_code == 201
    data = response.get_json()
    assert data["success"] and len(data["job_id"]) == 32
    assert data["filename"] == "My Book.pdf"
    assert data["default_output_filename"] == "My Book.epub"
    assert data["preview"]["page_count"] == 4
    assert data["preview"]["metadata"]["author"] == "Jane Writer"

    preview = client.get(f"/api/preview/{data['job_id']}").get_json()
    assert preview["preview"]["page_count"] == 4
    assert preview["status"] == "uploaded"
    # Server paths are never exposed.
    assert "storage" not in str(preview) and "/tmp" not in str(preview)


@pytest.mark.parametrize(
    ("builder", "name", "message"),
    [
        ("encrypted_pdf", None, "password-protected"),
        ("corrupted_pdf", None, "corrupted"),
        ("simple_text_pdf", "document.txt", "Only PDF files"),
    ],
)
def test_upload_rejects_invalid_files(client, make_pdf, builder, name, message, app):
    response = _upload(client, make_pdf(builder), name)
    assert response.status_code == 400
    data = response.get_json()
    assert data["success"] is False and message in data["message"]
    assert app.extensions["job_storage"].iter_job_ids() == []  # nothing left behind


def test_upload_rejects_bad_signature_and_mime(client, tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf at all")
    response = _upload(client, fake)
    assert response.status_code == 400 and "signature" in response.get_json()["message"]
    response = _upload(client, fake, mimetype="image/png")
    assert response.status_code == 400


def test_upload_empty_and_missing(client, tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert _upload(client, empty).status_code == 400
    response = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert response.status_code == 400 and response.get_json()["success"] is False


def test_upload_too_large(client, tmp_path):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.7\n" + b"0" * (6 * 1024 * 1024))
    response = _upload(client, big)
    assert response.status_code == 413
    assert "upload limit" in response.get_json()["message"]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/preview/{}"), ("get", "/api/status/{}"), ("get", "/api/download/{}"),
    ("post", "/api/convert/{}"), ("delete", "/api/job/{}"),
])  # fmt: skip
@pytest.mark.parametrize("job_id", [FAKE_ID, "not-a-job", "..%2F..%2Fetc%2Fpasswd"])
def test_invalid_job_ids(client, method, path, job_id):
    response = getattr(client, method)(path.format(job_id))
    assert response.status_code == 404
    assert response.get_json()["success"] is False


def test_convert_rejects_bad_settings(client, make_pdf):
    job_id = _upload(client, make_pdf("simple_text_pdf")).get_json()["job_id"]
    response = client.post(f"/api/convert/{job_id}", json={"language": "not a language"})
    assert response.status_code == 400
    response = client.post(f"/api/convert/{job_id}", data="{broken", content_type="application/json")
    assert response.status_code == 400
    assert client.get(f"/api/download/{job_id}").status_code == 409  # not converted yet


def test_end_to_end_conversion_and_download(client, make_pdf):
    job_id = _upload(client, make_pdf("book_pdf")).get_json()["job_id"]
    response = client.post(f"/api/convert/{job_id}", json={"output_filename": "Final Book", "language": "en"})
    assert response.status_code == 202
    assert response.get_json()["status_url"] == f"/api/status/{job_id}"

    duplicate = client.post(f"/api/convert/{job_id}", json={})
    assert duplicate.status_code in (409, 202)  # 202 only if the first run already finished

    status = _wait_for(client, job_id)
    assert status["status"] == "completed", status.get("message")
    assert status["filename"] == "Final Book.epub"
    assert status["download_url"] == f"/api/download/{job_id}"
    assert status["validation"]["structural_valid"]
    assert [s["status"] for s in status["stages"]].count("done") >= 5

    download = client.get(status["download_url"])
    assert download.status_code == 200
    assert download.mimetype == "application/epub+zip"
    assert 'filename="Final Book.epub"' in download.headers["Content-Disposition"]
    body = download.get_data()
    assert len(body) == status["epub_size"]
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.namelist()[0] == "mimetype"

    # The downloaded file opens in an EPUB reader library.
    path = Path(client.application.config["STORAGE_DIR"]) / "downloaded.epub"
    path.write_bytes(body)
    book = epub.read_epub(str(path))
    assert book.get_metadata("DC", "title")[0][0] == "The Test Book"
    assert len(book.toc) == 3

    # Delete removes all job files.
    assert client.delete(f"/api/job/{job_id}").status_code == 200
    assert client.get(f"/api/status/{job_id}").status_code == 404
    assert client.get(f"/api/download/{job_id}").status_code == 404


def test_failed_conversion_reports_message(client, make_pdf):
    job_id = _upload(client, make_pdf("blank_pdf")).get_json()["job_id"]
    assert client.post(f"/api/convert/{job_id}", json={}).status_code == 202
    status = _wait_for(client, job_id)
    assert status["status"] == "failed"
    assert "No readable content" in status["message"]
    assert "Traceback" not in str(status)


def test_unknown_route_and_method(client):
    assert client.get("/api/nope").status_code == 404
    response = client.put("/api/upload")
    assert response.status_code == 405 and response.get_json()["success"] is False
