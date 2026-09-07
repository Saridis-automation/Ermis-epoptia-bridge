"""Conservative filtering for the optional inspection report, never raw logs."""

import json
import math
import re
import unicodedata
from collections import Counter

MAX_REPORT_BYTES = 4096  # Includes the complete JSON object, with ASCII escaping.
MAX_INPUT_CHARS = 65_536
REDACTED = "[REDACTED]"
TRUNCATED = "\n[TRUNCATED]"
SENSITIVE = re.compile(
    r"password|passwd|pwd|token|secret|credential|api[\s_-]*key|"
    r"authorization|bearer|basic\s+|cookie|session|private[\s_-]*key|"
    r"connection[\s_-]*string|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://|"
    r"[a-z][a-z0-9+.-]*://[^\s/]*@|"
    r"\b[A-Z][A-Z0-9_]{2,}\s*[=:]|"
    r"\b(?:sk-|gh[pousr]_|github_pat_|AKIA|ASIA)[A-Za-z0-9_-]+|"
    r"\beyJ[A-Za-z0-9_-]+\.", re.IGNORECASE,
)
VALUES = re.compile(r"[^\s]{16,}")


def _suspicious(match):
    value = match.group()
    entropy = -sum((n / len(value)) * math.log2(n / len(value))
                   for n in Counter(value).values())
    return (entropy >= 3.5 or
            (len(value) >= 24 and any(c.isdigit() for c in value)) or
            bool(re.fullmatch(r"[0-9a-fA-F]{16,}", value)))


def build_report(value, previously_truncated=False):
    """Filter before truncating; ambiguous lines and multiline blocks fail closed.

    Unknown fields never survive. No environment or credential files are read.
    Deliberately over-redacts assignments and secret-related prose.
    """
    if type(value) is not str or len(value) > MAX_INPUT_CHARS:
        return {"text": "[REPORT WITHHELD]", "truncated": True}
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = unicodedata.normalize("NFKC", value)
    value = "".join(c for c in value if c in "\n\t" or
                    not unicodedata.category(c).startswith("C"))
    # Withhold everything after a key/block opener, including unterminated keys.
    value = re.sub(r"-----\s*BEGIN\b[\s\S]*", REDACTED, value, flags=re.I)
    lines = []
    for line in value.splitlines():
        if SENSITIVE.search(line) or any(_suspicious(m) for m in VALUES.finditer(line)):
            # Withhold the suffix too: secrets may continue on unlabelled lines.
            lines.append(REDACTED)
            break
        lines.append(line)
    text = "\n".join(lines)
    report = {"text": text, "truncated": bool(previously_truncated)}
    if len(json.dumps(report, ensure_ascii=True).encode("ascii")) <= MAX_REPORT_BYTES:
        return report
    # Binary search the longest fitting prefix. JSON escaping is included.
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = {"text": text[:mid] + TRUNCATED, "truncated": True}
        if len(json.dumps(candidate, ensure_ascii=True).encode("ascii")) <= MAX_REPORT_BYTES:
            low = mid
        else:
            high = mid - 1
    return {"text": text[:low] + TRUNCATED, "truncated": True}
