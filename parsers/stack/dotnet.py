"""
parsers/stack/dotnet.py

.NET/C# stack trace parser.

Frame format:
  at Namespace.Class.Method(args) in /path/File.cs:line 42
  at Namespace.Class.Method(args)   (no source info)

Filtering: skip System.*, Microsoft.*, and mscorlib frames.
"""

from __future__ import annotations

import re

from models.types import ParsedStack, StackFrame, StackLanguage

_FRAME_WITH_SOURCE_RE = re.compile(
    r"\s*at\s+([\w\.<>\[\], ]+)\(.*?\)\s+in\s+(.+):line\s+(\d+)"
)
_FRAME_NO_SOURCE_RE = re.compile(
    r"\s*at\s+([\w\.<>\[\], ]+)\(.*?\)"
)

_SKIP_PREFIXES = (
    "System.", "Microsoft.", "mscorlib.", "Newtonsoft.",
    "AutoMapper.", "NHibernate.", "Castle.",
)


def _is_app_frame(full_name: str, prefix: str = "") -> bool:
    if any(full_name.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if prefix and not full_name.startswith(prefix):
        return False
    return True


def _split_method(full_name: str) -> tuple[str, str]:
    """Split 'Namespace.Class.Method(...)' into (class, method)."""
    name = full_name.split("(")[0].strip()
    if "." in name:
        parts = name.rsplit(".", 1)
        return parts[0], parts[1]
    return "", name


def parse(stack_trace: str, app_package_prefix: str = "") -> ParsedStack:
    all_frames: list[StackFrame] = []

    for line in stack_trace.splitlines():
        m = _FRAME_WITH_SOURCE_RE.search(line)
        if m:
            full_name = m.group(1)
            file_name = m.group(2)
            line_num = int(m.group(3))
            class_name, method_name = _split_method(full_name)
            is_app = _is_app_frame(full_name, app_package_prefix)
            all_frames.append(
                StackFrame(
                    class_name=class_name,
                    method_name=method_name,
                    file_name=file_name,
                    line_number=line_num,
                    is_app_frame=is_app,
                )
            )
            continue

        m2 = _FRAME_NO_SOURCE_RE.search(line)
        if m2:
            full_name = m2.group(1)
            class_name, method_name = _split_method(full_name)
            is_app = _is_app_frame(full_name, app_package_prefix)
            all_frames.append(
                StackFrame(
                    class_name=class_name,
                    method_name=method_name,
                    file_name="",
                    line_number=0,
                    is_app_frame=is_app,
                )
            )

    app_frames = [f for f in all_frames if f.is_app_frame]

    # .NET exception message is usually the first line
    lines = stack_trace.strip().splitlines()
    caused_by: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("at ") and "Exception" in stripped:
            caused_by.append(stripped)

    return ParsedStack(
        language=StackLanguage.DOTNET,
        culprit_frame=app_frames[0] if app_frames else None,
        caused_by_chain=caused_by,
        top_app_frames=app_frames[:5],
        full_stack_preview="\n".join(lines[:5]),
    )
