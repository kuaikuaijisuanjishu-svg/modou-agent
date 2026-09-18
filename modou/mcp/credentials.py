"""MCP 侧的模型凭据装配。单独成文件，是因为这段代码只有一个职责，而这个职责
出错的代价是密钥落盘。

密钥**只**有两条路进入进程：环境变量，或 `modou.providers`（macOS 钥匙串）。
绝不从 MCP 的配置文件里读——`claude mcp add` 的 env 段会以明文躺在
`~/.claude.json` 里，那等于把钥匙串这一层白做了。

取出来之后也只放进本进程的环境。被测仓库的子进程环境是
`modou.executor.sanitized_environment` 从零白名单构造的，模型配置不在白名单上，
所以被审查的测试代码读不到密钥。

**走的是 Cockpit 的同一条路。** 这个文件曾经自己硬编码一个钥匙串条目名和一组
DeepSeek 默认值，于是你在启动器里换了密钥、加了服务商，Cockpit 跟着变而 MCP
不变——两个界面对"现在用的是哪个模型"给出两种说法，错误信息还不解释为什么。
现在唯一的事实来源是 `modou.providers` 里选中的那一家。
"""
from __future__ import annotations

import os
from typing import Callable, NoReturn

from modou.agent.provider import OpenAICompatibleProvider, ProviderUnavailable
from modou.env import PREFIX, get as env_get


def selected_provider() -> dict:
    """启动器里选中、且已经存了密钥的那一家。取不到就返回空字典。

    地址与模型 id 一律取**注册表**（`~/.modou/providers.json`）而不是钥匙串
    记录里的 ``endpoint`` 字段：历史上有过另一个写入方把 ``endpoint`` 写成
    ``"coding"`` 这样的档位标签而不是 URL，拿它当 base_url 会静默地装配出一个
    连不上的 provider。钥匙串只负责密钥。
    """
    try:
        from modou import providers
        rows = [row for row in providers.status()
                if row.get("selected") and row.get("has_key")]
        if not rows:
            return {}
        row = rows[0]
        key = providers.read_key(row["id"])
    except Exception:                       # noqa: BLE001 — 取不到就当没有
        return {}
    if not key:
        return {}
    return {"id": row["id"], "name": row.get("name") or row["id"], "api_key": key,
            "base_url": str(row.get("base_url") or ""),
            "model_id": str(row.get("model_id") or "")}


def api_key_from_keychain() -> str:
    """只要密钥的那条路径；保留给只关心"有没有钥匙"的调用方。"""
    return str(selected_provider().get("api_key") or "")


def live_provider(*, base_url: str | None = None, model_id: str | None = None,
                  fail: Callable[[str], NoReturn],
                  keychain: Callable[[], str] = api_key_from_keychain,
                  resolve: Callable[[], dict] = selected_provider,
                  ) -> OpenAICompatibleProvider:
    """按 Cockpit 的同一条路径装配 provider。

    优先级：环境变量 > 命令行显式给的 ``--base-url`` / ``--model-id`` > 启动器里
    选中的那一家。命令行没给地址和模型时，用选中那家的，所以你在启动器里换到
    智谱按量，MCP 下一次拉起就跟着换。

    拿不到密钥就调 ``fail`` 退出，**绝不静默降级成确定性调度**——降级会让调用方
    以为模型在参与，而证据包里根本没有模型的份。
    """
    # 选中那一家只解析一次，而且只在真的缺东西时才去碰钥匙串。
    cache: dict = {}
    resolved = False

    def chosen() -> dict:
        nonlocal resolved
        if not resolved:
            cache.update(resolve() or {})
            resolved = True
        return cache

    if not env_get("MODEL_API_KEY"):
        key = str(chosen().get("api_key") or "") or keychain()
        if not key:
            fail("启用模型需要密钥：启动器里没有选中任何一家已存密钥的模型服务，"
                 f"也没有设置 {PREFIX}MODEL_API_KEY。先用 tools/模型API.command "
                 "登记一次密钥并选用它，或在环境里提供密钥——不要写进 MCP 配置文件。")
        os.environ[PREFIX + "MODEL_API_KEY"] = key

    base_url = base_url or env_get("MODEL_BASE_URL") or str(chosen().get("base_url") or "")
    model_id = model_id or env_get("MODEL_ID") or str(chosen().get("model_id") or "")
    if not base_url or not model_id:
        fail("启用模型需要接口地址和模型 id：选中的那一家没有登记它们，"
             "命令行也没有给 --base-url / --model-id。")
    os.environ.setdefault(PREFIX + "MODEL_BASE_URL", base_url)
    os.environ.setdefault(PREFIX + "MODEL_ID", model_id)
    os.environ.setdefault(PREFIX + "MODEL_THINKING", "disabled")
    try:
        return OpenAICompatibleProvider.from_env()
    except ProviderUnavailable as exc:
        fail(f"模型服务不可用：{exc}")
