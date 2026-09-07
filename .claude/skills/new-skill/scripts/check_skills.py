#!/usr/bin/env python3
"""Проверяет, что скиллы в .claude/skills/ оформлены так, чтобы Claude Code их подхватил."""

import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
FIELD_RE = re.compile(r"^(name|description)\s*:\s*(.*)$", re.MULTILINE)


def check(skill_dir: Path) -> list[str]:
    errors = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        others = [p.name for p in skill_dir.glob("*.md")]
        hint = f" (нашлось: {', '.join(others)})" if others else ""
        return [f"{skill_dir.name}: нет файла SKILL.md{hint}"]

    match = FRONTMATTER_RE.match(skill_md.read_text(encoding="utf-8"))
    if not match:
        return [f"{skill_dir.name}/SKILL.md: нет фронтматтера в начале файла (блок между ---)"]

    fields = dict(FIELD_RE.findall(match.group(1)))
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()

    if not name:
        errors.append(f"{skill_dir.name}/SKILL.md: во фронтматтере нет поля name")
    elif not NAME_RE.match(name):
        errors.append(f"{skill_dir.name}/SKILL.md: name '{name}' не в kebab-case")
    elif name != skill_dir.name:
        errors.append(f"{skill_dir.name}/SKILL.md: name '{name}' не совпадает с именем папки")

    if not description:
        errors.append(f"{skill_dir.name}/SKILL.md: во фронтматтере нет поля description")

    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    skill_dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))

    if not skill_dirs:
        print(f"В {root} нет ни одной папки скилла — Claude Code ничего не подхватит.")
        return 1

    errors = [e for d in skill_dirs for e in check(d)]
    for error in errors:
        print(f"ОШИБКА: {error}")

    ok = len(skill_dirs) - len({e.split("/")[0].split(":")[0] for e in errors})
    print(f"Проверено скиллов: {len(skill_dirs)}, корректных: {ok}")

    stray = [p.name for p in root.glob("*.md") if p.name != "README.md"]
    if stray:
        print(f"ВНИМАНИЕ: файлы {', '.join(stray)} лежат вне папок и скиллами не считаются.")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
