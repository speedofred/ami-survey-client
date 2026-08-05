"""Text normalisation shared by both halves of the system.

This lives on its own, rather than in the API, because the client needs the same
answer *before* the API ever sees the value. Stage markers are buffered locally
while the work is happening - there is no run yet - and the buffer echoes the
marker back to the agent. If the client echoed the raw text and the server later
cleaned it, an agent that emitted `Score &amp; Tier Candidates` would be told its
marker reads `Score &amp; Tier Candidates`, conclude it had corrupted the name,
and send a corrective duplicate. That is not hypothetical: it happened, and the
duplicate is in the record.

Only normalisation lives here. Redaction - stripping the submitter's paths and
usernames - stays with the API, because it is a property of the server's
collection policy rather than of the text.
"""

from __future__ import annotations

import html
import re

# Some MCP clients HTML-escape tool arguments in transit, so "A & B" arrives as
# "A &amp; B" and "<none>" as "&lt;none&gt;".
ENTITY = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9A-Fa-f]+);")
# Tab and newline survive; everything else non-printing does not.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def normalise(raw: str | None) -> str:
    """Trim, undo transport HTML-escaping, and drop control characters.

    Unescaping repeats until it reaches a fixed point, because a value that was
    escaped twice on the way in ("&amp;amp;") would otherwise arrive half-decoded.
    """
    text = (raw or "").strip()
    while ENTITY.search(text):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    return CONTROL.sub("", text)
