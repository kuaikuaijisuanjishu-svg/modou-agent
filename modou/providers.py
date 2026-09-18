"""模型服务的增删改查。密钥只进 macOS 钥匙串，且**不经过 argv**。

这个模块是三套实现收敛后的唯一一套。收敛前的状况值得记下来，因为踩过的坑就在
这里：

- `本地工具/model_credentials.py`（仓库外）用 Security.framework 直调钥匙串，
  docstring 第一行就写着「Secrets never enter argv, files, or output」。它是对的。
- 启动器里那段 `setup_model` 用 `security` CLI，只存得下一把 key。
- 后来加的多服务管理也用 `security add-generic-password -w <key>`——**密钥明文
  出现在进程 argv 里**，同机任何进程在那一瞬间都读得到。这是安全退步，不是等价
  实现。

所以钥匙串这一层照搬第一套的做法：`SecKeychain*` 直调、写完回读校验、缓冲区清零。
多服务管理（增删改切换）是第二、三套带来的能力，保留。

**读取兼容三种历史格式**，免得已经配好的人被要求重输：

1. `org.shuimu-yanma.local-model-api` / account=服务 id → profile JSON（当前格式）
2. `modou-shuimu-yancode` / account=$USER → 裸 key（启动器的老条目，只认 deepseek）
3. `modou-shuimu-yancode-<id>` / account=$USER → 裸 key（短暂存在过的中间格式）

写入一律用格式 1。
"""
from __future__ import annotations

import ctypes as C
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SERVICE = b"org.shuimu-yanma.local-model-api"
LEGACY_LAUNCHER_SERVICE = "modou-shuimu-yancode"
LEGACY_PREFIX = "modou-shuimu-yancode"
CONFIG_PATH = Path.home() / ".modou" / "providers.json"
SCHEMA_VERSION = "model-providers-v2"
#: 钥匙串条目大小上限，与旧实现一致；异常大的记录当作损坏。
MAX_RECORD_BYTES = 16384

#: 预置两家。两家一视同仁：存密钥就是存密钥，没有多余的确认步骤。
BUILTIN = (
    {"id": "deepseek", "name": "DeepSeek",
     "base_url": "https://api.deepseek.com/v1", "model_id": "deepseek-v4-pro"},
    {"id": "glm", "name": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model_id": "glm-5.3"},
)


class ProviderError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    model_id: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "base_url": self.base_url, "model_id": self.model_id}


# ------------------------------------------------------------------ 钥匙串

