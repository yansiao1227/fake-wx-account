"""微信账号发现。以密钥认证绑定登录进程，不按文件修改时间猜账号。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .crypto import PAGE_SIZE, verify_key
from .errors import DatabaseReadError


@dataclass(frozen=True)
class AccountBinding:
    account_id: str
    account_dir: Path
    db_storage: Path
    pid: int
    version: str
    wxid: str = ""


def cache_directory(config: dict) -> Path:
    configured = str(config.get("db_cache_dir", "") or "")
    if configured:
        return Path(configured).expanduser().resolve()
    from common.utils import expand_path
    from config import conf
    return Path(expand_path(conf().get("agent_workspace", "~/cow"))) / "wechat_desktop_db"


def normalize_root(value: str) -> Path:
    if re.fullmatch(r"[A-Za-z]:", value):
        value += "\\"
    return Path(value).expanduser().resolve()


def _current_database_paths(db_storage: Path, pattern: str) -> list[Path]:
    # 认证和读取必须使用同一目录范围，迁移遗留库不能证明当前登录身份。
    return sorted((path for path in db_storage.rglob(pattern)
                   if all(part.casefold() != "migrate" for part in path.relative_to(db_storage).parts)), key=str)


def _contact_pages(db_storage: Path) -> list[bytes]:
    pages = []
    for contact in _current_database_paths(db_storage, "contact.db"):
        with contact.open("rb") as handle:
            pages.append(handle.read(PAGE_SIZE))
    return pages


def _extract_paths(text: str) -> list[str]:
    text = text.strip().lstrip("\ufeff")
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return [v for k, v in payload.items() if isinstance(v, str)
                    and any(s in k.lower() for s in ("path", "dir", "save"))]
    except (ValueError, TypeError):
        pass
    if re.match(r"^[A-Za-z]:[\\/]", text) and "\n" not in text and "\x00" not in text:
        return [text]
    return re.findall(r"[A-Za-z]:[\\/][^\x00-\x1f\"'\r\n]+", text)


def _configured_roots() -> list[Path]:
    roots = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = Path(os.environ.get(env, ""))
        if not os.environ.get(env):
            continue
        for relative in ("Tencent/xwechat/config", "Tencent/xwechat", "Tencent/WeChat"):
            directory = base / relative
            if not directory.is_dir():
                continue
            for item in directory.iterdir():
                if not item.is_file() or item.stat().st_size > 65536:
                    continue
                raw = item.read_bytes()
                for encoding in ("utf-8-sig", "utf-16", "gbk"):
                    try:
                        roots.extend(normalize_root(value) for value in _extract_paths(raw.decode(encoding)))
                    except (UnicodeError, OSError):
                        continue
    if os.name == "nt":
        import winreg
        for subkey in (r"Software\Tencent\xwechat", r"Software\Tencent\WeChat"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                    index = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        index += 1
                        if isinstance(value, str) and any(s in name.lower() for s in ("path", "dir", "save")):
                            roots.append(normalize_root(value))
            except OSError:
                continue
    roots.extend((Path.home() / "Documents", Path.home()))
    return roots


class DatabaseCatalog:
    def __init__(self, config: dict, *, scanner=None, processes=None):
        self.config = config
        self.scanner = scanner
        self.processes = processes
        self._binding = None
        self._candidates = []

    def account_directories(self) -> list[Path]:
        explicit = str(self.config.get("db_data_dir", "") or "")
        roots = [normalize_root(explicit)] if explicit else _configured_roots()
        accounts = {}
        for root in roots:
            if root.name == "db_storage":
                root = root.parent
            if (root / "db_storage").is_dir():
                accounts[str(root).casefold()] = root
                continue
            for candidate in (root, root / "xwechat_files", root / "WeChat Files"):
                if not candidate.is_dir():
                    continue
                for item in candidate.iterdir():
                    if item.is_dir() and (item / "db_storage").is_dir():
                        accounts[str(item).casefold()] = item
        selected = str(self.config.get("db_account", "") or "")
        result = sorted(accounts.values(), key=lambda path: str(path).casefold())
        if selected:
            result = [path for path in result if path.name == selected or str(path) == selected]
        if not result:
            raise DatabaseReadError("account_not_found", "未找到微信账号目录，请设置 db_data_dir 和 db_account")
        return result

    def resolve(self) -> AccountBinding:
        if self._binding is not None:
            if not self.validate_binding(self._binding):
                self._binding = None
                self._candidates = []
                raise DatabaseReadError("login_process_changed", "微信登录进程已变化，需要重新绑定账号")
            return self._binding
        from .keys import find_weixin_processes, scan_key_candidates
        processes = self.processes if self.processes is not None else find_weixin_processes()
        if not processes:
            raise DatabaseReadError("wechat_not_running", "没有找到已运行的微信进程")
        directories = self.account_directories()
        samples = []
        for directory in directories:
            pages = _contact_pages(directory / "db_storage")
            if pages:
                samples.append((directory, pages))
        scanner = self.scanner or scan_key_candidates
        process_order = {pid: index for index, (pid, _) in enumerate(processes)}
        # 扫描器以集合收集密钥，必须恢复主窗口进程优先的枚举顺序。
        self._candidates = sorted(
            scanner(processes, float(self.config["db_key_scan_timeout_seconds"])),
            key=lambda candidate: process_order.get(candidate[0], len(processes)))
        matches = {}
        for directory, pages in samples:
            for pid, key in self._candidates:
                if any(verify_key(key, page) for page in pages):
                    matches[str(directory)] = (directory, pid)
                    break
        if len(matches) != 1:
            code = "account_ambiguous" if len(matches) > 1 else "key_not_found"
            raise DatabaseReadError(code, "无法唯一认证微信登录账号；请确认登录状态和读取权限")
        directory, pid = next(iter(matches.values()))
        versions = dict(processes)
        identity = hashlib.sha256(str(directory.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
        self._binding = AccountBinding(identity, directory, directory / "db_storage", pid,
            versions.get(pid, "unknown"), re.sub(r"_[A-Za-z0-9]{4}$", "", directory.name))
        return self._binding

    def validate_binding(self, binding: AccountBinding) -> bool:
        from .keys import candidate_is_resident, process_is_alive
        if not process_is_alive(binding.pid):
            return False
        if self.scanner is not None:
            return True  # 合成扫描器的测试账号没有真实进程内存。
        pages = _contact_pages(binding.db_storage)
        if not pages:
            return False
        return any(pid == binding.pid and any(verify_key(key, page) for page in pages)
                   and candidate_is_resident(pid, key)
                   for pid, key in self._candidates)

    def list_databases(self, binding: AccountBinding) -> list[Path]:
        # 发送者资源映射也是消息解析输入；不扫描媒体或朋友圈库。
        return [path for path in _current_database_paths(binding.db_storage, "*.db")
                if path.name in {"contact.db", "session.db", "message_resource.db"}
                or re.fullmatch(r"message_\d+\.db", path.name)]

    @property
    def key_candidates(self):
        return tuple(self._candidates)
