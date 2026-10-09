"""本机私有缓存 ACL 与 Windows 用户范围 DPAPI。"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import DatabaseReadError


def private_directory(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        target.chmod(0o700)
        return target
    try:
        import win32api
        import win32con
        import win32security
        import ntsecuritycon
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
        acl = win32security.ACL()
        flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        for account_sid in (sid, system):
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, flags, ntsecuritycon.FILE_ALL_ACCESS, account_sid)
        win32security.SetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    except Exception as exc:
        raise DatabaseReadError("cache_permissions_failed", "无法限制数据库缓存目录访问权限") from exc
    return target


def protect(data: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise DatabaseReadError("dpapi_unavailable", "密钥持久化仅支持 Windows DPAPI")
    import win32crypt
    return win32crypt.CryptProtectData(data, "WeChat database read key", entropy, None, None, 1)


def unprotect(data: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise DatabaseReadError("dpapi_unavailable", "密钥持久化仅支持 Windows DPAPI")
    import win32crypt
    return win32crypt.CryptUnprotectData(data, entropy, None, None, 1)[1]
