"""
parsers/stack/python_parser.py

Python stack trace parser.

Frame format:
  File "/path/to/module.py", line 42, in method_name
    code_line_here

Exception line: ExceptionType: message  (last line of traceback)

Filtering: skip site-packages and standard library frames.
"""

from __future__ import annotations

import re

from models.types import ParsedStack, StackFrame, StackLanguage

_FRAME_RE = re.compile(
    r'\s*File "([^"]+)", line (\d+), in (\S+)'
)
_SKIP_PATTERNS = (
    "site-packages",
    "lib/python",
    "lib\\python",
    "/usr/lib",
    "<frozen",
    "<string>",
)


def _is_app_frame(file_path: str, prefix: str = "") -> bool:
    if any(p in file_path for p in _SKIP_PATTERNS):
        return False
    if prefix:
        # Python: prefix maps to directory path segment (e.g. "myapp")
        return prefix in file_path
    return True


def parse(stack_trace: str, app_package_prefix: str = "") -> ParsedStack:
    all_frames: list[StackFrame] = []
    lines = stack_trace.strip().splitlines()

    for line in lines:
        m = _FRAME_RE.match(line)
        if not m:
            continue
        file_path = m.group(1)
        line_num = int(m.group(2))
        func_name = m.group(3)
        # Derive class name from file path (best effort)
        class_name = file_path.rsplit("/", 1)[-1].replace(".py", "")
        is_app = _is_app_frame(file_path, app_package_prefix)
        all_frames.append(
            StackFrame(
                class_name=class_name,
                method_name=func_name,
                file_name=file_path,
                line_number=line_num,
                is_app_frame=is_app,
            )
        )

    app_frames = [f for f in all_frames if f.is_app_frame]

    # Last line of a Python traceback is usually the exception type + message
    caused_by: list[str] = []
    if lines:
        last = lines[-1].strip()
        if not last.startswith("File ") and not last.startswith("  "):
            caused_by = [last]

    return ParsedStack(
        language=StackLanguage.PYTHON,
        culprit_frame=app_frames[0] if app_frames else None,
        caused_by_chain=caused_by,
        top_app_frames=app_frames[:5],
        full_stack_preview="\n".join(lines[:5]),
    )
