"""Log normalizer: collapse repeated lines, then cap length (head 2/3 + tail 1/3)."""

MAX_LOG_CHARS = 8_000   # ~2K tokens


def clean_log(text: str, max_chars: int = MAX_LOG_CHARS) -> str:
    out: list[str] = []
    prev, repeat = None, 0
    for line in text.splitlines():
        if line == prev:
            repeat += 1
            continue
        if repeat:
            out.append(f"  ... (previous line repeated {repeat}x)")
        out.append(line)
        prev, repeat = line, 0
    if repeat:
        out.append(f"  ... (previous line repeated {repeat}x)")
    s = "\n".join(out)
    if len(s) <= max_chars:
        return s
    head, tail = s[: max_chars * 2 // 3], s[-max_chars // 3:]
    return f"{head}\n... [truncated {len(s) - max_chars} chars] ...\n{tail}"
