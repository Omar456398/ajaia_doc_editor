import base64
from email.parser import BytesParser
from email.policy import default as default_email_policy
import io
import json
import mimetypes
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from fastapi import HTTPException, Request

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


async def parse_upload_request(
    request: Request,
) -> tuple[str, str, bytes, dict[str, str]]:
    """
    Parse an uploaded file from either `multipart/form-data` (browser FormData)
    or `application/json` ({ filename, content_type, data_base64, ... }) without
    requiring external multipart dependencies.
    Returns (filename, content_type, file_bytes, extra_fields).
    """
    content_type_header = request.headers.get("content-type", "")
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(status_code=400, detail="Empty upload payload")

    if "multipart/form-data" in content_type_header.lower():
        mime_Blob = (
            f"Content-Type: {content_type_header}\r\n"
            f"MIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8") + raw_body
        msg = BytesParser(policy=default_email_policy).parsebytes(mime_Blob)

        filename = ""
        file_content_type = "application/octet-stream"
        file_bytes = b""
        extra_fields: dict[str, str] = {}

        if msg.is_multipart():
            for part in msg.iter_parts():
                part_filename = part.get_filename()
                field_name = part.get_param("name", header="content-disposition")
                payload = part.get_payload(decode=True) or b""
                if part_filename is not None or field_name == "file":
                    filename = part_filename or "uploaded_file"
                    file_content_type = (
                        part.get_content_type()
                        or mimetypes.guess_type(filename)[0]
                        or "application/octet-stream"
                    )
                    file_bytes = payload
                elif field_name:
                    extra_fields[str(field_name)] = payload.decode(
                        "utf-8", errors="replace"
                    )

        if not filename and not file_bytes:
            raise HTTPException(
                status_code=400, detail="No file found in multipart/form-data request"
            )
        return filename or "uploaded_file", file_content_type, file_bytes, extra_fields

    # Fallback: JSON payload ({ filename, content_type, data_base64 })
    try:
        data: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
        filename = str(data.get("filename") or "uploaded_file")
        b64_str = str(data.get("data_base64") or "")
        if "," in b64_str and b64_str.startswith("data:"):
            b64_str = b64_str.split(",", 1)[1]
        file_bytes = base64.b64decode(b64_str)
        file_content_type = (
            str(data.get("content_type") or "")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        extra_fields = {
            k: str(v)
            for k, v in data.items()
            if k not in ("filename", "content_type", "data_base64") and v is not None
        }
        return filename, file_content_type, file_bytes, extra_fields
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Unable to parse upload payload: {exc}"
        ) from exc


def _extract_docx_run_markdown(run_el: ET.Element) -> str:
    """Convert a WordprocessingML <w:r> element into inline Markdown."""
    chunks: list[str] = []
    for child in run_el:
        if child.tag == f"{W_NS}t":
            chunks.append(child.text or "")
        elif child.tag == f"{W_NS}tab":
            chunks.append("    ")
        elif child.tag in (f"{W_NS}br", f"{W_NS}cr"):
            chunks.append("\n")

    text = "".join(chunks)
    if not text or not text.strip():
        return text

    r_pr = run_el.find(f"{W_NS}rPr")
    if r_pr is None:
        return text

    def _is_enabled(tag_name: str) -> bool:
        el = r_pr.find(f"{W_NS}{tag_name}")
        if el is None:
            return False
        val = el.attrib.get(f"{W_NS}val", "true").lower()
        return val not in ("0", "false", "none")

    is_bold = _is_enabled("b")
    is_italic = _is_enabled("i")
    is_strike = _is_enabled("strike") or _is_enabled("dstrike")

    leading_ws = text[: len(text) - len(text.lstrip())]
    trailing_ws = text[len(text.rstrip()) :]
    core = text.strip()

    if is_strike:
        core = f"~~{core}~~"
    if is_italic:
        core = f"*{core}*"
    if is_bold:
        core = f"**{core}**"

    return f"{leading_ws}{core}{trailing_ws}"


def _convert_docx_paragraph(p_el: ET.Element) -> str | None:
    """Convert a WordprocessingML <w:p> element into a Markdown line."""
    style_val = ""
    is_list_item = False
    p_pr = p_el.find(f"{W_NS}pPr")
    if p_pr is not None:
        p_style = p_pr.find(f"{W_NS}pStyle")
        if p_style is not None:
            style_val = p_style.attrib.get(f"{W_NS}val", "").lower()
        if p_pr.find(f"{W_NS}numPr") is not None or "list" in style_val:
            is_list_item = True

    runs_md: list[str] = []
    for child in p_el:
        if child.tag == f"{W_NS}r":
            runs_md.append(_extract_docx_run_markdown(child))
        elif child.tag == f"{W_NS}hyperlink":
            for sub_r in child.findall(f"{W_NS}r"):
                runs_md.append(_extract_docx_run_markdown(sub_r))

    line_text = "".join(runs_md).strip()
    if not line_text:
        return ""

    if style_val in ("title", "heading1", "heading 1", "h1"):
        return f"# {line_text}"
    if style_val in ("subtitle", "heading2", "heading 2", "h2"):
        return f"## {line_text}"
    if style_val.startswith("heading") or style_val in ("h3", "h4", "h5", "h6"):
        return f"### {line_text}"
    if "quote" in style_val:
        return f"> {line_text}"
    if is_list_item:
        return f"- {line_text}"

    return line_text


