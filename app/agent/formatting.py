"""Reply formatting for WhatsApp."""

import re

MAX_MESSAGE_CHARS = 1000


def format_rupees(amount: float | int | str | None) -> str:
    """₹1,299 or ₹1,299.50."""
    if amount is None:
        return "₹?"
    value = float(amount)
    return f"₹{value:,.0f}" if value == int(value) else f"₹{value:,.2f}"


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split a long reply into WhatsApp-sized messages.

    Prefers paragraph boundaries, then line breaks, then sentence ends, then spaces; a single
    word longer than the limit is hard-cut. Short replies come back unchanged as one message.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for piece in _pieces(text, limit):
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(piece) <= limit:
            current += sep + piece
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _pieces(text: str, limit: int) -> list[str]:
    """Paragraphs, each further broken down until it fits the limit."""
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if paragraph:
            pieces.extend(_fit(paragraph, limit, [r"\n", r"(?<=[.!?।])\s+", r"\s+"]))
    return pieces


def _fit(text: str, limit: int, separators: list[str]) -> list[str]:
    if len(text) <= limit:
        return [text]
    if not separators:
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    joiner = "\n" if separators[0] == r"\n" else " "
    out: list[str] = []
    current = ""
    for part in re.split(separators[0], text):
        part = part.strip()
        if not part:
            continue
        candidate = f"{current}{joiner}{part}" if current else part
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current)
        if len(part) <= limit:
            current = part
        else:
            out.extend(_fit(part, limit, separators[1:])[:-1])
            current = _fit(part, limit, separators[1:])[-1]
    if current:
        out.append(current)
    return out
