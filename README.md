# 水木验码 Shuimu Yanma

> 一个受测试证据约束的 AI 代码审查智能体。
> An AI code-review agent constrained by test evidence.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![pytest](https://img.shields.io/badge/tested%20with-pytest-0A9EDC?logo=pytest&logoColor=white)](https://pytest.org/)

## 中文

“水木”取自清华大学校园文化中的“水木清华”意象，“验码”对应项目对代码和测试证据的验证。这个命名表达了对严谨求真的期待；本项目是独立参赛作品，不代表清华大学官方产品或官方背书。

水木验码面向一个越来越现实的问题：AI 写代码越来越快，但原来的测试真的能发现这些新增代码的问题吗？

水木验码不把自己定位成“输入代码、输出评论”的聊天工具。它是一个会围绕目标持续观察、执行实验、更新策略并决定下一步行动的本地智能体：

```text
用户目标 + 本地仓库 + 测试范围
              ↓
        智能体建立审查计划
              ↓
     在隔离工作区执行一次真实实验
              ↓
        获得 Observation / 证据
              ↓
 AI 研究员读取证据，选择继续、调整顺序或停止
              ↓
        恢复工作区并生成可回放结论
```

### 为什么它是智能体

- **有目标**：围绕“新增代码是否被测试真正承载”组织整次审查，而不是只回答一个静态问题。
- **有状态**：持续保存目标、候选单元、已获得的观察、当前步骤和时间预算。
- **会行动**：检查仓库、收集测试、运行基线，并在隔离工作区临时移除候选代码后再次运行测试。
- **会根据观察改变后续行为**：每次真实实验完成后，策略层读取新证据，决定继续检查哪个候选，或在目标满足、预算耗尽、基线异常或恢复失败时停止。
- **有边界**：智能体可以安排实验，但不能修改候选范围，也不能替测试下结论；测试结果和恢复校验才是事实来源。

核心闭环是：

```text
目标 → 行动 → Observation → 下一步决策 → 行动……
```

默认演示使用确定性的本地策略，保证评委可以稳定复现；同一套状态和策略接口也支持在真实 Observation 之后重新排列后续检查顺序。

### 核心方法

水木验码把 AI 补丁放进隔离工作区，暂时拿走新增代码，再运行同一批仓库测试：

- 测试因候选代码被移除而失败：说明这部分代码被测试真正承载。
- 测试仍然通过：说明测试执行到了这部分代码，但没有形成有效约束。
- 测试没有触及：说明当前测试范围没有提供证据。
- 文件不在测试路径或执行链路中：标记为游离候选。

实验结束后，水木验码验证工作区恢复状态，并把观察、结论和必要的聚合证据保存为可回放的本地证据包。

### 快速运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/run_demo.py
```

演示会在临时 fixture 仓库中运行一次完整审查，展示智能体事件流、真实测试观察、逐行结论和公开证据摘要。运行产生的文件保存在本机运行目录，不会写回展示仓库。

### 可选界面

```bash
npm --prefix web install
npm --prefix web run build
python demo/run_demo.py
python -m modou.server --allow-repo demo/retry_demo
```

服务只监听本机回环地址，并要求启动令牌。评委可用浏览器打开终端打印的本地地址体验界面。

### 公开边界

- 只接受评委明确指定的本地仓库和仓库内测试路径。
- 结论只针对实际运行过的测试范围，不把未覆盖部分推断为安全。
- 系统不会自动提交、推送或修改评委仓库的历史。
- 对外证据包会移除绝对路径、主机身份、源码行、原始命令输出、原始模型响应和内部标识。
- 本包没有 GitHub App、评测数据集、历史运行目录或内部验收脚本。

更具体的本地安全边界见 `docs/security-boundary.md`。发布前检查使用：

```bash
python tools/public_release_check.py
```

### 目录

| 路径 | 用途 |
| --- | --- |
| `modou/` | 智能体状态、策略、证据引擎和本地服务 |
| `demo/` | 自足的最小演示 fixture |
| `web/` | 可选的本地 Review Cockpit |
| `configs/review-presets.example.json` | 一个脱敏的演示 preset |
| `tools/public_release_check.py` | 发布前公开边界检查 |

### 许可

本展示包暂未指定开源许可证；如需复用代码，请先联系项目维护者。

## English

“Shuimu” comes from the “Shuimu Qinghua” cultural image associated with Tsinghua University, while “Yanma” means verifying code. The name reflects an aspiration for rigorous, evidence-based engineering. This is an independent competition project, not an official Tsinghua University product or endorsement.

Shuimu Yanma addresses a practical problem: AI can write code faster, but do the existing tests actually detect problems in the newly added code?

Shuimu Yanma is not a chat tool that simply turns code into comments. It is a local agent that maintains a goal, performs experiments, reads observations, updates its strategy, and decides what to do next:

```text
User goal + local repository + test scope
                    ↓
              Agent builds a review plan
                    ↓
       Runs a real experiment in an isolated workspace
                    ↓
                 Receives an observation
                    ↓
       Reads the evidence and chooses what to do next
                    ↓
        Restores the workspace and produces replayable results
```

### Why it is an agent

- **Goal-directed**: it organizes the review around whether the added code is actually carried by tests, rather than answering a static question.
- **Stateful**: it tracks the goal, candidate units, observations, current step, and time budget.
- **Action-oriented**: it inspects the repository, collects tests, runs a baseline, and temporarily removes candidate code in an isolated workspace before rerunning the same tests.
- **Observation-driven**: after each real experiment, the policy layer reads the new evidence and chooses the next candidate, or stops when the goal is satisfied, the budget is exhausted, the baseline is invalid, or restoration fails.
- **Constrained by design**: the agent can schedule experiments, but it cannot change the candidate universe or overrule test facts; test results and restoration checks remain the source of truth.

The central loop is:

```text
Goal → Action → Observation → Next decision → Action …
```

The default demo uses a deterministic local policy so judges can reproduce the same run. The same state and policy interfaces can reorder later checks after a real observation.

### Core method

Shuimu Yanma places an AI patch in an isolated workspace, temporarily removes newly added code, and runs the same repository tests again:

- If a test fails after the candidate code is removed, the code is genuinely exercised and carried by that test.
- If the test still passes, the test reached the code but did not form an effective constraint.
- If no test reaches it, the current test scope provides no evidence for that candidate.
- If a file is outside the test path or execution chain, it is marked as an unconnected candidate.

After the experiment, Shuimu Yanma verifies workspace restoration and stores observations, conclusions, and necessary aggregate evidence in a replayable local evidence bundle.

### Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
python demo/run_demo.py
```

The demo runs a complete review in a temporary fixture repository and shows the agent event stream, real test observations, line-level conclusions, and a public evidence summary. Generated files stay in a local runtime directory and are not written back to the showcase repository.

### Optional local UI

```bash
npm --prefix web install
npm --prefix web run build
python demo/run_demo.py
python -m modou.server --allow-repo demo/retry_demo
```

The service listens only on the local loopback interface and requires a startup token. Judges can open the local address printed in the terminal.

### Public boundary

- Only explicitly selected local repositories and repository test paths are accepted.
- Conclusions apply only to the tests that actually ran; uncovered code is not inferred to be safe.
- The system does not automatically commit, push, or rewrite a judge's repository history.
- Public evidence bundles remove absolute paths, host identity, source lines, raw command output, raw model responses, and internal identifiers.
- This package contains no GitHub App, evaluation dataset, historical run directory, or internal acceptance scripts.

See `docs/security-boundary.md` for the local security boundary. Run the release check before publishing:

```bash
python tools/public_release_check.py
```

### Repository layout

| Path | Purpose |
| --- | --- |
| `modou/` | Agent state, policy, evidence engine, and local service |
| `demo/` | Self-contained minimal demo fixture |
| `web/` | Optional local Review Cockpit |
| `configs/review-presets.example.json` | Sanitized demo preset |
| `tools/public_release_check.py` | Public-boundary release check |

### License

This showcase package currently has no open-source license. Contact the maintainers before reuse.
