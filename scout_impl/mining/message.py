"""Commit message parsing and identity redaction.

Everything returned here is free of email addresses and of the names in the redaction
universe: trailers are counted and dropped, addresses are replaced, and names are replaced
before any length cap so a cap can never leave half of one behind.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable

BODY_MAX_CHARS = 20_000
SECTION_MAX_CHARS = 6_000
DEFAULT_AUTHOR_SALT = "scout-commit-1"
EMAIL_PLACEHOLDER = "<email>"
NAME_PLACEHOLDER = "<name>"
UNIVERSE_SINGLE_MIN_CHARS = 5
OWN_SINGLE_MIN_CHARS = 3
MULTI_MIN_CHARS = 5

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"\w+(?:[-'.]\w+)*")
NAME_GAP_RE = re.compile(r"[ \t.()\[\]]+")
PR_SUFFIX_RE = re.compile(r"\(#(\d+)\)\s*$")
PR_SUFFIXES_RE = re.compile(r"(?:\s*\(#\d+\))+\s*$")
PR_REFERENCE_RE = re.compile(r"\bPR\s*#?\s*(\d+)|#(\d+)", re.IGNORECASE)
QUOTED_REVERT_RE = re.compile(r'^\s*revert\s*:?\s*"(?P<inner>.*)"(?:\s*\(#\d+\))*\s*$', re.IGNORECASE)
BARE_REVERT_RE = re.compile(r"^\s*revert\b\s*:?\s*(?P<inner>.*)$", re.IGNORECASE)
REVERTS_SHA_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
REVERTS_PR_RE = re.compile(r"\breverts?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)", re.IGNORECASE)
BRACKET_TAGS_RE = re.compile(r"^\s*((?:\[[^\]\n]+\]\s*)+)")
COLON_TAG_RE = re.compile(r"^([A-Za-z0-9_./-]{1,40}):\s")
TRAILER_RE = re.compile(
    r"^\s*(signed-off-by|co-authored-by|reviewed-by|acked-by|tested-by|reported-by|suggested-by|helped-by|cc)\s*:",
    re.IGNORECASE,
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
EMPTY_FENCE_RE = re.compile(r"^```[^\n]*\n```[ \t]*$\n?", re.MULTILINE)
HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*|\*\*\s*-?\s*)(?P<title>[^#*\n]+?)\s*(?:\*\*)?\s*:?\s*$")
# "* 35fb54fd - (HEAD -> master) [fdbsyncd]: Validate (#4847) (6 hours ago) [Author Name]"
PULLED_LINE_RE = re.compile(r"^\s*\*?\s*(?=[0-9a-f]*\d)[0-9a-f]{7,40}\s+(?:-\s+)?\S")
PULLED_TAIL_RE = re.compile(r"\s+\([^()\n]*\bago\)\s+\[[^\]\n]*\]\s*$")

SECTION_TITLES = {
    "why i did it": "why",
    "what i did": "why",
    "what i did it": "why",
    "how i did it": "how",
    "how to verify it": "verify",
    "how to verify": "verify",
}
SECTIONS = ("why", "how", "verify")


@dataclass(frozen=True)
class Message:
    subject: str
    body: str
    body_truncated: bool
    body_length: int
    pr_number: int | None
    subject_tags: tuple[str, ...]
    sections: dict[str, str | None]
    trailer_counts: dict[str, int]
    revert_depth: int
    reverts_sha: str | None
    reverts_pr: int | None
    pulled_commit_lines: int


class Redactor:
    """Replaces email addresses and known names in free text.

    Multi-word names match case-insensitively with only spaces, tabs, dots or brackets between
    their words. Single-word names match exactly, and only when long enough not to be an
    ordinary word; ``stoplist`` holds identities that are not people, such as "GitHub". A
    possessive "'s" after a name survives its placeholder.
    """

    def __init__(self, names: Iterable[str] = (), stoplist: Iterable[str] = ()) -> None:
        self._stoplist = frozenset(item.lower() for item in stoplist)
        self._single: set[str] = set()
        self._multi: dict[str, list[tuple[str, ...]]] = {}
        for name in names:
            self._add(name, UNIVERSE_SINGLE_MIN_CHARS)
        self._sort()

    @property
    def digest(self) -> str:
        """Identifies the universe, so a record states which names it was redacted against."""
        material = "\n".join(sorted(self._single)) + "\x00" + "\n".join(
            sorted(" ".join(tokens) for group in self._multi.values() for tokens in group)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def with_names(self, names: Iterable[str]) -> "Redactor":
        """A copy that also redacts ``names`` (the commit's own author and committer)."""
        copy = Redactor.__new__(Redactor)
        copy._stoplist = self._stoplist
        copy._single = set(self._single)
        copy._multi = {key: list(value) for key, value in self._multi.items()}
        for name in names:
            copy._add(name, OWN_SINGLE_MIN_CHARS)
        copy._sort()
        return copy

    def __call__(self, text: str) -> str:
        text = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
        if not self._single and not self._multi:
            return text
        tokens = list(TOKEN_RE.finditer(text))
        pieces: list[str] = []
        last = 0
        index = 0
        while index < len(tokens):
            match = tokens[index]
            width, keep = self._match(text, tokens, index)
            if width:
                pieces.append(text[last:match.start()])
                pieces.append(NAME_PLACEHOLDER)
                last = tokens[index + width - 1].end() - keep
                if not keep:
                    span = text[match.start():last]
                    unclosed = span.count("(") + span.count("[") - span.count(")") - span.count("]")
                    while unclosed > 0 and last < len(text) and text[last] in ")]":
                        last += 1
                        unclosed -= 1
                index += width
            else:
                index += 1
        if not pieces:
            return text
        pieces.append(text[last:])
        return "".join(pieces)

    def _match(self, text: str, tokens: list[re.Match[str]], index: int) -> tuple[int, int]:
        """Tokens the name starting at ``index`` spans, and the possessive suffix length to keep."""
        token = tokens[index].group()
        for candidate in self._multi.get(token.lower(), ()):
            width = len(candidate)
            if index + width > len(tokens):
                continue
            end, keep = _possessive(tokens[index + width - 1].group())
            if end.lower() == candidate[-1] and all(
                (offset == width - 1 or tokens[index + offset].group().lower() == candidate[offset])
                and NAME_GAP_RE.fullmatch(text, tokens[index + offset - 1].end(), tokens[index + offset].start())
                for offset in range(1, width)
            ):
                return width, keep
        core, keep = _possessive(token)
        return (1, keep) if core in self._single else (0, 0)

    def _add(self, name: str, single_min: int) -> None:
        name = " ".join(name.split())
        if not name or "@" in name or name.lower() in self._stoplist:
            return
        tokens = TOKEN_RE.findall(name)
        if len(tokens) == 1:
            if len(tokens[0]) >= single_min and tokens[0].lower() not in self._stoplist:
                self._single.add(tokens[0])
        elif len(tokens) > 1 and sum(len(token) for token in tokens) >= MULTI_MIN_CHARS:
            lowered = tuple(token.lower() for token in tokens)
            group = self._multi.setdefault(lowered[0], [])
            if lowered not in group:
                group.append(lowered)

    def _sort(self) -> None:
        for group in self._multi.values():
            group.sort(key=lambda tokens: (-len(tokens), tokens))


def _possessive(token: str) -> tuple[str, int]:
    if len(token) > 2 and token[-2:].lower() == "'s":
        return token[:-2], 2
    return token, 0


def author_id(email: str, salt: str = DEFAULT_AUTHOR_SALT) -> str:
    """A stable pseudonym: salted SHA-256 of the normalized email, 16 hex digits."""
    digest = hashlib.sha256(f"{salt}\x00{email.strip().lower()}".encode("utf-8")).hexdigest()
    return digest[:16]


def is_bot(name: str, email: str, bots: Iterable[str]) -> bool:
    identity = (name.lower(), email.lower().partition("@")[0])
    return any(bot in part for bot in bots for part in identity)


def split_message(message: str) -> tuple[str, str]:
    """Git's subject (first paragraph, lines joined) and the body after it."""
    text = message.replace("\r\n", "\n").lstrip("\n")
    head, _, body = text.partition("\n\n")
    return " ".join(head.split()), body


def parse_message(message: str, *, redact: Callable[[str], str], count_pulled: bool) -> Message:
    """Parse one commit message. ``count_pulled`` is set when the commit moves a gitlink."""
    raw_subject, raw_body = split_message(message)
    subject = redact(raw_subject)
    depth, innermost, first_inner, bare = _unwrap_reverts(subject)

    lines = HTML_COMMENT_RE.sub("", raw_body).splitlines()
    kept, trailer_counts = _strip_trailers(lines)
    kept = [PULLED_TAIL_RE.sub("", line) for line in kept]
    pulled = sum(1 for line in kept if PULLED_LINE_RE.match(line)) if count_pulled else 0
    body = _normalize_body(redact("\n".join(kept)))

    reverts_sha = reverts_pr = None
    if depth:
        sha_match = REVERTS_SHA_RE.search(body)
        reverts_sha = sha_match.group(1) if sha_match else None
        reverts_pr = _pr_reference(first_inner) if bare else _pr_suffix(first_inner)
        if reverts_pr is None:
            pr_match = REVERTS_PR_RE.search(body)
            reverts_pr = int(pr_match.group(1)) if pr_match else None

    return Message(
        subject=subject,
        body=body[:BODY_MAX_CHARS],
        body_truncated=len(body) > BODY_MAX_CHARS,
        body_length=len(body),
        pr_number=_pr_suffix(subject),
        subject_tags=_subject_tags(innermost),
        sections=_sections(body),
        trailer_counts=trailer_counts,
        revert_depth=depth,
        reverts_sha=reverts_sha,
        reverts_pr=reverts_pr,
        pulled_commit_lines=pulled,
    )


def _unwrap_reverts(subject: str) -> tuple[int, str, str, bool]:
    """``(depth, innermost subject, subject one level in, unquoted)`` of a revert chain.

    An unquoted revert ("Revert PR#12 because ...") loses its own trailing PR numbers, so
    only a reference to the reverted change remains.
    """
    depth = 0
    current = subject.strip()
    first_inner = current
    while True:
        match = QUOTED_REVERT_RE.match(current)
        if match is None:
            break
        depth += 1
        current = match.group("inner").strip()
        if depth == 1:
            first_inner = current
    if depth == 0:
        bare = BARE_REVERT_RE.match(current)
        if bare is not None:
            current = first_inner = PR_SUFFIXES_RE.sub("", bare.group("inner")).strip()
            return 1, current, first_inner, True
    return depth, current, first_inner, False


def _pr_suffix(text: str) -> int | None:
    match = PR_SUFFIX_RE.search(text)
    return int(match.group(1)) if match else None


def _pr_reference(text: str) -> int | None:
    match = PR_REFERENCE_RE.search(text)
    return int(match.group(1) or match.group(2)) if match else None


def _subject_tags(subject: str) -> tuple[str, ...]:
    tags: list[str] = []
    bracket = BRACKET_TAGS_RE.match(subject)
    if bracket is not None:
        tags.extend(re.findall(r"\[([^\]\n]+)\]", bracket.group(1)))
    else:
        colon = COLON_TAG_RE.match(subject)
        if colon is not None:
            tags.append(colon.group(1))
    normalized = (" ".join(tag.split()).lower() for tag in tags)
    return tuple(dict.fromkeys(tag for tag in normalized if tag))


def _strip_trailers(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    counts = {"signed_off_by": 0, "co_authored_by": 0, "other": 0}
    kept = []
    for line in lines:
        match = TRAILER_RE.match(line)
        if match is None:
            kept.append(line)
            continue
        key = match.group(1).lower().replace("-", "_")
        counts[key if key in counts else "other"] += 1
    return kept, counts


def _normalize_body(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = EMPTY_FENCE_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def _sections(body: str) -> dict[str, str | None]:
    collected: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current: str | None = None
    for line in body.splitlines():
        heading = HEADING_RE.match(line)
        if heading is not None and (line.lstrip().startswith("#") or line.lstrip().startswith("**")):
            current = SECTION_TITLES.get(" ".join(heading.group("title").lower().split()).rstrip("?"))
            continue
        if current is not None:
            collected[current].append(line)
    sections: dict[str, str | None] = {}
    for name in SECTIONS:
        text = "\n".join(collected[name]).strip()
        sections[name] = text[:SECTION_MAX_CHARS] if text else None
    return sections
