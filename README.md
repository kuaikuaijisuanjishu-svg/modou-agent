# 水木验码 Shuimu Yanma

> 用可逆实验验证新增代码是否真正受到测试约束。
> Verify whether newly added code is genuinely constrained by tests through reversible experiments.

[![CI](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/workflows/ci.yml)
[![Experimental release](https://img.shields.io/badge/release-v0.2.0--experimental.1-orange)](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.2.0-experimental.1)
[![License](https://img.shields.io/badge/license-Apache--2.0-6C5CE7)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

最新公开实验版：**[v0.2.0-experimental.1](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.2.0-experimental.1)**，发布于 2026-09-08。

这是 **experimental / opt-in** 的降级发布，`stable_eligible=false`。新增能力仅供明确选择启用的隔离试用；本版本不承诺第三方仓库兼容性或生产就绪。此前的 [v0.1.1 评审展示版](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.1.1)仍可单独获取。

**验证边界：**AI 安全探针未执行，不能解释为零事件；六库兼容性试点两次方法作废后停止，没有正式 product-on 对照或 6/6 通过结论。详见[本版本发布说明](docs/release/v0.2.0-experimental.1.md)。

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
| 最新公开版本 | v0.2.0-experimental.1；Pre-release；experimental / opt-in；`stable_eligible=false` |
| 本版本发布流水线 | 公开扫描与发布元数据检查通过；Python 冒烟测试 2 项、前端测试 46 项、浏览器端到端测试 2 项通过；前端构建成功 |
| 可逆反事实证据（核心闭环） | 已验证：公开演示可复现，最小回归通过 |
| Python 适配器、Vitest 与 Jest 适配器、候选修复 | 实验性可选实现（experimental / opt-in），默认关闭；尚无第三方仓库兼容性结论 |
| 完整 ddmin 最小化 | 实验性可选实现，不是默认演示路径 |
| 惰性结论标签 | 已关闭，不进入公开三态结论 |
| AI+Skill 安全探针 | 因环境与凭据预检阻塞，计划中的 10 个合成候选 session 未执行；不是已完成的安全验证或真人研究 |
| 六库 product-off / product-on 试点 | 两次方法批次均在正式六库冻结前作废，失败预算 2/2 已用尽；未运行正式 product-on 对照 |
| stable 门禁 | 独立隐藏 QA、独立 Linux 主机验证、授权私库试点、真人开发者研究、非实现者人工安全签字均未完成 |

能力状态的机器可读来源是 [configs/capabilities.json](configs/capabilities.json)。发布检查会校验公开文档的措辞与该状态一致：为尚未验证的能力写下更强的结论会导致检查失败。

发布流水线的记录见 [GitHub Actions](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/runs/34219991504)。这些检查支持公开演示和声明范围内的机器回归，不代表任意仓库适用性、完整安全认证、语义等价或代码可安全删除。

## 试用前须知

- 只在你信任的仓库和隔离环境中试用；Git 工作区隔离本身不是安全沙箱。
- 先运行公开演示，再按明确范围选择启用实验性能力；保持人工审查与审批。
- 本版本不承诺固定修复时限。发现安全问题时按 [SECURITY.md](SECURITY.md) 报告；若私密漏洞上报入口不可用，只提交请求私密联系方式的最小 issue，不要公开漏洞细节、凭据、私有路径或源码。
- AI 探针和六库兼容性验证留待后续版本重新计划，本版本不会开启第三批六库追跑。

## 5 分钟运行公开演示

### 环境要求

- Python 3.11 及以上（持续集成在 3.12 上验证）
- Node.js 22（持续集成验证版本）
- 浏览器端到端检查还需要 Chromium：`(cd web && npx playwright install chromium)`

```bash
git clone --branch v0.2.0-experimental.1 https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
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

以上命令覆盖公开扫描和测试；正式发布流水线还核对冻结提交、公开树指纹和 Notes。端到端检查使用公开 fixture，并验证人工批准、真实服务链路和仓库路径边界。

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
3. 实验性可选实现必须披露启用方式、证据范围和未完成验证；只有补齐对应证据后才能升级为已验证能力。
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

### Shuimu Yanma v0.2.0-experimental.1

> Use reversible experiments to verify whether newly added code is genuinely constrained by tests.

**Shuimu Yanma** is a sanitized, runnable public showcase for evidence-based code review. The Chinese name combines the cultural image of *shuimu* (water and wood) with *yanma* (code verification). This is an independent competition project; it is not an official Tsinghua University product and carries no institutional endorsement.

The latest public experimental release is **[v0.2.0-experimental.1](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.2.0-experimental.1)**, published on 2026-09-08. It follows the degraded release path, remains **experimental / opt-in**, and has `stable_eligible=false`. It makes no third-party repository compatibility or production-readiness claim. The earlier [v0.1.1 showcase](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/releases/tag/v0.1.1) remains available separately.

**Validation limits:** the AI safety probe was not executed; this is not a zero-event result. The six-repository trial stopped after two method invalidations, without a formal product-on comparison or a six-of-six passing result. See the [release notes](docs/release/v0.2.0-experimental.1.md).

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
| Latest public release | v0.2.0-experimental.1; Pre-release; experimental / opt-in; `stable_eligible=false` |
| Release workflow for this version | Public scan and release metadata checks passed; 2 Python smoke tests, 46 frontend tests, and 2 browser end-to-end tests passed; frontend build succeeded |
| Reversible counterfactual evidence (core loop) | Verified: reproducible from the public demo, minimal regression passing |
| Python adapter, Vitest and Jest adapters, repair candidates | Experimental / opt-in implementations, disabled by default; third-party repository compatibility has not been established |
| Full ddmin minimization | Experimental optional implementation, not the default demo path |
| Inert finding label | Disabled; it does not enter the public three-state findings |
| AI+Skill safety probe | Environment and credential preflight blocked execution of the 10 planned synthetic candidate sessions; neither completed safety validation nor a human study |
| Six-repository product-off / product-on trial | Both method batches were invalidated before the formal six-repository freeze; the 2/2 failure budget is exhausted; no formal product-on comparison ran |
| Stable gates | Independent hidden QA, independent Linux-host validation, authorized private-repository pilot, real-developer study, and independent human security sign-off remain pending |

The machine-readable source for capability state is [configs/capabilities.json](configs/capabilities.json). The release check verifies that public wording matches that state: writing a stronger conclusion than a capability's state supports makes the check fail.

The release workflow is recorded in [GitHub Actions](https://github.com/kuaikuaijisuanjishu-svg/modou-agent/actions/runs/34219991504). These checks support the public demo and machine regression within the declared scope. They do not establish suitability for arbitrary repositories, complete security certification, semantic equivalence, or safe deletion.

### Before trying this release

- Use only repositories you trust, in an isolated environment. Git worktree isolation alone is not a security sandbox.
- Start with the public demo, then explicitly opt in to experimental capabilities for a declared scope; retain human review and approval.
- This release has no fixed remediation-time commitment. Follow [SECURITY.md](SECURITY.md) for reporting. If private vulnerability reporting is unavailable, open only a minimal issue requesting a private contact channel; do not post exploit details, credentials, private paths, or source code.
- The AI probe and compatibility trial require a new plan for a later version. This release will not start a third six-repository batch.

### Run the public demo in five minutes

#### Requirements

- Python 3.11 or newer (continuous integration verifies 3.12)
- Node.js 22 (the version verified in continuous integration)
- The browser end-to-end check also needs Chromium: `(cd web && npx playwright install chromium)`

```bash
git clone --branch v0.2.0-experimental.1 https://github.com/kuaikuaijisuanjishu-svg/modou-agent.git
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

These commands cover the public scan and tests. The release workflow additionally verifies the frozen commit, public-tree fingerprint, and Notes. The end-to-end checks use public fixtures and verify the approval flow, the real service path, and repository-path boundaries.

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
3. Experimental optional implementations must disclose how to opt in, their evidence scope, and incomplete validation. A capability becomes verified only after its required evidence is complete.
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
