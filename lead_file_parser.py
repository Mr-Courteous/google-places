"""
lead_file_parser.py
---------------------
Takes an uploaded leads file (CSV from this app's own export, or the
generated PDF report) and extracts a flat list of {name, email} pairs
ready for verification. Handles multiple emails per row/business
(as produced by the "Scrape Websites" feature, semicolon-separated).
"""

import csv
import io
import re

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _split_emails(raw: str):
    if not raw:
        return []
    parts = re.split(r"[;,]", raw)
    return [p.strip() for p in parts if p.strip() and EMAIL_RE.match(p.strip())]


def parse_csv(file_bytes: bytes):
    """Parse a leads CSV (from this app's own export). Returns list of {name, email}."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out = []

    for row in reader:
        name = (row.get("name") or "").strip()
        emails = set()
        # Cover both our own CSV's columns, in case of manual edits
        for col in ("all_emails", "email", "emails"):
            if col in row:
                emails.update(_split_emails(row[col]))

        for email in emails:
            out.append({"name": name, "email": email})

    return out


def parse_pdf(file_bytes: bytes):
    """
    Parse a leads PDF. Tries structured table extraction first (works
    for the PDF this app itself generates); falls back to scanning all
    text for emails if no usable table is found (name will be blank).
    """
    import pdfplumber

    out = []
    seen_structured = False

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                header = [str(h or "").strip().lower() for h in table[0]]
                if not any("email" in h for h in header):
                    continue

                name_idx = next((i for i, h in enumerate(header) if "name" in h), None)
                email_idx = next((i for i, h in enumerate(header) if "email" in h), None)
                if email_idx is None:
                    continue

                for row in table[1:]:
                    if email_idx >= len(row):
                        continue
                    name = (row[name_idx] or "").strip() if name_idx is not None and name_idx < len(row) else ""
                    emails = _split_emails(row[email_idx] or "")
                    for email in emails:
                        out.append({"name": name, "email": email})
                        seen_structured = True

        if not seen_structured:
            # Fallback: no usable table found anywhere, just grab every
            # email-looking string in the document text.
            all_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            for email in set(EMAIL_RE.findall(all_text)):
                out.append({"name": "", "email": email})

    return out


def parse_upload(filename: str, file_bytes: bytes):
    lower = filename.lower()
    if lower.endswith(".csv"):
        return parse_csv(file_bytes)
    elif lower.endswith(".pdf"):
        return parse_pdf(file_bytes)
    else:
        raise ValueError("Unsupported file type — please upload a .csv or .pdf file.")
