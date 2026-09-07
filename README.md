# 水木验码 Shuimu Yanma

> 用可逆实验验证新增代码是否真正受到测试约束。
> Verify whether newly added code is genuinely constrained by tests through reversible experiments.

[![CI](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/kuaikuaijisuanjishu-svg/modou-agent?display_name=tag)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-6C5CE7)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

当前公开版本：**v0.1.1**（评审展示版）

“水木”取自“水木清华”的文化意象，“验码”对应对代码和测试证据的验证。本项目是独立参赛作品，不代表清华大学官方产品或官方背书。

## 它解决什么问题

AI 写出的补丁即使测试全绿，也不代表每一行新增代码都被测试真正约束。水木验码会在隔离工作区中临时移除新增代码，重新运行仓库原有测试，观察具体哪条测试发生变化，然后恢复现场。

```text
本地 Git 仓库 + 明确的测试范围
              ↓
          冻结检查计划
              ↓
     在隔离工作区运行可逆实验
              ↓
      测试结果 + 恢复状态校验
              ↓
       三态结论与可回放证据包
```

- **承重**：临时移除候选代码后，出现具名测试失败。
- **无据**：声明的测试范围没有为该候选提供足够约束。
- **游离**：新增文件未进入已观察到的测试或引用路径。
- **边界纪律**：结论只适用于实际运行的测试范围，不等于证明代码正确、可安全删除或语义等价。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 最新公开版本 | v0.1.1 |
| 公开门禁 | 隐私与结构检查、Python 最小回归、前端单元测试、生产构建、浏览器端到端流程 |
| 可逆反事实证据（核心闭环） | 已验证：公开演示可复现，最小回归通过 |
| 完整 ddmin 最小化 | 实验性可选实现，不是默认演示路径 |
| 惰性结论标签 | 已关闭，不进入公开三态结论 |
| 后续版本能力 | 在非公开工作区验证中；未通过公开门槛前不进入本仓库，也不在此作能力承诺 |

能力状态的机器可读来源是 [configs/capabilities.json](configs/capabilities.json)。发布检查会校验公开文档的措辞与该状态一致：为尚未验证的能力写下更强的结论会导致检查失败。

## 5 分钟运行公开演示

### 环境要求

- Python 3.11 及以上（持续集成在 3.12 上验证）
- Node.js 22（持续集成验证版本）
- 浏览器端到端检查还需要 Chromium：`(cd web && npx playwright install chromium)`

```bash
git clone https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
cd modou-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/build_demo.py
python demo/run_demo.py
```

启动本地界面：

```bash
(cd web && npm ci)
(cd web && npm run build)
python -m modou.server \
  --allow-repo demo/retry_demo \
  --preset-config configs/review-presets.example.json
```

服务只监听本机回环地址，并要求一次性启动令牌。请在浏览器打开终端打印的完整本地地址。

## 验证公开包

```bash
python tools/public_release_check.py
python tests/run.py
(cd web && npm test)
(cd web && npm run build)
(cd web && npx playwright install chromium)
(cd web && npm run test:e2e)
```

以上命令也是 GitHub Actions 的发布门禁；端到端检查使用公开 fixture，并验证人工批准、真实服务链路和仓库路径边界。

端到端检查会自行拉起一个真实服务进程，它使用当前 `PATH` 上的 Python。请在**已激活虚拟环境**的同一个终端里运行，否则该进程会因为找不到已安装的依赖而启动失败。

## 公开版与完整研究版的边界

这个仓库是经过脱敏处理的**公开展示包**，按照公开项目的方式维护，但不是完整研究工作区。

公开仓库包含：

- 可运行的核心审查闭环、最小演示和本地界面；
- 公开安全边界、版本记录、贡献与安全反馈方式；
- 为验证公开内容而设置的最小测试和发布检查。

公开仓库不包含，也不会通过 README、提交历史、Issue、Release 或构建产物披露：

- 历史测试、内部验收规则、未公开评测数据和原始运行记录；
- 项目历史计划、冲刺看板、内部研究笔记和模型原始响应；
- 个人绝对路径、主机信息、密钥、账户标识或私有仓库位置；
- 尚未通过公开门槛的能力主张和完整私有实现。

