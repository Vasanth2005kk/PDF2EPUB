"""EPUB validation: built-in structural checks plus optional EPUBCheck.

The structural checks verify the container layout, package document, manifest, spine,
navigation, XHTML well-formedness and internal references. They are *not* a substitute for
EPUBCheck, and results always state which kind of validation was performed.
"""

from __future__ import annotations

import json
import logging
import posixpath
import shutil
import subprocess
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from lxml import etree

logger = logging.getLogger(__name__)

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}
MAX_UNCOMPRESSED_BYTES = 2 * 1024**3
MAX_COMPRESSION_RATIO = 200

# Hardened XML parser: no entity expansion, no network access, no huge trees.
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False)


@dataclass
class ValidationReport:
    structural_valid: bool
    structural_errors: list[str] = field(default_factory=list)
    epubcheck_available: bool = False
    epubcheck_ran: bool = False
    epubcheck_valid: bool | None = None
    epubcheck_version: str = ""
    epubcheck_messages: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.epubcheck_ran:
            if self.epubcheck_valid:
                return f"Validated with EPUBCheck {self.epubcheck_version}: no errors."
            return f"EPUBCheck {self.epubcheck_version} reported problems."
        if self.structural_valid:
            return "Structural checks passed (EPUBCheck not installed, full validation not performed)."
        return "Structural checks failed."

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["summary"] = self.summary
        return data


def _parse_xml(data: bytes, name: str, errors: list[str]) -> etree._Element | None:
    try:
        return etree.fromstring(data, parser=_SAFE_PARSER)
    except etree.XMLSyntaxError as exc:
        errors.append(f"{name} is not well-formed XML: {exc}")
        return None


def structural_check(path: Path) -> ValidationReport:
    errors: list[str] = []
    stats: dict[str, int] = {}
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        return ValidationReport(False, [f"Not a readable ZIP archive: {exc}"])

    with archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        total = sum(info.file_size for info in infos)
        compressed = sum(info.compress_size for info in infos) or 1
        if total > MAX_UNCOMPRESSED_BYTES or total / compressed > MAX_COMPRESSION_RATIO:
            return ValidationReport(False, ["Archive expands to an unsafe size."])
        for info in infos:
            normalized = posixpath.normpath(info.filename)
            if info.filename.startswith("/") or normalized.startswith(".."):
                errors.append(f"Unsafe path in archive: {info.filename}")

        # mimetype: first entry, stored, exact content.
        if not infos or infos[0].filename != "mimetype":
            errors.append("'mimetype' must be the first file in the archive.")
        elif infos[0].compress_type != zipfile.ZIP_STORED:
            errors.append("'mimetype' must be stored uncompressed.")
        elif archive.read("mimetype") != b"application/epub+zip":
            errors.append("'mimetype' has the wrong content.")

        if "META-INF/container.xml" not in names:
            errors.append("META-INF/container.xml is missing.")
            return ValidationReport(False, errors, stats=stats)
        container = _parse_xml(archive.read("META-INF/container.xml"), "container.xml", errors)
        rootfile = container.find(".//c:rootfile", NS) if container is not None else None
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        if not opf_path or opf_path not in names:
            errors.append("container.xml does not reference an existing package document.")
            return ValidationReport(False, errors, stats=stats)

        opf = _parse_xml(archive.read(opf_path), opf_path, errors)
        if opf is None:
            return ValidationReport(False, errors, stats=stats)
        opf_dir = posixpath.dirname(opf_path)

        if opf.get("version") != "3.0":
            errors.append(f"Package version is {opf.get('version')!r}, expected '3.0'.")
        metadata = opf.find("opf:metadata", NS)
        if metadata is None:
            errors.append("Package metadata is missing.")
        else:
            for tag in ("identifier", "title", "language"):
                element = metadata.find(f"dc:{tag}", NS)
                if element is None or not (element.text or "").strip():
                    errors.append(f"Required metadata dc:{tag} is missing.")
            unique_id = opf.get("unique-identifier")
            if not unique_id or metadata.find(f"dc:identifier[@id='{unique_id}']", NS) is None:
                errors.append("unique-identifier does not reference a dc:identifier.")
            if metadata.find("opf:meta[@property='dcterms:modified']", NS) is None:
                errors.append("Required dcterms:modified metadata is missing.")

        manifest: dict[str, tuple[str, str, str]] = {}  # id -> (href path in zip, media type, properties)
        for item in opf.iterfind("opf:manifest/opf:item", NS):
            href = posixpath.normpath(posixpath.join(opf_dir, unquote(item.get("href", ""))))
            manifest[item.get("id", "")] = (href, item.get("media-type", ""), item.get("properties", ""))
            if href not in names:
                errors.append(f"Manifest item {item.get('href')} does not exist in the archive.")
        manifest_paths = {href for href, _, _ in manifest.values()}

        nav_items = [href for href, _, props in manifest.values() if "nav" in props.split()]
        if len(nav_items) != 1:
            errors.append("Exactly one manifest item must have the 'nav' property.")

        spine = opf.find("opf:spine", NS)
        spine_refs = [ref.get("idref", "") for ref in spine.iterfind("opf:itemref", NS)] if spine is not None else []
        if not spine_refs:
            errors.append("The spine is empty.")
        for ref in spine_refs:
            if ref not in manifest:
                errors.append(f"Spine references unknown manifest id {ref!r}.")

        # Parse every XHTML document; collect ids and references.
        ids: dict[str, set[str]] = {}
        references: list[tuple[str, str, str]] = []  # (document, attribute, target)
        xhtml_count = 0
        for href, media_type, _ in manifest.values():
            if media_type != "application/xhtml+xml" or href not in names:
                continue
            xhtml_count += 1
            root = _parse_xml(archive.read(href), href, errors)
            if root is None:
                continue
            if root.find("x:body", NS) is None:
                errors.append(f"{href} has no body element.")
            ids[href] = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
            for el in root.iter(f"{{{NS['x']}}}img"):
                references.append((href, "img", el.get("src", "")))
            for el in root.iter(f"{{{NS['x']}}}a"):
                if el.get("href"):
                    references.append((href, "a", el.get("href", "")))
            for el in root.iter(f"{{{NS['x']}}}link"):
                references.append((href, "link", el.get("href", "")))

        for document, kind, target in references:
            parts = urlsplit(target)
            if parts.scheme or parts.netloc:
                continue  # external link
            target_path = (
                posixpath.normpath(posixpath.join(posixpath.dirname(document), unquote(parts.path)))
                if parts.path
                else document
            )
            if target_path not in names:
                errors.append(f"{document}: {kind} reference '{target}' points to a missing file.")
            elif target_path not in manifest_paths:
                errors.append(f"{document}: {kind} reference '{target}' is not declared in the manifest.")
            elif parts.fragment and parts.fragment not in ids.get(target_path, set()):
                errors.append(f"{document}: link '{target}' points to a missing anchor.")

        stats = {
            "files": len(infos),
            "xhtml_documents": xhtml_count,
            "images": sum(1 for _, mt, _ in manifest.values() if mt.startswith("image/")),
            "spine_items": len(spine_refs),
            "references_checked": len(references),
            "uncompressed_bytes": total,
        }
    return ValidationReport(not errors, errors[:50], stats=stats)


def _epubcheck_command(configured: str | None) -> list[str] | None:
    """Locate EPUBCheck: an explicit jar/executable path, or ``epubcheck`` on PATH."""
    candidate = configured or shutil.which("epubcheck")
    if not candidate:
        return None
    if candidate.endswith(".jar"):
        java = shutil.which("java")
        if not java or not Path(candidate).is_file():
            return None
        return [java, "-jar", candidate]
    return [candidate] if Path(candidate).exists() or shutil.which(candidate) else None


def epubcheck_available(configured: str | None) -> bool:
    return _epubcheck_command(configured) is not None


def run_epubcheck(path: Path, configured: str | None, timeout: int, report: ValidationReport) -> None:
    command = _epubcheck_command(configured)
    if command is None:
        return
    report.epubcheck_available = True
    try:
        completed = subprocess.run(
            [*command, str(path), "--json", "-", "--quiet"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        report.epubcheck_messages.append(f"EPUBCheck did not finish within {timeout}s.")
        return
    except OSError as exc:
        report.epubcheck_messages.append(f"EPUBCheck could not be started: {exc.strerror}")
        return

    try:
        result = json.loads(completed.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        report.epubcheck_messages.append("EPUBCheck output could not be parsed.")
        return

    report.epubcheck_ran = True
    report.epubcheck_version = str(result.get("checker", {}).get("checkerVersion", ""))
    messages = result.get("messages", [])
    problems = [m for m in messages if m.get("severity") in {"FATAL", "ERROR"}]
    warnings = [m for m in messages if m.get("severity") == "WARNING"]
    report.epubcheck_valid = not problems
    for message in (problems + warnings)[:30]:
        locations = message.get("locations") or [{}]
        where = locations[0].get("path", "")
        report.epubcheck_messages.append(
            f"{message.get('severity')} {message.get('ID')}: {message.get('message')}"
            + (f" ({where})" if where else "")
        )


def validate_epub(path: Path, *, epubcheck_path: str | None = None, epubcheck_timeout: int = 180) -> ValidationReport:
    report = structural_check(path)
    run_epubcheck(path, epubcheck_path, epubcheck_timeout, report)
    return report
