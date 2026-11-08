"""Files that let an interactive Claude Code session edit a project.

``.mcp.json`` registers the editing-tools MCP server for the project folder
and ``CLAUDE.md`` gives Claude Code the editing guide, so running ``claude``
inside the project folder (logged in with your subscription) is enough.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .agent.claude_code import mcp_config
from .agent.prompts import SYSTEM_PROMPT

if TYPE_CHECKING:  # pragma: no cover
    from .project import Project

CLAUDE_MD = """# Video editing project

This folder is an AI Video Editor project. Edit the video only through the `ave` MCP tools (they operate on
`timeline.json`, take snapshots for undo and validate every change); do not edit the JSON files by hand.
Start with `get_project_overview`, then follow the editing guide below. Reply in the user's language.

{guide}
"""


def write_claude_files(project: "Project") -> list[Path]:
    root = project.root
    mcp = root / ".mcp.json"
    mcp.write_text(json.dumps(mcp_config(root), indent=2), encoding="utf-8")
    md = root / "CLAUDE.md"
    md.write_text(CLAUDE_MD.format(guide=SYSTEM_PROMPT), encoding="utf-8")
    return [mcp, md]
