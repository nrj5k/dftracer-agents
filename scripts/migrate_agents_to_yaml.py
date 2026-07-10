#!/usr/bin/env python3
"""One-time migration: markdown agents (harness frontmatter) -> YAML templates.

Reads every ``src/dftracer_agents/.agents/agents/*.md``, splits the body into
``## `` sections, factors any section whose (title, body) is byte-identical
across >= MIN_SHARED agents into ``common-sections.yaml``, and writes one
``<name>.yaml`` template per agent. Also rewrites stale references to the old
``.md`` definition path inside the prose.

Run from the repo root:  python scripts/migrate_agents_to_yaml.py [--delete-md]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "src" / "dftracer_agents" / ".agents" / "agents"
MIN_SHARED = 3

# Concrete class -> level, for the handful of agents without model_level.
CLASS_TO_LEVEL = {"haiku": "level_1", "sonnet": "level_2", "opus": "level_4"}


class _LiteralStr(str):
    pass


def _literal_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_LiteralStr, _literal_representer)


def _parse_frontmatter(raw: str, path: Path) -> dict:
    """Line-based frontmatter parse — several files have unquoted ':' in
    descriptions, which strict YAML rejects but the harnesses tolerate."""
    fm: dict = {}
    key = None
    folded: list[str] = []
    for line in raw.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m and not line.startswith(" "):
            if key and folded:
                fm[key] = " ".join(folded).strip()
                folded = []
            k, v = m.group(1), m.group(2).strip()
            if v in (">", ">-", "|", "|-"):
                key = k
            else:
                fm[k] = v
                key = None
        elif key is not None and (line.startswith("  ") or not line.strip()):
            folded.append(line.strip())
        else:
            sys.exit(f"{path}: cannot parse frontmatter line: {line!r}")
    if key and folded:
        fm[key] = " ".join(folded).strip()
    return fm


def parse_md(path: Path):
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        sys.exit(f"{path}: no frontmatter")
    fm = _parse_frontmatter(m.group(1), path)
    body = text[m.end():]
    # Split into sections at '## ' headings; keep any preamble as untitled.
    sections = []
    pieces = re.split(r"(?m)^## (.+)$", body)
    preamble = pieces[0].strip("\n")
    if preamble.strip():
        sections.append(("", preamble))
    for i in range(1, len(pieces), 2):
        title = pieces[i].strip()
        content = pieces[i + 1].strip("\n")
        sections.append((title, content))
    return fm, sections


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    s = re.sub(r"-mandatory.*$", "", s).strip("-")
    return s or "section"


REWRITES = [
    # The definitions are YAML templates now, and re-materialization is agents_sync.
    (r"src/dftracer_agents/\.agents/agents/<this-agent>\.md",
     "src/dftracer_agents/.agents/agents/<this-agent>.yaml"),
    (r"src/dftracer_agents/\.agents/agents/\*\.md",
     "src/dftracer_agents/.agents/agents/*.yaml"),
    (r"re-materialize\s*\(`ensure_agents_setup\(force=True\)`\)",
     "re-render (`agents_sync` MCP tool)"),
]


def rewrite(body: str) -> str:
    for pat, rep in REWRITES:
        body = re.sub(pat, rep, body)
    return body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete-md", action="store_true")
    args = ap.parse_args()

    md_files = sorted(AGENTS.glob("*.md"))
    parsed = {}
    section_count: Counter = Counter()
    for path in md_files:
        fm, sections = parse_md(path)
        sections = [(t, rewrite(b)) for t, b in sections]
        parsed[path] = (fm, sections)
        for t, b in sections:
            if t:  # only titled sections are shareable
                section_count[(t, b)] += 1

    # Factor shared sections.
    shared = {}  # (title, body) -> slug
    used_slugs = set()
    for (title, body), n in section_count.items():
        if n >= MIN_SHARED:
            slug = slugify(title)
            while slug in used_slugs:
                slug += "-x"
            used_slugs.add(slug)
            shared[(title, body)] = slug

    common_doc = {
        "sections": {
            slug: {"title": title, "body": _LiteralStr(body.rstrip("\n") + "\n")}
            for (title, body), slug in sorted(shared.items(), key=lambda kv: kv[1])
        }
    }
    (AGENTS / "common-sections.yaml").write_text(
        "# Shared prompt sections referenced by agent templates via `- include: <name>`.\n"
        "# Edit here once; every agent that includes the section picks up the change\n"
        "# on the next agents_sync.\n"
        + yaml.dump(common_doc, sort_keys=False, width=1000)
    )
    print(f"common-sections.yaml: {len(shared)} shared sections")

    for path, (fm, sections) in parsed.items():
        model_level = fm.get("model_level")
        if not model_level:
            model_level = CLASS_TO_LEVEL.get(str(fm.get("model", "")), None)
        if not model_level:
            sys.exit(f"{path}: cannot determine model_level (model={fm.get('model')!r})")

        doc = {
            "name": fm["name"],
            "description": fm.get("description", "").strip(),
            "model_level": model_level,
        }
        if fm.get("effort"):
            doc["effort"] = fm["effort"]
        if fm.get("isolation"):
            doc["isolation"] = fm["isolation"]
        if fm.get("tools"):
            doc["tools"] = [t.strip() for t in str(fm["tools"]).split(",") if t.strip()]
        if fm.get("skills"):
            doc["skills"] = [s.strip() for s in str(fm["skills"]).split(",") if s.strip()]

        out_sections = []
        for title, body in sections:
            key = (title, body)
            if key in shared:
                out_sections.append({"include": shared[key]})
            else:
                entry = {"title": title, "body": _LiteralStr(body.rstrip("\n") + "\n")}
                out_sections.append(entry)
        doc["sections"] = out_sections

        out = AGENTS / (path.stem + ".yaml")
        out.write_text(yaml.dump(doc, sort_keys=False, width=1000))
        n_inc = sum(1 for s in out_sections if "include" in s)
        print(f"{out.name}: {len(out_sections)} sections ({n_inc} shared)")
        if args.delete_md:
            path.unlink()

    if args.delete_md:
        print(f"deleted {len(md_files)} .md sources")


if __name__ == "__main__":
    main()