class Keychain:
    """直接调 Security.framework；不把密钥交给任何命令行。"""

    def __init__(self, service: bytes = SERVICE):
        if sys.platform != "darwin":
            raise ProviderError("KEYCHAIN_UNSUPPORTED", "钥匙串仅在 macOS 可用")
        self.service = service
        self.sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = C.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ptr, u32, status = C.c_void_p, C.c_uint32, C.c_int32
        self.sec.SecKeychainFindGenericPassword.argtypes = [
            ptr, u32, C.c_char_p, u32, C.c_char_p,
            C.POINTER(u32), C.POINTER(ptr), C.POINTER(ptr)]
        self.sec.SecKeychainFindGenericPassword.restype = status
        self.sec.SecKeychainAddGenericPassword.argtypes = [
            ptr, u32, C.c_char_p, u32, C.c_char_p, u32, ptr, C.POINTER(ptr)]
        self.sec.SecKeychainAddGenericPassword.restype = status
        self.sec.SecKeychainItemModifyAttributesAndData.argtypes = [ptr, ptr, u32, ptr]
        self.sec.SecKeychainItemModifyAttributesAndData.restype = status
        self.sec.SecKeychainItemDelete.argtypes = [ptr]
        self.sec.SecKeychainItemDelete.restype = status
        self.sec.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
        self.sec.SecKeychainItemFreeContent.restype = status
        self.cf.CFRelease.argtypes = [ptr]
        self.cf.CFRelease.restype = None

    @staticmethod
    def _check(status: int) -> None:
        if status != 0:
            raise ProviderError(
                "KEYCHAIN_FAILED",
                f"钥匙串操作未完成（系统错误码 {int(status)}）；请解锁钥匙串或检查授权。")

    def load(self, account: str):
        raw = account.encode("ascii")
        size, data = C.c_uint32(), C.c_void_p()
        code = self.sec.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(raw), raw,
            C.byref(size), C.byref(data), None)
        if code == -25300:
            return None
        self._check(code)
        try:
            if size.value > MAX_RECORD_BYTES:
                raise ProviderError("KEYCHAIN_RECORD_ODD",
                                    "钥匙串记录大小异常，请重新配置。")
            text = C.string_at(data, size.value).decode("utf-8")
        finally:
            self.sec.SecKeychainItemFreeContent(None, data)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"api_key": text}          # 老格式：裸 key

    def save(self, account: str, value: dict) -> None:
        raw_account = account.encode("ascii")
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        buffer = C.create_string_buffer(raw)
        item = C.c_void_p()
        try:
            code = self.sec.SecKeychainFindGenericPassword(
                None, len(self.service), self.service,
                len(raw_account), raw_account, None, None, C.byref(item))
            if code == -25300:
                code = self.sec.SecKeychainAddGenericPassword(
                    None, len(self.service), self.service,
                    len(raw_account), raw_account, len(raw), buffer, None)
            elif code == 0:
                code = self.sec.SecKeychainItemModifyAttributesAndData(
                    item, None, len(raw), buffer)
            self._check(code)
        finally:
            C.memset(buffer, 0, C.sizeof(buffer))    # 别把明文留在进程内存里
            if item.value:
                self.cf.CFRelease(item)
        if self.load(account) != value:
            raise ProviderError("KEYCHAIN_VERIFY_FAILED",
                                "钥匙串保存后的校验失败；本次不报告配置成功。")

    def delete(self, account: str) -> bool:
        raw = account.encode("ascii")
        item = C.c_void_p()
        code = self.sec.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(raw), raw,
            None, None, C.byref(item))
        if code == -25300:
            return False
        self._check(code)
        try:
            self._check(self.sec.SecKeychainItemDelete(item))
        finally:
            if item.value:
                self.cf.CFRelease(item)
        return True


_store: Keychain | None = None


def keychain() -> Keychain:
    """惰性单例。测试把它换掉，就不必碰真实钥匙串。"""
    global _store
    if _store is None:
        _store = Keychain()
    return _store


def set_keychain(store) -> None:
    global _store
    _store = store


# ------------------------------------------------------------------ 校验

def valid_key(value) -> bool:
    return isinstance(value, str) and 8 <= len(value) <= 4096 and all(
        33 <= ord(char) <= 126 for char in value)


def _valid_id(value: str) -> str:
    value = str(value or "").strip().lower()
    if not value or len(value) > 32 or not all(
            c.isalnum() or c in "-_" for c in value):
        raise ProviderError("PROVIDER_ID_INVALID",
                            "id 只能用字母、数字、- 和 _，且不超过 32 个字符")
    return value


def _valid_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ProviderError("PROVIDER_URL_INVALID", "接口地址要以 http(s):// 开头")
    return value


# ------------------------------------------------------------------ 配置

def load(path: Path = CONFIG_PATH) -> tuple[list[Provider], str]:
    raw: dict = {}
    if Path(path).is_file():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    entries = raw.get("providers")
    if not isinstance(entries, list) or not entries:
        entries = [dict(x) for x in BUILTIN]
    providers: list[Provider] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            providers.append(Provider(
                id=_valid_id(entry.get("id")),
                name=str(entry.get("name") or entry.get("id") or "")[:40],
                base_url=_valid_url(entry.get("base_url")),
                model_id=str(entry.get("model_id") or "")[:80]))
        except ProviderError:
            continue
    selected = str(raw.get("selected") or "")
    if selected not in {p.id for p in providers}:
        selected = providers[0].id if providers else ""
    return providers, selected


def save(providers: list[Provider], selected: str,
         path: Path = CONFIG_PATH) -> None:
    """配置是明文文件，所以它里面只能有非敏感字段。"""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "selected": selected,
               "providers": [p.as_dict() for p in providers]}
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def upsert(provider: Provider, *, path: Path = CONFIG_PATH,
           select: bool = False) -> None:
    providers, selected = load(path)
    providers = [p for p in providers if p.id != provider.id] + [provider]
    save(providers, provider.id if select else selected, path)


