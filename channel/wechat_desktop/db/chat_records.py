"""合并转发 appmsg 19 的有界、只读正文提取，不下载或猜测媒体内容。

schema 核对来源（仅使用结构，测试内容为合成数据）：
https://github.com/wechaty/wechaty/issues/1679
https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31/internal/model#DataItem
https://pkg.go.dev/github.com/sjzar/chatlog@v0.0.31/internal/model#RecordXML
https://doc.geweapi.com/api-484848631

recorditem 可为 CDATA/转义 XML 字符串或 recordinfo 元素；datatype 1 的
datadesc 才是文字正文，title/desc/info 是卡片元数据，不能用来补正文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree import ElementTree

from channel.wechat_desktop.config import DEFAULT_CONFIG


def empty_chat_record(title="", *, status="unavailable", truncated=False, budget=None):
    title = str(title or "")
    if budget is not None:
        title = _bounded_title(title, budget)
        truncated = truncated or budget.truncated
    return {"title": title, "content": "", "status": status,
            "entries": [], "truncated": bool(truncated)}


def _field(node, *paths):
    for path in paths:
        value = node.findtext(path)
        if value and value.strip():
            return value.strip()
    return ""


def _bounded_title(title, budget):
    clipped = title[:budget.remaining_chars]
    if len(clipped) < len(title):
        budget.truncated = True
    budget.remaining_chars -= len(clipped)
    return clipped


@dataclass
class ChatRecordBudget:
    """一次原生消息解析的共享预算；嵌套记录不重置任何额度。"""

    remaining_messages: int
    remaining_chars: int
    remaining_xml_bytes: int
    max_depth: int
    truncated: bool = False
    invalid: bool = False
    partial: bool = False
    has_text: bool = False
    entries: list = field(default_factory=list)
    lines: list = field(default_factory=list)

    @classmethod
    def from_config(cls, config):
        def setting(key):
            return int(config.get(key, DEFAULT_CONFIG[key]))
        return cls(setting("chat_record_max_messages"), setting("chat_record_max_chars"),
                   setting("chat_record_max_xml_bytes"), setting("chat_record_max_depth"))

    def parse_xml(self, value):
        # UTF-8 最少每个字符占一字节；先检查长度，避免对超额输入再生成大 bytes。
        if len(value) > self.remaining_xml_bytes:
            self.truncated = True
            return None
        size = len(value.encode("utf-8"))
        if size > self.remaining_xml_bytes:
            self.truncated = True
            return None
        self.remaining_xml_bytes -= size
        if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
            self.invalid = True
            return None
        try:
            return ElementTree.fromstring(value)
        except (ElementTree.ParseError, ValueError):
            self.invalid = True
            return None

    def append(self, *, content, sender, time_text, kind, datatype, depth, readable):
        # 输出行和结构化字段共用字符上限，包含发送者、时间和分隔字符。
        separator = "\n" if self.lines else ""
        sender = sender[:self.remaining_chars]
        time_text = time_text[:max(0, self.remaining_chars - len(sender))]
        prefix = ("  " * depth) + (f"[{time_text}] " if time_text else "") + (f"{sender}: " if sender else "")
        available = self.remaining_chars - len(separator) - len(prefix)
        if available <= 0:
            self.truncated = True
            return
        clipped = content[:available]
        if len(clipped) < len(content):
            self.truncated = True
        if not clipped.strip():
            self.partial = True
            return
        line = prefix + clipped
        self.remaining_chars -= len(separator) + len(line)
        self.lines.append(line)
        self.entries.append({"sender_name": sender, "time_text": time_text, "content_type": kind,
                             "content": clipped, "native_type": datatype, "depth": depth,
                             "resolved": bool(readable)})
        self.has_text = self.has_text or bool(readable)


def app_node(content, budget):
    if not content.lstrip().startswith("<"):
        return None
    root = budget.parse_xml(content)
    if root is None:
        return None
    # 仅识别最外层 appmsg；不沿 refermsg/recorditem 向下寻找另一层应用消息。
    return root if root.tag == "appmsg" else root.find("appmsg")


def _record_node(container, budget):
    if container is None:
        return None
    if container.tag == "recordinfo":
        return container
    record = container.find("recordinfo")
    if record is not None:
        return record
    text = (container.text or "").strip()
    if not text.startswith("<"):
        return None
    root = budget.parse_xml(text)
    if root is None:
        return None
    return root if root.tag == "recordinfo" else root.find("recordinfo")


def _walk_record(record, budget, depth):
    if depth > budget.max_depth:
        budget.truncated = True
        return
    datalist = record.find("datalist")
    if datalist is None:
        budget.partial = True
        return
    seen = 0
    for item in datalist:
        if item.tag != "dataitem":
            budget.partial = True
            continue
        if budget.remaining_messages <= 0 or budget.remaining_chars <= 0:
            budget.truncated = True
            break
        budget.remaining_messages -= 1
        seen += 1
        try:
            datatype = int(item.get("datatype") or "0")
        except (ValueError, OverflowError):
            datatype = 0
        if datatype == 17:
            # recordxml/recordinfo 是合并记录内部的嵌套，和 type57 引用层数无关。
            if depth >= budget.max_depth:
                budget.truncated = True
                continue
            nested = _record_node(item.find("recordxml"), budget)
            if nested is None:
                budget.partial = True
            else:
                _walk_record(nested, budget, depth + 1)
            continue
        sender = _field(item, "sourcename", "sendername", "displayname", "dataitemsource/realchatname",
                        "dataitemsource/fromusr")
        time_text = _field(item, "sourcetime", "srcMsgCreateTime")
        if datatype == 1:
            content = _field(item, "datadesc", "content", "text")
            if not content:
                budget.partial = True
                continue
            kind, readable = "text", True
        else:
            # 媒体/链接只给未读取标记，文件名、URL、预览、密钥及本机路径都不输出。
            kind, label = {2: ("image", "图片"), 3: ("voice", "语音"), 4: ("video", "视频"),
                           5: ("share_card", "链接"), 7: ("music", "音乐"), 8: ("file", "文件"),
                           6: ("location", "位置")}.get(datatype, ("unsupported", "不支持的消息"))
            content, readable = f"[{label}，内容未读取]", False
            budget.partial = True
        budget.append(content=content, sender=sender, time_text=time_text, kind=kind,
                      datatype=datatype, depth=depth, readable=readable)
    try:
        declared = int(datalist.get("count") or seen)
    except (ValueError, OverflowError):
        declared = seen
    if declared > seen and not budget.truncated:
        budget.partial = True


def parse_chat_record(app, budget):
    title = _field(app, "title")
    record = _record_node(app.find("recorditem"), budget)
    if record is not None:
        if not title:
            title = _field(record, "title")
    title = _bounded_title(title, budget)
    if record is not None:
        _walk_record(record, budget, 0)
    if budget.has_text and budget.entries:
        status = "partial" if budget.partial or budget.truncated or budget.invalid else "success"
    else:
        status = "invalid" if budget.invalid else "unavailable"
    return {"title": title, "content": "\n".join(budget.lines), "status": status,
            "entries": budget.entries, "truncated": budget.truncated}