def convert_docx_bytes_to_markdown(raw_bytes: bytes) -> str:
    """Parse a .docx OOXML zip archive into clean Markdown text."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError("Missing word/document.xml inside .docx archive")
            xml_bytes = zf.read("word/document.xml")
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid .docx file: {exc}"
        ) from exc

    root = ET.fromstring(xml_bytes)
    body = root.find(f"{W_NS}body")
    if body is None:
        return ""

    blocks: list[str] = []
    for elem in body:
        if elem.tag == f"{W_NS}p":
            md_line = _convert_docx_paragraph(elem)
            if md_line is not None:
                blocks.append(md_line)
        elif elem.tag == f"{W_NS}tbl":
            for row in elem.findall(f"{W_NS}tr"):
                cells: list[str] = []
                for cell in row.findall(f"{W_NS}tc"):
                    cell_parts = [
                        _convert_docx_paragraph(p) or ""
                        for p in cell.findall(f"{W_NS}p")
                    ]
                    cells.append(" ".join(p for p in cell_parts if p).strip())
                if any(cells):
                    blocks.append(" | ".join(cells))

    # Collapse excessive consecutive blank lines to at most 1 blank line
    cleaned_lines: list[str] = []
    prev_blank = False
    for line in blocks:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue
        cleaned_lines.append(line)
        prev_blank = is_blank

    return "\n".join(cleaned_lines).strip()


def convert_doc_bytes_to_markdown(raw_bytes: bytes) -> str:
    """
    Extract readable text/Markdown from a legacy .doc file
    (supports plain-text/HTML/RTF-wrapped .doc files as well as binary OLE2 Word .doc).
    """
    # 1. If the .doc is actually a .docx ZIP archive
    if raw_bytes.startswith(b"PK\x03\x04"):
        return convert_docx_bytes_to_markdown(raw_bytes)

    # 2. If the .doc is plain UTF-8 text or HTML
    try:
        decoded_utf8 = raw_bytes.decode("utf-8")
        if "<html" in decoded_utf8.lower() or "<body" in decoded_utf8.lower():
            stripped = re.sub(r"<[^>]+>", " ", decoded_utf8)
            return re.sub(r"\s+", " ", stripped).strip()
        return decoded_utf8.replace("\r\n", "\n").replace("\r", "\n").strip()
    except UnicodeDecodeError:
        pass

    # 3. Binary OLE2 .doc: extract readable ASCII and UTF-16LE text runs
    if raw_bytes.startswith(b"\xd0\xcf\x11\xe0"):
        utf16_matches = re.findall(rb"(?:[\x20-\x7e\r\n]\x00){6,}", raw_bytes)
        ascii_matches = re.findall(rb"[\x20-\x7e\r\n]{8,}", raw_bytes)

        candidates: list[str] = []
        for m in utf16_matches:
            text = m.decode("utf-16le", errors="ignore").strip()
            if text and not any(
                noise in text
                for noise in (
                    "Root Entry",
                    "WordDocument",
                    "SummaryInformation",
                    "CompObj",
                    "Normal.dot",
                    "Microsoft Word",
                )
            ):
                candidates.append(text)

        if not candidates:
            for m in ascii_matches:
                text = m.decode("latin-1", errors="ignore").strip()
                if text and not any(
                    noise in text
                    for noise in (
                        "Root Entry",
                        "WordDocument",
                        "SummaryInformation",
                        "CompObj",
                        "Normal.dot",
                        "Microsoft Word",
                        "MSWordDoc",
                    )
                ):
                    candidates.append(text)

        merged = "\n\n".join(candidates)
        return merged.replace("\r\n", "\n").replace("\r", "\n").strip()

    # 4. Fallback latin-1 decode
    return (
        raw_bytes.decode("latin-1", errors="replace")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def convert_document_bytes_to_markdown(
    filename: str, raw_bytes: bytes
) -> tuple[str, str]:
    """
    Convert an uploaded .doc, .docx, .md, or .txt file into (title, markdown_content).
    """
    path_obj = Path(filename)
    ext = path_obj.suffix.lower()
    stem_title = path_obj.stem.replace("_", " ").replace("-", " ").strip() or "Uploaded Document"

    if ext not in (".doc", ".docx", ".md", ".txt"):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Please upload a .doc, .docx, .md, or .txt file.",
        )

    if ext == ".docx":
        md_content = convert_docx_bytes_to_markdown(raw_bytes)
    elif ext == ".doc":
        md_content = convert_doc_bytes_to_markdown(raw_bytes)
    else:
        # .md or .txt
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw_bytes.decode("latin-1", errors="replace")
        md_content = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Derive title from first "# Heading" if present, otherwise use filename stem
    title = stem_title
    lines = md_content.split("\n") if md_content else []
    for line in lines:
        trimmed = line.strip()
        if trimmed.startswith("# "):
            extracted = trimmed[2:].strip()
            if extracted:
                title = extracted[:250]
            break

    if not md_content:
        md_content = f"# {title}\n\n"
    elif ext == ".txt" and not md_content.lstrip().startswith("#"):
        md_content = f"# {title}\n\n{md_content}"

    return title, md_content
