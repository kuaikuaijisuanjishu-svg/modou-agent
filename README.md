<h1 align="center">水木验码 · Shuimu Yanma</h1>

<p align="center"><strong>用可逆实验验证新增代码是否真正受到测试约束。</strong><br>
Verify whether newly added code is genuinely constrained by tests through reversible experiments.</p>

<p align="center">
  <a href="https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml"><img src="https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml/badge.svg?branch=main" alt="Public CI"></a>
  <a href="https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.2.0-experimental.1"><img src="https://img.shields.io/badge/preview-v0.2.0--experimental.1-blue" alt="v0.2 experimental preview"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-6C5CE7" alt="Apache-2.0 license"></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12: public CI runtime"></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/Node.js-22-339933?logo=nodedotjs&logoColor=white" alt="Node.js 22: public CI runtime"></a>
</p>

<p align="center">
  <a href="#quickstart">快速上手</a> · <a href="#downloads">下载安装</a> · <a href="#capabilities">语言与能力</a> · <a href="docs/integrations.md">CLI / MCP / VS Code</a> · <a href="#english">English</a>
</p>

**水木验码是面向 AI 编程的本地测试证据验证工具。** AI 写出的补丁即使测试全绿，也不代表每一行新增代码都被测试真正约束。水木验码在隔离工作区中临时移除候选新增代码，重新运行声明范围内的测试，观察具体哪条测试发生变化，再校验工作区恢复，留下可回放的证据。

当前公开版本：**v0.2 实验预览版**（`v0.2.0-experimental.1`）。在可信项目和可恢复的工作副本中试用；测试通过、实验支持与功能完全正确不是同一件事。

“水木”取自“水木清华”的文化意象，“验码”对应对代码和测试证据的验证。本项目是独立作品，不代表任何高校的官方产品或官方背书。

## 它解决什么问题

```text
本地 Git 仓库 + 明确的测试范围
              ↓
          冻结检查计划
              ↓
     在隔离工作区运行可逆实验
              ↓
      具名测试 + 恢复状态校验
              ↓
       三态结论与可回放证据
              ↓
    补测候选 → 用户采用 → 新版本复验
```

- **承重**：临时移除候选代码后，出现具名测试失败。
- **无据**：声明的测试范围没有为该候选提供足够约束。
- **游离**：新增文件未进入已观察到的测试或引用路径。
- **边界纪律**：结论只适用于实际运行的代码、测试范围和环境，不等于证明代码正确、可安全删除或语义等价。环境异常或证据不足时，可能返回不可判定。

## v0.2 可以做什么

| 能力 | 使用方式与价值 |
| --- | --- |
| 审查当前改动 | 围绕验收要求查看改动及相关测试依据，区分有支持、缺少依据和暂时无法判断 |
| 查看与导出证据 | 在本地工作台阅读逐行结果、实验记录和对应版本的回执 |
| 处置测试缺口 | 准备或接收补测候选，在隔离环境验证，再由用户确认是否采用 |
| 复验新版本 | 采用候选或修改代码后重新检查，保留轮次与历史，避免把旧证据当作当前结论 |
| 有限期委托 | 按授权期限、次数和预算，通过显式检查事件组织复验，查看进度并请求停止 |
| 连接编程工具 | CLI、MCP、VS Code 连接同一本地后台，访问任务和回执 |

<a id="quickstart"></a>

## 快速上手：复制即可运行

**源码方式需要 Git、Python 3.12 和 Node.js 22**（与公开 CI 对齐）。下面的命令适用于 macOS、Linux 或 Windows 的 WSL2 终端；首次安装需要网络。预置确定性演示不需要模型 API。

```bash
git clone https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
cd modou-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-agent.in
python demo/build_demo.py
python demo/run_demo.py
```

启动本地工作台：

```bash
(cd web && npm ci)
(cd web && npm run build)
python -m modou.server \
  --allow-repo demo/retry_demo \
  --preset-config configs/review-presets.example.json
```

打开终端打印的完整本地地址。服务只监听本机回环地址，并使用启动令牌；不要分享含令牌的链接。

**第一次使用：** 选择项目与验收要求 → 确认计划 → 阅读结果 → 验证补测候选 → 确认采用 → 对新版本复验。例如：“同一学号重复报名，不能增加第二条记录或重复扣除名额。”一次检查只回答这次声明的范围。

<a id="downloads"></a>

## 下载与安装

