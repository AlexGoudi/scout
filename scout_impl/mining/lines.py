"""What kind of line a changed line is: blank, comment or code, judged from the line alone.

Comment markers come from the file name. The test is per line, so the inside of a
``/* ... */`` block that does not start with ``*`` counts as code, and ``#include`` in C
is never a comment because C has no ``#`` comments, and a ``#!/`` shebang is code.

Two lines differ only in whitespace when they match after trimming both ends and shrinking
every run of whitespace to one space, as in ``git diff -b``; adding or removing a separator
is a code change. In Python, YAML and Makefiles the indentation is syntax, so a re-indented
line there is a code change. Whitespace inside a quoted string on the line is part of the
value, so changing it is a code change too; strings that span lines are not tracked.
"""

from __future__ import annotations

import posixpath
import re

QUOTED_RE = re.compile(rb""""(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?""")
SPACE_RE = re.compile(rb"\s+")
SHEBANG = b"#!/"
HASH = (b"#",)
SLASH = (b"//", b"/*", b"*/", b"* ", b"*\t")
JINJA = (b"{#",)
MARKUP = (b"<!--",)
DASHES = (b"--",)

EXTENSION_MARKERS: dict[str, tuple[bytes, ...]] = {
    **dict.fromkeys(
        (".py", ".sh", ".bash", ".mk", ".conf", ".yml", ".yaml", ".ini", ".cfg", ".toml", ".pl", ".pm",
         ".rb", ".service", ".timer", ".socket", ".profile", ".bcm", ".cmake", ".env", ".rules", ".install"),
        HASH,
    ),
    **dict.fromkeys(
        (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".go", ".java", ".js", ".ts", ".yang", ".proto",
         ".rs", ".cs", ".kt", ".scala", ".swift", ".p4"),
        SLASH,
    ),
    **dict.fromkeys((".xml", ".html", ".htm", ".md", ".xsd"), MARKUP),
    **dict.fromkeys((".lua", ".sql"), DASHES),
}
TEMPLATE_EXTENSIONS = (".j2", ".jinja", ".jinja2")
NAME_MARKERS = {"makefile": HASH, "dockerfile": HASH, "cmakelists.txt": HASH, "rules": HASH}
INDENTED_EXTENSIONS = (".py", ".yml", ".yaml", ".mk")
INDENTED_NAMES = ("rules",)


def comment_markers(path: str | None) -> tuple[bytes, ...]:
    """Prefixes that open a comment line in ``path``; extensionless files use ``#``."""
    if not path:
        return ()
    name = posixpath.basename(path).lower()
    stem, extension = posixpath.splitext(name)
    if extension in TEMPLATE_EXTENSIONS:
        inner = comment_markers(stem) if stem else ()
        return JINJA + tuple(marker for marker in inner if marker not in JINJA)
    if name in NAME_MARKERS or name.startswith("makefile"):
        return NAME_MARKERS.get(name, HASH)
    if not extension:
        return HASH
    return EXTENSION_MARKERS.get(extension, ())


def indentation_matters(path: str | None) -> bool:
    """Whether leading whitespace is syntax in ``path``: Python, YAML and Makefiles."""
    if not path:
        return False
    name = posixpath.basename(path).lower()
    stem, extension = posixpath.splitext(name)
    if extension in TEMPLATE_EXTENSIONS:
        return indentation_matters(stem)
    return extension in INDENTED_EXTENSIONS or name in INDENTED_NAMES or name.startswith("makefile")


def line_kind(content: bytes, markers: tuple[bytes, ...]) -> str:
    """``blank``, ``comment`` or ``code`` for one line without its leading ``+`` or ``-``."""
    stripped = content.strip()
    if not stripped:
        return "blank"
    if stripped.startswith(SHEBANG):
        return "code"
    if markers and (stripped.startswith(markers) or (stripped == b"*" and SLASH[0] in markers)):
        return "comment"
    return "code"


def squeeze(content: bytes, keep_indent: bool = False) -> bytes:
    """The trimmed line with each whitespace run outside quotes shrunk to one space.

    ``keep_indent`` keeps the leading whitespace, so that only lines with the same indentation match.
    """
    body = content.strip()
    if b'"' in body or b"'" in body:
        parts, end = [], 0
        for match in QUOTED_RE.finditer(body):
            parts += [SPACE_RE.sub(b" ", body[end:match.start()]), match.group()]
            end = match.end()
        squeezed = b"".join(parts) + SPACE_RE.sub(b" ", body[end:])
    else:
        squeezed = SPACE_RE.sub(b" ", body)
    if keep_indent:
        return content[:len(content) - len(content.lstrip())] + squeezed
    return squeezed
