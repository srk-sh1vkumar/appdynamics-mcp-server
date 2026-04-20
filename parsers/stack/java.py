"""
parsers/stack/java.py

Java stack trace parser.

Frame format:   at com.example.Service.method(Service.java:42)
Caused by:      Caused by: java.lang.NullPointerException: msg

Filtering:
- Skip any frame whose class does NOT start with app_package_prefix.
- Unconditionally skip common framework prefixes even when no prefix is set.
"""

from __future__ import annotations

import re

from models.types import ParsedStack, StackFrame, StackLanguage

_FRAME_RE = re.compile(
    r"\s*at\s+([\w\$\.]+)\.([\w\$<>]+)\(([\w\.]+):(\d+)\)"
)
_CAUSED_BY_RE = re.compile(r"Caused by:\s*(.+)")

_FRAMEWORK_PREFIXES = (
    "java.", "javax.", "sun.", "com.sun.", "jdk.",
    "org.springframework.", "org.hibernate.", "org.apache.",
    "io.netty.", "ch.qos.", "org.slf4j.", "com.zaxxer.",
    "org.jboss.", "reactor.", "io.undertow.", "org.glassfish.",
)


def _is_app_frame(class_name: str, prefix: str) -> bool:
    if any(class_name.startswith(p) for p in _FRAMEWORK_PREFIXES):
        return False
    if prefix and not class_name.startswith(prefix):
        return False
    return True


def parse(stack_trace: str, app_package_prefix: str = "") -> ParsedStack:
    all_frames: list[StackFrame] = []

    for line in stack_trace.splitlines():
        m = _FRAME_RE.match(line)
        if not m:
            continue
        class_name = m.group(1)
        method_name = m.group(2)
        file_name = m.group(3)
        line_num = int(m.group(4))
        is_app = _is_app_frame(class_name, app_package_prefix)
        all_frames.append(
            StackFrame(
                class_name=class_name,
                method_name=method_name,
                file_name=file_name,
                line_number=line_num,
                is_app_frame=is_app,
            )
        )

    app_frames = [f for f in all_frames if f.is_app_frame]
    caused_by = [m.group(1).strip() for m in _CAUSED_BY_RE.finditer(stack_trace)]
    preview_lines = stack_trace.strip().splitlines()[:5]

    return ParsedStack(
        language=StackLanguage.JAVA,
        culprit_frame=app_frames[0] if app_frames else None,
        caused_by_chain=caused_by,
        top_app_frames=app_frames[:5],
        full_stack_preview="\n".join(preview_lines),
    )