[![下载 macOS 预览包](https://img.shields.io/badge/下载-macOS_预览包-0969DA?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/download/v0.2.0-experimental.1/shuimu-yanma-v0.2-public-mac-ecb17036.zip)
[![下载 Windows WSL2 预览包](https://img.shields.io/badge/下载-Windows_%2B_WSL2_预览包-0969DA?style=for-the-badge)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/download/v0.2.0-experimental.1/shuimu-yanma-v0.2-public-windows-ecb17036.zip)

- **macOS**：[安装说明](docs/install-mac.md)，解压后双击 `安装水木验码.command`，安装完成再双击 `启动水木验码.command`。
- **Windows + WSL2**：[安装说明](docs/install-windows.md)。后端运行在 WSL2 Linux 中；当前仍是待目标机器验证的预览包。
- **版本与校验**：[本次预览 Release](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.2.0-experimental.1)。GitHub 的 `releases/latest` 仍指向 v0.1.1，请使用这里的明确版本入口。

<details>
<summary>安装包 SHA-256（2026-09-18 上传，源码 ecb17036）</summary>

| 平台 | SHA-256 |
| --- | --- |
| macOS | `00523bb1e165b6f40db95e447d5d2fc89d468d29d3ee873dd4f3a0ffcba41cb4` |
| Windows + WSL2 | `507b008649e1fb64757c44fa6ceee3636555a45e661ebf777a4b41f0ec6b2eb5` |

macOS 校验命令：

```bash
shasum -a 256 shuimu-yanma-v0.2-public-mac-ecb17036.zip
```

Windows PowerShell 校验命令：

```powershell
Get-FileHash .\shuimu-yanma-v0.2-public-windows-ecb17036.zip -Algorithm SHA256
```

</details>

<a id="capabilities"></a>

## 语言与能力

[![Python](https://img.shields.io/badge/Python-pytest-3776AB?logo=python&logoColor=white)](configs/capabilities.json)
[![TypeScript](https://img.shields.io/badge/TypeScript-Vitest_%2F_Jest-3178C6?logo=typescript&logoColor=white)](configs/capabilities.json)
[![Go](https://img.shields.io/badge/Go-go_test-00ADD8?logo=go&logoColor=white)](modou/go_adapter.py)
[![Java](https://img.shields.io/badge/Java-Maven_%2F_Surefire-ED8B00?logo=openjdk&logoColor=white)](modou/java_adapter.py)

这些徽章表示源码中的适配入口，不代表所有语言都通过了同等程度的验证。

| 范围 | 当前状态 |
| --- | --- |
| 预置确定性演示 | 可逆实验、具名测试和脱敏证据包有公开最小回归覆盖 |
| Python / pytest | 实验性、选择启用；不承诺任意仓库适用 |
| TypeScript / Vitest、Jest | 实验性、选择启用；两种框架需分别验证 |
| Go / Java | 包含测试适配实现；不宣称完整归因、修复或真实项目兼容性已验证 |
| 候选修复 | 实验性；验证通过与用户采用是不同步骤，采用后仍需复验 |

详见 [能力状态](configs/capabilities.json)、[当前限制](docs/limitations.md) 和 [架构说明](docs/architecture-overview.md)。

## 验证公开包

在上面的虚拟环境中完成安装和前端构建后执行：

```bash
python tools/public_release_check.py
python tests/run.py
(cd web && npm test)
(cd web && npx playwright install chromium)
(cd web && npm run test:e2e)
```

Linux 首次运行浏览器检查可能还需执行 `npx playwright install --with-deps chromium`（在 `web/` 中运行）。公开 CI 同时运行发布检查、后端测试、前端测试、构建和浏览器端到端检查。

## 公开版边界与隐私

公开仓库包含可运行的审查闭环、演示、本地工作台、接入入口及公开验证；不包含完整非公开研究工作区、内部评测、原始模型响应、个人路径、凭据或未获授权的项目内容。

- 确定性路径可不调用模型。启用模型能力后，选定代码内容可能发送给你配置的模型服务，并产生相应费用。
- 不要提交私人源码、密钥、含令牌链接或未经脱敏的诊断日志。
- 有限期委托按显式事件触发，不表示后台持续扫描全部文件；停止请求需等待执行确认，待恢复状态需先处理恢复。
- 功能只有在公开代码、说明与验证同时具备时才应进入发布；后续发布使用新标签，保留已发布快照及其校验记录。

详见 [安全边界](docs/security-boundary.md)、[隐私说明](docs/privacy.md) 和 [安全反馈](SECURITY.md)。

## 目录与维护

| 路径 | 用途 |
| --- | --- |
| `modou/` | 证据引擎、本地服务及客户端；包名保留用于兼容 |
| `demo/` | 可重建的最小演示仓库 |
| `web/` | 本地 Review Cockpit |
| `extensions/shuimu-evidence/` | VS Code 接入 |
| `configs/` | 能力状态与演示预设 |
| `tests/`、`tools/` | 公开回归测试与发布检查 |

[版本记录](CHANGELOG.md) · [参与贡献](CONTRIBUTING.md) · [行为准则](CODE_OF_CONDUCT.md) · [发布记录](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases)

## 许可与标识

本仓库已发布代码使用 [Apache License 2.0](LICENSE)，第三方组件及品牌说明见 [NOTICE](NOTICE)。名称和图形标识的使用权不随代码许可证自动授予。前端 `private: true` 只用于防止误发布到 npm，不改变源码许可。

---

<a id="english"></a>

## English

### Shuimu Yanma — evidence for AI-generated code

> Use reversible experiments to verify whether newly added code is genuinely constrained by tests.

**Shuimu Yanma** is a local evidence-based code review tool. Passing tests do not necessarily mean every added line is constrained by those tests. It temporarily removes candidate additions in an isolated workspace, reruns the declared tests, records which named tests change, and checks workspace restoration. The result is a scoped finding with replayable evidence.

Current public version: **v0.2 experimental preview** (`v0.2.0-experimental.1`). Use trusted projects and recoverable working copies. This independent project has no institutional endorsement.

### How it works

```text
Local Git repository + declared test scope
                 ↓
          Freeze the review plan
                 ↓
       Run reversible experiments
                 ↓
     Named tests + restoration checks
                 ↓
      Findings + replayable evidence
                 ↓
 Candidate tests → user adoption → revalidation
```

- **Load-bearing:** removing the candidate causes a named test to fail.
- **Unevidenced:** the declared tests do not provide enough constraint for the candidate.
- **Orphaned:** a new file is not reached by an observed test or reference path.
- **Scope discipline:** results apply only to the reviewed code, executed tests and environment. They do not prove correctness, safe deletion or semantic equivalence. Missing evidence or execution problems may leave a result inconclusive.

### What is new in v0.2?

Review changes against acceptance requirements, inspect and export version-specific evidence, validate candidate tests before user adoption, and recheck after code changes. Bounded delegations organize further checks through explicit events within the approved time, count and budget. CLI, MCP and VS Code share the same local backend and receipts.

### Quick start

Prerequisites: Git, **Python 3.12 and Node.js 22**, matching public CI. Run in macOS, Linux or WSL2. Initial setup needs network access; the deterministic demo needs no model API.

```bash
git clone https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
cd modou-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-agent.in
python demo/build_demo.py
python demo/run_demo.py
(cd web && npm ci)
(cd web && npm run build)
python -m modou.server \
  --allow-repo demo/retry_demo \
  --preset-config configs/review-presets.example.json
```

Open the complete local URL printed by the server. The service binds to loopback and uses a startup token. Do not share token-bearing URLs. Select a project and requirement, approve the plan, read findings, validate candidates, confirm adoption and revalidate the new version.

### Downloads and language support

Use the [explicit preview release](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.2.0-experimental.1) or the [download buttons and SHA-256 table above](#downloads). GitHub's latest-release shortcut still points to v0.1.1. macOS offers double-click installation and launch scripts. The Windows backend requires WSL2 and remains a preview pending target-machine validation.

The deterministic demo has public regression coverage. Python/pytest and TypeScript/Vitest/Jest adapters are experimental and opt-in. Go and Java test adapters are included, without claiming fully validated attribution, repair or real-project compatibility. Language badges identify implementation entry points, not equivalent validation status. See the [capability registry](configs/capabilities.json) and [limitations](docs/limitations.md).

### Verify the public package

After setup and frontend build, with the virtual environment active:

```bash
python tools/public_release_check.py
python tests/run.py
(cd web && npm test)
(cd web && npx playwright install chromium)
(cd web && npm run test:e2e)
```

On Linux, browser system dependencies may require `npx playwright install --with-deps chromium` inside `web/`.

### Public boundary, privacy and maintenance

This repository includes the runnable public review loop, demo, local interface, integrations and public checks. It excludes private research history, internal evaluations, raw model responses, personal paths, credentials and unauthorized project content. The `modou/` package name remains for import compatibility.

Model-enabled features may send selected code to your configured provider and incur costs. Review data scope before enabling them. Candidate validation is separate from adoption; adoption still requires revalidation. Delegations use explicit check events, and a stop request must be followed by confirmed execution stop or recovery.

[Architecture](docs/architecture-overview.md) · [Integrations](docs/integrations.md) · [Security boundary](docs/security-boundary.md) · [Privacy](docs/privacy.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security reporting](SECURITY.md)

Published code is licensed under [Apache License 2.0](LICENSE); see [NOTICE](NOTICE). Unpublished materials and brand rights are not granted by this license. Future releases should use new tags and preserve published snapshots and verification records.