**位置说明：**公开展示版以本 GitHub 仓库为唯一公开位置；完整研究版保存在独立的非公开工作区。公开文档只描述两者的边界，不公布私有工作区的本地路径、目录结构或历史内容。

发布规则：

1. 只从事先建立的公开白名单中选择文件，不从完整研究版整体复制。
2. 每次发布先运行敏感信息与目录边界检查，再运行最小测试和前端构建。
3. 新功能只有在公开代码、公开说明和公开验证同时具备时才进入 Release。
4. 公开证据包移除绝对路径、源码正文、原始命令输出、原始模型响应和内部标识。
5. Apache-2.0 仅适用于本仓库实际发布的文件；未进入本仓库的私有材料不因本许可证而获得授权。
6. 只有 `main` 分支与 `v*` 发布标签会进入公开仓库。集成分支、研究工作树和内部候选版本不进入公开 refs，也不通过公开仓库中转。

更具体的边界见 [docs/security-boundary.md](docs/security-boundary.md)，架构和资源边界见 [docs/architecture-overview.md](docs/architecture-overview.md)，漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 目录

| 路径 | 公开用途 |
| --- | --- |
| `modou/` | 内部兼容包名：状态、策略、证据引擎和本地服务 |
| `demo/` | 可重建的最小演示仓库 |
| `web/` | 水木验码本地 Review Cockpit |
| `configs/` | 脱敏的能力状态和演示预设 |
| `tests/` | 公开边界的最小回归测试 |
| `docs/` | 公开安全边界与架构说明 |
| `.github/workflows/` | 公开持续集成与发布门禁 |
| `tools/public_release_check.py` | 发布前隐私与结构检查 |

## 项目维护

- 版本变化：[CHANGELOG.md](CHANGELOG.md)
- 参与方式：[CONTRIBUTING.md](CONTRIBUTING.md)
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- 安全反馈：[SECURITY.md](SECURITY.md)
- 发布页面：[GitHub Releases](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases)

## 许可与标识

本仓库代码按 [Apache License 2.0](LICENSE) 许可，详见 [NOTICE](NOTICE)。

“水木验码 / Shuimu Yanma”名称和图形标识不因代码许可证而自动获得商标或品牌使用授权。内部 `modou/` 包名仅用于兼容导入。详见 NOTICE。

---

## English

### Shuimu Yanma v0.1.1

> Use reversible experiments to verify whether newly added code is genuinely constrained by tests.

**Shuimu Yanma** is a sanitized, runnable public showcase for evidence-based code review. The Chinese name combines the cultural image of *shuimu* (water and wood) with *yanma* (code verification). This is an independent competition project; it is not an official Tsinghua University product and carries no institutional endorsement.

### What problem does it solve?

Passing tests do not necessarily mean that every newly added line is actually constrained by those tests. Shuimu Yanma temporarily removes candidate additions inside an isolated Git worktree, reruns the repository's declared tests, observes which named tests change, and restores the workspace afterward.

```text
Local Git repository + declared test scope
                 ↓
          Freeze the review plan
                 ↓
       Run reversible experiments
                 ↓
       Test results + restore checks
                 ↓
      Three-state findings + replayable evidence
```

- **Load-bearing** — removing the candidate causes a named test to fail.
- **Unevidenced** — the declared test scope does not provide enough constraint for the candidate.
- **Orphaned** — a new file is not reached by an observed test or reference path.
- **Scope discipline** — findings apply only to the tests that actually ran. They do not prove correctness, safe deletion, or semantic equivalence.

### Current status

| Item | Status |
| --- | --- |
| Latest public release | v0.1.1 |
| Public gate | privacy and structure check, minimal Python regression, frontend unit tests, production build, browser end-to-end flow |
| Reversible counterfactual evidence (core loop) | Verified: reproducible from the public demo, minimal regression passing |
| Full ddmin minimization | Experimental optional implementation, not the default demo path |
| Inert finding label | Disabled; it does not enter the public three-state findings |
| Later capabilities | Under verification in the non-public workspace. They do not enter this repository, and are not claimed here, before passing the public gate |

The machine-readable source for capability state is [configs/capabilities.json](configs/capabilities.json). The release check verifies that public wording matches that state: writing a stronger conclusion than a capability's state supports makes the check fail.

### Run the public demo in five minutes

