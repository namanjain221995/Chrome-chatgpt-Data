"""Server-side HTML sanitisation (defence in depth).

The extension already sanitises before upload, but the backend must never trust
client input. Anything stored as `sanitized_html` passes through this allowlist
first, so an admin UI can render archived content without script execution.
"""

from __future__ import annotations

import re

import bleach

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "hr",
        "span",
        "div",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "mark",
        "sub",
        "sup",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "blockquote",
        "pre",
        "code",
        "kbd",
        "samp",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "colgroup",
        "col",
        "a",
        "img",
        "figure",
        "figcaption",
        "cite",
        "abbr",
        "time",
    }
)

ALLOWED_ATTRS: dict[str, list[str]] = {
    "*": ["class", "title", "dir", "lang"],
    "a": ["href", "rel", "target"],
    "img": ["src", "alt", "width", "height"],
    "code": ["class", "data-language"],
    "pre": ["class", "data-language"],
    "th": ["colspan", "rowspan", "scope"],
    "td": ["colspan", "rowspan"],
    "col": ["span"],
    "time": ["datetime"],
    "ol": ["start", "type"],
}

ALLOWED_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "mailto"})

_CLEANER = bleach.Cleaner(
    tags=set(ALLOWED_TAGS),
    attributes=ALLOWED_ATTRS,
    protocols=set(ALLOWED_PROTOCOLS),
    strip=True,
    strip_comments=True,
)

_SCRIPTISH = re.compile(r"(?i)(javascript:|vbscript:|data:text/html|<\s*script)")
_WS = re.compile(r"[ \t]+")


def sanitize_html(html: str | None, *, max_length: int = 2_000_000) -> str | None:
    """Return allowlist-sanitised HTML, or None for empty input."""
    if not html:
        return None
    if len(html) > max_length:
        html = html[:max_length]
    cleaned = _CLEANER.clean(html)
    # Belt and braces: even after cleaning, refuse to store scriptish markers.
    if _SCRIPTISH.search(cleaned):
        cleaned = _SCRIPTISH.sub("", cleaned)
    return cleaned or None


def strip_control_characters(text: str) -> str:
    """Remove C0/C1 control characters except tab, newline and carriage return."""
    return "".join(
        ch for ch in text if ch in "\t\n\r" or (ch >= " " and not ("\x7f" <= ch <= "\x9f"))
    )


def clean_plain_text(text: str | None, *, max_length: int = 1_000_000) -> str:
    if not text:
        return ""
    text = strip_control_characters(text)
    text = _WS.sub(" ", text)
    if len(text) > max_length:
        text = text[:max_length]
    return text.strip()
