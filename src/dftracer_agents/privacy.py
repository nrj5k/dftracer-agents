"""Anonymization for everything this package persists or publishes.

Memory, lessons, skills and agent definitions are git-tracked and ship to other
people. We want to learn from experience without recording *who* ran it, so any
text on its way into one of those — or out through a pull request — passes
through :func:`anonymize` first.

The substitutions are deliberately conservative: they replace identity and
machine-local location with the shell variables a reader can resolve themselves
(``$USER``, ``$HOME``, ``$PROJECT_ROOT``, ``$LUSTRE_ROOT``), and they collapse
run-specific identifiers (flux job ids, session UUIDs, node hostnames) into
angle-bracket placeholders.

Published bibliography is *not* scrubbed. A paper's author list is a public
reference, not telemetry, and mangling it breaks the citation. Lines carrying a
citation marker are passed through untouched.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Pattern, Tuple

#: Lines matching these are bibliography, not telemetry — never rewritten.
_CITATION_LINE: Pattern[str] = re.compile(
    r"\*\*Citation:\*\*|\*\*Authors:\*\*|^\s*\[\d+\]\s", re.IGNORECASE
)

#: A path segment that is already anonymous. ``/p/lustre5/$USER`` is the desired
#: output, not a leak, so the location patterns below must not re-match it.
_PLACEHOLDER = r"(?!\$|<|\*|\.\.\.)"

#: Ordered: longest / most specific patterns first, so a path is rewritten as a
#: path before its embedded username is rewritten as ``$USER``.
_SUBS: List[Tuple[Pattern[str], str]] = [
    # Absolute, user-rooted filesystem locations. Each requires a concrete
    # (non-placeholder) segment where the username sits.
    (re.compile(rf"/usr/(?:WS|workspace)\d*/{_PLACEHOLDER}[^/\s\"')`]+/dftracer-agents"), "$PROJECT_ROOT"),
    (re.compile(rf"/p/lustre\d+/{_PLACEHOLDER}[^/\s\"')`]+"), "$LUSTRE_ROOT"),
    (re.compile(rf"/p/vast\d+/{_PLACEHOLDER}[^/\s\"')`]+"), "$VAST_ROOT"),
    (re.compile(rf"/g/g\d+/{_PLACEHOLDER}[^/\s\"')`]+"), "$HOME"),
    (re.compile(rf"/usr/(?:WS|workspace)\d*/{_PLACEHOLDER}[^/\s\"')`]+"), "$HOME"),
    (re.compile(rf"/home/{_PLACEHOLDER}[^/\s\"')`]+"), "$HOME"),
    # Identity. Excludes SSH/git remotes (``git@github.com``) and ELF symbol
    # version tags (``_ZSt@GLIBCXX_3.4.26``), neither of which is a person.
    (
        re.compile(
            r"(?<![\w.@])(?!git@)[\w.+-]+@(?![A-Z_])[a-z0-9-]+\.[a-z]{2,}\b"
        ),
        "<redacted-email>",
    ),
    # Run- and machine-specific identifiers.
    (re.compile(r"\borigin_?[Ss]ession_?[Ii]d:.*$", re.MULTILINE), ""),
    (re.compile(r"\bf[0-9][A-Za-z0-9]{10,}\b"), "<flux-jobid>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    # Hostnames like tuolumne1002 -> tuolumne<node>; keep the system name.
    #
    # Narrow on purpose. A bare ``[a-z]{4,}\d{3,4}`` also eats build/wheel tags
    # such as ``rocm710`` in ``torch==2.10.0+rocm710``. So: reject anything
    # glued to a version string by a preceding ``+ = . - _`` or word character,
    # and reject the software prefixes that legitimately carry digits.
    (
        re.compile(
            r"(?<![+=.\-_\w])"
            r"(?!(?:rocm|cuda|hip|gcc|cce|craype|python|torch|rhel|toss|glibc)\d)"
            r"([a-z]{4,})\d{3,4}\b"
        ),
        r"\1<node>",
    ),
]


#: Substitutions that remove identity WITHOUT changing a path's shape. Used for
#: unified diffs, where collapsing ``/usr/WS2/<user>/dftracer-agents/...`` to
#: ``$PROJECT_ROOT/...`` would change the leading component count and silently
#: break ``patch -pN``. Replacing only the username keeps the component count and
#: the line count identical, so the patch stays structurally valid.
_IDENTITY_ONLY: List[Tuple[Pattern[str], str]] = [
    (
        re.compile(
            r"(?<![\w.@])(?!git@)[\w.+-]+@(?![A-Z_])[a-z0-9-]+\.[a-z]{2,}\b"
        ),
        "<redacted-email>",
    ),
    (re.compile(r"\borigin_?[Ss]ession_?[Ii]d:.*$", re.MULTILINE), ""),
    (re.compile(r"\bf[0-9][A-Za-z0-9]{10,}\b"), "<flux-jobid>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    # Replace ONLY the username segment of a home-like path, keeping the path's
    # component count — ``/usr/workspace/alice/x`` -> ``/usr/workspace/$USER/x``.
    # This catches other people's usernames too, which `extra_users` cannot know.
    (re.compile(rf"(/usr/(?:WS|workspace)\d*/){_PLACEHOLDER}[^/\s\"')`]+"), r"\1$USER"),
    (re.compile(rf"(/p/lustre\d+/){_PLACEHOLDER}[^/\s\"')`]+"), r"\1$USER"),
    (re.compile(rf"(/p/vast\d+/){_PLACEHOLDER}[^/\s\"')`]+"), r"\1$USER"),
    (re.compile(rf"(/g/g\d+/){_PLACEHOLDER}[^/\s\"')`]+"), r"\1$USER"),
    (re.compile(rf"(/home/){_PLACEHOLDER}[^/\s\"')`]+"), r"\1$USER"),
    (
        re.compile(
            r"(?<![+=.\-_\w])"
            r"(?!(?:rocm|cuda|hip|gcc|cce|craype|python|torch|rhel|toss|glibc)\d)"
            r"([a-z]{4,})\d{3,4}\b"
        ),
        r"\1<node>",
    ),
]

#: Structure-preserving formats: identity is stripped, path shape is not touched.
STRUCTURED_SUFFIXES = frozenset({".patch", ".diff"})


def _patterns_file() -> Path:
    return Path(__file__).resolve().parent / ".agents" / "workspace" / "privacy_patterns.yaml"


def learned_patterns() -> List[dict]:
    """Corner-case rules the privacy guard discovered in past sessions.

    Kept as data in a git-tracked YAML file rather than as code, so the guard can
    persist a new rule (via the ``privacy_add_pattern`` MCP tool) without editing
    this module, and so the rule ships to everyone who installs the package.
    """
    path = _patterns_file()
    if not path.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        return list(data.get("patterns") or [])
    except Exception:
        return []


def _compiled_learned(preserve_structure: bool) -> List[Tuple[Pattern[str], str]]:
    """Compile the learned rules, skipping path-reshaping ones for diffs."""
    out: List[Tuple[Pattern[str], str]] = []
    for entry in learned_patterns():
        if preserve_structure and not entry.get("structure_safe", True):
            continue
        try:
            out.append((re.compile(entry["regex"]), entry["replacement"]))
        except (KeyError, re.error):
            continue
    return out


#: Heuristics for content that *looks* identifying but no rule matches yet. These
#: never rewrite anything — they feed ``privacy_suspects`` so the guard can find
#: corner cases instead of waiting to be told about them.
#: System directories that are the same on every machine — a path through one of
#: them names software, not a person.
_SYSTEM_DIRS = (
    "local|tce|gapps|global|share|lib|lib64|bin|sbin|include|opt|src|apps|"
    "workspace|WS|projects|tmp"
)

_SUSPECT_PROBES: List[Tuple[str, Pattern[str]]] = [
    # A home-like path whose second segment is not a known system dir.
    (
        "home-like path",
        re.compile(rf"/(?:home|users|Users|nfs|work|scratch)/(?!(?:{_SYSTEM_DIRS})/)[A-Za-z][\w.-]{{2,}}"),
    ),
    # Neither the site segment nor the segment after it may be a system dir:
    # `/usr/tce/packages/` and `/usr/global/tools/` name software, not people.
    (
        "site path with user segment",
        re.compile(
            rf"/(?:p|g|usr)/(?!(?:{_SYSTEM_DIRS})/)[\w.]+/"
            rf"(?!(?:{_SYSTEM_DIRS}|packages|tools|python)/)[A-Za-z][\w.-]{{2,}}/"
        ),
    ),
    # Emails, minus SSH remotes and ELF symbol version tags. `(?<![+\w.@])`
    # also rejects the `+@decorator` lines that show up inside unified diffs.
    (
        "possible email",
        re.compile(r"(?<![+\w.@])(?!git@)[\w.+-]+@(?![A-Z_])[a-z0-9-]+\.[a-z]{2,}\b"),
    ),
    ("ssh url", re.compile(r"(?<![\w.])(?!git@)[\w.-]+@[\w.-]+:[\w./-]+")),
    # Opaque ids must actually look opaque: mixed alnum with digits, not a
    # camelCase config key like `checkpointFileIntervalTime`.
    ("long opaque id", re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[a-zA-Z])[a-f0-9]{16,}\b")),
    # Match the real hostname rule's shape so the probe agrees with the redactor.
    ("hostname-like", re.compile(r"(?<![+=.\-_\w])[a-z]{4,}\d{3,4}\b")),
    # Only flag an IP in a network context, and never loopback/private ranges —
    # otherwise every `HDF5 1.14.3.3` version string looks like an address.
    (
        "ip address",
        re.compile(
            r"(?:https?://|@|host[=: ]|addr[=: ])"
            r"(?!127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
            r"(?:\d{1,3}\.){3}\d{1,3}"
        ),
    ),
    ("api key-ish", re.compile(r"\b(?:sk|ghp|gho|pat)_[A-Za-z0-9]{8,}\b")),
    ("aws key-ish", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # uid=0 is root, not a person.
    ("uid/gid", re.compile(r"\b(?:uid|gid)=(?!0\b)\d{3,}")),
]


def suspects(text: str, extra_users: List[str] | None = None) -> List[Tuple[str, str]]:
    """Return ``(probe_name, matched_text)`` for content that *may* identify.

    A match here is not a leak — it is a candidate. Anything :func:`anonymize`
    already rewrites is filtered out, so what remains is precisely the corner
    cases the current rules miss. The guard reviews these and, when one is real,
    persists a new rule with ``privacy_add_pattern``.
    """
    found: List[Tuple[str, str]] = []
    for line in text.split("\n"):
        if _CITATION_LINE.search(line):
            continue
        # Whatever the existing rules already handle is not a corner case.
        residue = anonymize(line, extra_users=extra_users)
        for name, probe in _SUSPECT_PROBES:
            for m in probe.finditer(residue):
                token = m.group(0)
                if any(ph in token for ph in ("$", "<", "*", "...")):
                    continue
                found.append((name, token))
    return found


def anonymize(
    text: str,
    extra_users: List[str] | None = None,
    preserve_structure: bool = False,
) -> str:
    """Return *text* with identity and machine-local detail removed.

    *extra_users* are additional bare usernames to replace with ``$USER`` — pass
    the current user when it cannot be inferred from a path.

    With *preserve_structure* (use for ``.patch`` / ``.diff``), absolute paths
    keep their shape and only the username inside them is replaced, so hunk
    offsets and ``-pN`` strip counts survive.
    """
    subs = list(_IDENTITY_ONLY if preserve_structure else _SUBS)
    subs.extend(_compiled_learned(preserve_structure))
    for user in extra_users or []:
        if user:
            subs.append((re.compile(rf"\b{re.escape(user)}\b"), "$USER"))

    out = []
    for line in text.split("\n"):
        if _CITATION_LINE.search(line):
            out.append(line)
            continue
        for pattern, repl in subs:
            line = pattern.sub(repl, line)
        out.append(line)
    return "\n".join(out)


def find_identifiers(text: str, extra_users: List[str] | None = None) -> List[str]:
    """Return the identifying substrings *anonymize* would rewrite.

    Useful for asserting that a file is clean without mutating it.
    """
    hits: List[str] = []
    subs = list(_SUBS)
    subs.extend(_compiled_learned(preserve_structure=False))
    for user in extra_users or []:
        if user:
            subs.append((re.compile(rf"\b{re.escape(user)}\b"), "$USER"))
    for line in text.split("\n"):
        if _CITATION_LINE.search(line):
            continue
        for pattern, _ in subs:
            hits.extend(m.group(0) for m in pattern.finditer(line))
    return hits