#### Requirements

- Python 3.11 or newer (continuous integration verifies 3.12)
- Node.js 22 (the version verified in continuous integration)
- The browser end-to-end check also needs Chromium: `(cd web && npx playwright install chromium)`

```bash
git clone https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
cd modou-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/build_demo.py
python demo/run_demo.py
```

Start the local interface:

```bash
(cd web && npm ci)
(cd web && npm run build)
python -m modou.server \
  --allow-repo demo/retry_demo \
  --preset-config configs/review-presets.example.json
```

The service binds only to the local loopback interface and requires a one-time startup token. Open the complete local URL printed in the terminal.

### Verify the public package

```bash
python tools/public_release_check.py
python tests/run.py
(cd web && npm test)
(cd web && npm run build)
(cd web && npx playwright install chromium)
(cd web && npm run test:e2e)
```

These commands are also the GitHub Actions release gates. The end-to-end checks use public fixtures and verify the approval flow, the real service path, and repository-path boundaries.

The end-to-end check starts a real service process of its own, using the Python found on the current `PATH`. Run it from the **same terminal where the virtual environment is activated**, otherwise that process fails to start because it cannot find the installed dependencies.

### Public package vs. private research workspace

This repository is a **sanitized public showcase** maintained as a public project. It is not the complete research workspace.

The public repository includes:

- the runnable review loop, minimal demo, and local interface;
- public security boundaries, version history, contribution guidance, and security reporting;
- minimal tests and release checks for the published surface.

It intentionally does not contain, and will not disclose through README files, commit history, Issues, Releases, or build artifacts:

- historical tests, internal acceptance rules, unpublished evaluation data, or raw run records;
- project history plans, sprint boards, internal research notes, or raw model responses;
- personal absolute paths, host details, keys, account identifiers, or private repository locations;
- capability claims and complete private implementations that have not passed the public release gate.

**Location policy:** this GitHub repository is the only public location for the showcase. The complete research version remains in a separate non-public workspace. Public documentation describes the boundary without publishing that workspace's local path, directory topology, or historical contents.

### Public release rules

1. Select files only from a pre-established public allowlist; never copy the complete research workspace wholesale.
2. Run sensitive-data and structure checks before the minimal tests and frontend build for every release.
3. A feature enters a Release only when its public code, public explanation, and public verification are all present.
4. Remove absolute paths, source bodies, raw command output, raw model responses, and internal identifiers from public evidence bundles.
5. Apache-2.0 applies only to files actually published in this repository. Unpublished private materials receive no license from this repository.
6. Only the `main` branch and `v*` release tags reach the public repository. Integration branches, research worktrees, and internal candidates never enter public refs and are never staged through this repository.

See [docs/security-boundary.md](docs/security-boundary.md) for the detailed disclosure boundary, [docs/architecture-overview.md](docs/architecture-overview.md) for architecture and resource boundaries, and [SECURITY.md](SECURITY.md) for private vulnerability reports.

### Directory overview

| Path | Public purpose |
| --- | --- |
| `modou/` | Internal compatibility package name for the state, policy, evidence, and local-service layers |
| `demo/` | Rebuildable minimal demonstration repository |
| `web/` | Shuimu Yanma local Review Cockpit |
| `configs/` | Sanitized capability state and demo presets |
| `tests/` | Minimal regression tests for the public boundary |
| `docs/` | Public security boundary and architecture notes |
| `.github/workflows/` | Public continuous integration and release gates |
| `tools/public_release_check.py` | Pre-release privacy and structure check |

The `modou/` directory and related `MODOU_*` environment variables are retained as technical compatibility identifiers. They are internal implementation names, not the public product name.

### Project maintenance

- Version history: [CHANGELOG.md](CHANGELOG.md)
- Contribution guide: [CONTRIBUTING.md](CONTRIBUTING.md)
- Code of conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Security reporting: [SECURITY.md](SECURITY.md)
- Releases: [GitHub Releases](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases)

### License and brand identifiers

The code in this repository is licensed under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE) for the accompanying notice.

The names **Shuimu Yanma / 水木验码**, together with associated logos and visual identifiers, are project and brand identifiers. The software license does not grant trademark or brand-use permission. The internal `modou/` package name exists only for import compatibility.