def remove(provider_id: str, *, path: Path = CONFIG_PATH,
           forget_key: bool = True) -> None:
    """删掉一家；默认连它的密钥一起删。

    只删配置会在钥匙串里攒下没人记得来历的条目。要保留得显式说。
    """
    provider_id = _valid_id(provider_id)
    providers, selected = load(path)
    kept = [p for p in providers if p.id != provider_id]
    if len(kept) == len(providers):
        raise ProviderError("PROVIDER_NOT_FOUND", provider_id)
    if forget_key:
        delete_key(provider_id)
    if selected == provider_id:
        selected = kept[0].id if kept else ""
    save(kept, selected, path)


# ------------------------------------------------------------------ 密钥

def _legacy_cli_key(service: str) -> str:
    """读启动器留下的老条目。只读不写——写入一律走 Security.framework。"""
    try:
        done = subprocess.run(
            ["security", "find-generic-password",
             "-a", os.environ.get("USER", ""), "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def read_profile(provider_id: str) -> dict:
    """当前格式优先，读不到再回落到两种历史格式。"""
    try:
        record = keychain().load(provider_id)
    except ProviderError:
        record = None
    if isinstance(record, dict) and valid_key(record.get("api_key")):
        return record
    for service in (f"{LEGACY_PREFIX}-{provider_id}",
                    *( (LEGACY_LAUNCHER_SERVICE,) if provider_id == "deepseek" else () )):
        key = _legacy_cli_key(service)
        if valid_key(key):
            return {"api_key": key, "migrated_from": service}
    return {}


def read_key(provider_id: str) -> str:
    return str(read_profile(provider_id).get("api_key") or "")


def write_key(provider_id: str, key: str) -> bool:
    key = str(key).strip()
    if not valid_key(key):
        raise ProviderError("PROVIDER_KEY_INVALID",
                            "密钥应为 8–4096 个可见字符且不含空白")
    providers, _ = load()
    known = {p.id: p for p in providers}
    provider = known.get(provider_id)
    profile = {
        "api_key": key,
        "model_id": provider.model_id if provider else "",
        "endpoint": provider.base_url if provider else "",
        "configured_at": datetime.now(timezone.utc).isoformat(),
    }
    keychain().save(provider_id, profile)
    return True


def delete_key(provider_id: str) -> bool:
    removed = False
    try:
        removed = keychain().delete(provider_id)
    except ProviderError:
        pass
    # 老条目是 CLI 建的，只能用 CLI 删；失败不算错，它可能本来就不存在。
    for service in (f"{LEGACY_PREFIX}-{provider_id}",
                    *((LEGACY_LAUNCHER_SERVICE,) if provider_id == "deepseek" else ())):
        try:
            done = subprocess.run(
                ["security", "delete-generic-password",
                 "-a", os.environ.get("USER", ""), "-s", service],
                capture_output=True, text=True, timeout=10)
            removed = removed or done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            continue
    return removed


# ------------------------------------------------------------------ 验证与一览

def verify(provider: Provider, key: str, *, timeout: float = 25.0) -> dict:
    import urllib.error
    import urllib.request

    body = json.dumps({"model": provider.model_id,
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1}).encode()
    request = urllib.request.Request(
        f"{provider.base_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": response.status == 200, "code": response.status,
                    "message": "连接成功"}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str((payload.get("error") or {}).get("message") or payload)[:160]
        except Exception:                                   # noqa: BLE001
            pass
        return {"ok": False, "code": exc.code,
                "message": _explain(exc.code, provider), "detail": detail}
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "code": 0,
                "message": f"连不上：{type(exc).__name__}——检查网络或代理"}


def _explain(code: int, provider: Provider) -> str:
    if code in (401, 403):
        return "Key 被拒绝：多半是复制少了字符，或这把 key 已失效"
    if code == 404:
        return f"接口或模型不存在：{provider.base_url} 或 {provider.model_id} 对不上"
    if code == 429:
        return "触发限流：稍等几十秒再试，或换一把额度充足的 key"
    return f"接口返回 {code}"


def status(path: Path = CONFIG_PATH) -> list[dict]:
    providers, selected = load(path)
    rows = []
    for provider in providers:
        profile = read_profile(provider.id)
        rows.append({**provider.as_dict(),
                     "has_key": bool(profile.get("api_key")),
                     "legacy": bool(profile.get("migrated_from")),
                     "selected": provider.id == selected})
    return rows
