"""Import community Agent Skills as Lantern agent packs.

A `SKILL.md` (the Agent Skills folder format: YAML-like front matter with
`name` and `description`, then a Markdown body) becomes a data-only pack dict
that goes through `agentpacks.validate` and the normal install approval.
Scripts and every other file in the folder are never imported: packs never
run code, so they are only listed in the approval as "not imported".
"""
from __future__ import annotations

from dataclasses import dataclass

from . import agentpacks

MAX_SKILL_BYTES = 64 * 1024
MAX_BODY = agentpacks.MAX_INSTRUCTIONS  # instructions are cut to 4000 characters
SOURCE = "imported skill"

# The pack's tools default to this read-only set; the panel lets the user add
# more from Lantern's own tool list before installing.
READ_ONLY_TOOLS = ["list_folders", "list_dir", "search_files", "read_file"]

# Folder entries with these suffixes are reported as "not imported (packs
# never run code)". Anything else besides SKILL.md is ignored silently.
SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".ps1", ".bat", ".cmd", ".rb",
                   ".pl", ".php", ".exe", ".run")


class SkillImportError(ValueError):
    """A SKILL.md file or folder can't become a pack, with the reason."""


@dataclass
class Skill:
    """A parsed skill: front matter plus the Markdown body."""
    name: str
    description: str
    author: str
    version: str
    body: str
    truncated: bool
    skipped: list[str]


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split `---` front matter from the body. Only `key: value` lines are
    read (no new dependency); anything else in the block is ignored."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillImportError("that file has no front matter (it should start with '---')")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise SkillImportError("that file's front matter never ends (a second '---' line is missing)")
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line or not line.strip():
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        fields[key] = value.strip()
    return fields, "\n".join(lines[end + 1:])


def slugify(name: str) -> str:
    """A skill name ('My Cool Skill') as a pack name ('my-cool-skill')."""
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name)
    slug = "-".join(part for part in slug.split("-") if part)[:41]
    if not agentpacks.NAME.match(slug):
        return "imported-skill"
    return slug


def parse_skill(text: str) -> Skill:
    """Parse SKILL.md text. Raises SkillImportError with the reason."""
    if len(text.encode()) > MAX_SKILL_BYTES:
        raise SkillImportError("that skill is over 64 KB; refusing it")
    fields, body = parse_front_matter(text)
    raw_name = fields.get("name", "").strip()
    if not raw_name:
        raise SkillImportError("that skill has no name in its front matter")
    description = fields.get("description", "").strip()[: agentpacks.MAX_TEXT]
    if not description:
        raise SkillImportError("that skill has no description in its front matter")
    body = body.strip()
    if not body:
        raise SkillImportError("that skill has no instructions after its front matter")
    truncated = len(body) > MAX_BODY
    if truncated:
        note = f"\n\n…(cut from {len(body)} characters; the full text stays in the original SKILL.md)"
        body = body[: MAX_BODY - len(note)] + note
    author = fields.get("author", "").strip()[: agentpacks.MAX_TEXT] or "unknown"
    version = fields.get("version", "").strip() or "1.0.0"
    if not agentpacks.VERSION.match(version):
        version = "1.0.0"
    return Skill(slugify(raw_name), description, author, version, body, truncated, [])


def skill_to_pack(skill: Skill, *, tools: list[str] | None = None) -> dict:
    """A parsed skill as a pack dict, validated. Raises SkillImportError."""
    title = skill.name.replace("-", " ").replace("_", " ").title() or skill.name
    pack = {"format": agentpacks.FORMAT, "name": skill.name, "title": title[: agentpacks.MAX_TEXT],
            "description": skill.description, "author": skill.author, "version": skill.version,
            "inputs": [], "goal": skill.description,
            "instructions": skill.body, "tools": list(tools or READ_ONLY_TOOLS)}
    try:
        return agentpacks.validate(pack).as_dict()
    except agentpacks.PackError as e:
        raise SkillImportError(str(e)) from None


def _read_skill_file(policy, path) -> str:
    """Read one SKILL.md through the privacy policy (hidden/private refused)."""
    from .policy import PolicyError
    try:
        size = policy.stat(path).st_size
    except (OSError, PolicyError) as e:
        raise SkillImportError(f"can't read that skill ({e})") from None
    if size > MAX_SKILL_BYTES:
        raise SkillImportError("that skill is over 64 KB; refusing it")
    try:
        return policy.read_text(path)
    except (OSError, PolicyError) as e:
        raise SkillImportError(f"can't read that skill ({e})") from None


def import_text(text: str, *, filename: str = "SKILL.md") -> dict:
    """Import pasted SKILL.md text. Returns the preview the panel shows:
    the validated pack, skipped scripts, and whether the body was cut."""
    from .tools import TOOL_NAMES
    if filename.strip().lower() != "skill.md":
        raise SkillImportError("that is not a SKILL.md file")
    skill = parse_skill(text)
    pack = skill_to_pack(skill)
    return {"pack": pack, "skipped": [], "truncated": skill.truncated,
            "source": SOURCE, "available_tools": sorted(TOOL_NAMES)}


def import_path(policy, path: str) -> dict:
    """Import a SKILL.md file or a folder holding one, through the policy.
    Only the SKILL.md is ever read; scripts are listed, never copied."""
    from .policy import PolicyError
    from .tools import TOOL_NAMES
    try:
        resolved, _ = policy.check_visible(path or "")
    except PolicyError as e:
        raise SkillImportError(f"can't read that skill ({e})") from None
    skipped: list[str] = []
    if resolved.is_dir():
        try:
            entries = policy.list_dir(resolved)
        except (OSError, PolicyError) as e:
            raise SkillImportError(f"can't read that skill ({e})") from None
        names = sorted(p.name for p in entries if p.is_file())
        skill_names = [n for n in names if n.lower() == "skill.md"]
        if not skill_names:
            raise SkillImportError("that folder has no SKILL.md file")
        skipped = [n for n in names if n.lower() != "skill.md"
                   and n.lower().endswith(SCRIPT_SUFFIXES)]
        text = _read_skill_file(policy, resolved / skill_names[0])
    else:
        if resolved.name.lower() != "skill.md":
            raise SkillImportError("that is not a SKILL.md file")
        text = _read_skill_file(policy, resolved)
    skill = parse_skill(text)
    skill.skipped = skipped
    pack = skill_to_pack(skill)
    return {"pack": pack, "skipped": skipped, "truncated": skill.truncated,
            "source": SOURCE, "available_tools": sorted(TOOL_NAMES)}
