"""通道共享的纯文本分块规则，发送计费与 UIA 使用同一实现。"""

def split_message_text(value: str, limit: int) -> list[str]:
    """Split long replies at readable boundaries without exceeding limit."""
    text = str(value or "").strip()
    limit = max(100, int(limit))
    chunks = []
    while len(text) > limit:
        minimum_cut = max(1, int(limit * 0.6))
        cut = -1
        for marker in ("\n\n", "\n", "。", "！", "？", ";", "；", ",", "，"):
            candidate = text.rfind(marker, minimum_cut, limit)
            if candidate >= minimum_cut:
                candidate += len(marker)
                cut = max(cut, candidate)
        if cut < minimum_cut:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks

