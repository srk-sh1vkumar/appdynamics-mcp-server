"""
parsers/stack/nodejs.py

Node.js stack trace parser.

Frame formats:
  at ClassName.method (/path/file.js:10:5)
  at functionName (/path/file.js:10:5)
  at /path/file.js:10:5   (anonymous)

Filtering: skip node_modules and node:internal frames.
"""

from __future__ import annotations

import re

from models.types import ParsedStack, StackFrame, StackLanguage

_FRAME_RE = re.compile(
    r"\s*at\s+(?:([\w\.<>$\[\] ]+?)\s+)?"
    r"\(?((?:/|[A-Z]:)[^\):]+\.(?:js|ts|mjs|cjs)):(\d+):(\d+)\)?"
)
_SKIP_PATTERNS = (
    "node_modules", "node:internal", "node:timers", "node:events", "<anonymous>"
)


def _is_app_frame(file_path: str) -> bool:
    return not any(p in file_path for p in _SKIP_PATTERNS)


def parse(stack_trace: str, app_package_prefix: str = "") -> ParsedStack:
    all_frames: list[StackFrame] = []

    for line in stack_trace.splitlines():
        m = _FRAME_RE.search(line)
        if not m:
            continue
        raw_name = (m.group(1) or "anonymous").strip()
        file_path = m.group(2)
        line_num = int(m.group(3))
        is_app = _is_app_frame(file_path)

        if "." in raw_name:
            parts = raw_name.rsplit(".", 1)
            class_name, method_name = parts[0], parts[1]
        else:
            class_name, method_name = "", raw_name

        all_frames.append(
            StackFrame(
                class_name=class_name,
                method_name=method_name,
                file_name=file_path,
                line_number=line_num,
                is_app_frame=is_app,
            )
        )

    app_frames = [f for f in all_frames if f.is_app_frame]
    # Node.js: first non-"at" line is usually the error message
    lines = stack_trace.strip().splitlines()
    first = lines[0].strip() if lines else ""
    caused_by = [first] if first and not first.startswith("at ") else []

    return ParsedStack(
        language=StackLanguage.NODEJS,
        culprit_frame=app_frames[0] if app_frames else None,
        caused_by_chain=caused_by,
        top_app_frames=app_frames[:5],
        full_stack_preview="\n".join(lines[:5]),
    )
