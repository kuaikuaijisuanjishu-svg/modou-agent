'use strict';
const vscode = require('vscode');
const {execFile} = require('node:child_process');
const path = require('node:path');

function runClient(config, token, args, execute = execFile) {
  return new Promise((resolve, reject) => {
    const project = config.get('projectPath');
    if (!project || !path.isAbsolute(project)) {
      reject(new Error('请在设置中填写水木验码 projectPath 的绝对路径。')); return;
    }
    const argv = ['-m', 'modou.task_client', '--server', config.get('server'), ...args];
    execute(config.get('pythonPath') || 'python3', argv, {
      cwd: project, env: {...process.env, PYTHONPATH: project, SHUIMU_LOCAL_TOKEN: token},
      timeout: 45000, maxBuffer: 16 * 1024 * 1024, windowsHide: true,
    }, (error, stdout, stderr) => {
      if (error) {
        let message = '无法连接本地后台；请检查服务、解释器与启动令牌。';
        try { message = JSON.parse(stderr).error.message || message; } catch {}
        reject(new Error(message)); return;
      }
      try { resolve(JSON.parse(stdout)); }
      catch { reject(new Error('本地客户端返回的回执格式无效。')); }
    });
  });
}

// 从持续委托授权列表推导状态条文案。rows 按创建时间倒序（服务端排序）：
// 主状态只由**当前**（最新一份）授权决定，历史授权
// 只计条数——历史 stopped/expired 不得压过当前 active。只读推导：
// 委托有效≠验收通过，结论以回执 status 为准；不读文件、不监听保存。
function delegationState(payload) {
  const rows = Array.isArray(payload && payload.delegations) ? payload.delegations : [];
  if (!rows.length) return {label: '未委托', detail: '该任务当前没有持续委托授权记录。'};
  const current = rows[0];
  const status = String(current.derived_status || current.status || '');
  const historyNote = rows.length > 1 ? `（另有 ${rows.length - 1} 份历史授权，只读留档）` : '';
  // 运行中也只看当前授权：已投递复验且尚无回执。
  if (status === 'active' && current.last_check && current.last_check.outcome === 'dispatched'
      && !(current.last_receipt && current.last_receipt.receipt_status)) {
    return {label: '运行中',
      detail: '复验作业已投递、尚无回执；作业完成不等于验收通过。' + historyNote};
  }
  const labels = {active: '待复验', needs_reconfirm: '需用户决定', expired: '需用户决定',
    exhausted: '需用户决定', stopped: '已停止'};
  const details = {
    active: '授权有效，剩余复验次数 ' + (Number(current.remaining_reverify) || 0)
      + '；委托有效≠验收通过，结论以回执 status 为准。',
    needs_reconfirm: '验收要求已变化、授权到期或额度用完；是否新建授权由用户在页面决定。',
    expired: '授权已到期；是否新建授权由用户在页面决定。',
    exhausted: '复验额度已用完；是否新建授权由用户在页面决定。',
    stopped: '当前授权已被用户停止；是否重建由用户决定。',
  };
  return {label: labels[status] || '需用户决定',
    detail: (details[status] || '授权状态未识别，请在页面查看。') + historyNote};
}

// 检查事件结果的如实呈现（R3）：检查可能投递、无变化、被拒或已在途。
function checkOutcomeMessage(outcome) {
  const blockedReason = {expired: '授权已到期', attempt_exhausted: '复验额度已用完',
    requirement_changed: '验收要求已变化，需要重新确认委托', user_stop: '委托已被停止'}[
    outcome.blocked_reason] || outcome.blocked_reason || '原因见服务端记录';
  const labels = {
    dispatched: '已投递复验作业：' + (outcome.job_id || ''),
    no_change: '源码无变化：未启动实验（只记录检查）。',
    already_dispatched: '同一快照已有复验在途：' + (outcome.job_id || ''),
    no_authorization: '该任务没有持续委托授权；创建授权请到水木验码页面。',
    blocked: '检查被拒绝：' + blockedReason,
  };
  return labels[outcome.outcome] || ('检查结果：' + (outcome.outcome || '未返回'));
}

function activate(context) {
  const receiptChannel = vscode.window.createOutputChannel('水木验码 · 任务回执', {log: false});
  context.subscriptions.push(receiptChannel);
  let busy = false;
  async function session() {
    if (!vscode.workspace.isTrusted) throw new Error('请先信任当前工作区。');
    const config = vscode.workspace.getConfiguration('shuimu');
    const server = config.get('server');
    const secretKey = 'local-token:' + server;
    let token = process.env.SHUIMU_LOCAL_TOKEN || await context.secrets.get(secretKey);
    if (!token) {
      token = await vscode.window.showInputBox({title: '连接水木验码本地后台',
        prompt: '输入本次服务启动令牌（只保存到 VS Code SecretStorage）', password: true, ignoreFocusOut: true});
      if (!token) return null;
      await context.secrets.store(secretKey, token);
    }
    return {config, token, secretKey};
  }
  async function taskId() {
    return await vscode.window.showInputBox({title: '水木验码任务编号',
      value: context.workspaceState.get('lastTaskId', ''), ignoreFocusOut: true});
  }
  function register(command, action) {
    context.subscriptions.push(vscode.commands.registerCommand(command, async (...args) => {
      if (busy) { vscode.window.showInformationMessage('水木验码请求正在处理，请稍候。'); return; }
      busy = true;
      let active;
      try { active = await session(); if (active) await action(active, ...args); }
      catch (error) {
        const choice = await vscode.window.showErrorMessage(String(error.message), '重新输入连接令牌');
        if (choice && active) await context.secrets.delete(active.secretKey);
      } finally { busy = false; }
    }));
  }
  register('shuimu.reviewCurrentChange', async ({config, token}) => {
    const listed = await runClient(config, token, ['repos']);
    if (!Array.isArray(listed.repos) || !listed.repos.length) throw new Error('后台尚未登记仓库，请在本地服务中登记当前项目。');
    const chosen = await vscode.window.showQuickPick(listed.repos.map(repo => ({
      label: repo.display_name || repo.repo_id, description: repo.repo_id, repoId: repo.repo_id,
    })), {title: '选择当前改动所在的已登记仓库', ignoreFocusOut: true});
    if (!chosen) return;
    const mode = await vscode.window.showQuickPick([
      {label: '标准审查', description: '确定性分析，不调用模型', mode: 'standard'},
      {label: '智能体审查', description: '授权模型读取选定代码片段并提议补测；仍需确认计划', mode: 'agent'},
    ], {title: '选择本次审查方式', ignoreFocusOut: true});
    if (!mode) return;
    const goal = await vscode.window.showInputBox({title: '本次改动需要什么测试证据？', ignoreFocusOut: true});
    if (!goal || !goal.trim()) return;
    const result = await runClient(config, token, ['start', '--repo-id', chosen.repoId,
      '--goal', goal.trim(), '--mode', mode.mode, '--idempotency-key', require('node:crypto').randomUUID(), '--open']);
    await context.workspaceState.update('lastTaskId', result.task.task_id);
    vscode.window.showInformationMessage('证据任务已建立，请在水木验码中确认审查计划。');
  });
  register('shuimu.openTask', async ({config, token}) => {
    const id = await taskId(); if (!id) return;
    await runClient(config, token, ['open', id]);
    await context.workspaceState.update('lastTaskId', id);
  });
  register('shuimu.viewReceipt', async ({config, token}, input) => {
    const id = typeof input?.taskId === 'string' ? input.taskId : await taskId(); if (!id) return;
    const receipt = await runClient(config, token, ['receipt', id]);
    receiptChannel.clear();
    receiptChannel.appendLine(JSON.stringify(receipt, null, 2));
    receiptChannel.show(true);
    await context.workspaceState.update('lastTaskId', id);
  });
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.command = 'shuimu.checkDelegated';
  context.subscriptions.push(status);
  function showState(state) {
    status.text = `水木验码:${state.label}`;
    status.tooltip = state.detail;
    status.show();
  }
  // 轮询只在已关联任务且本机存有令牌时查询；绝不弹令牌输入框，也不监听文件保存。
  async function refreshDelegation() {
    const id = context.workspaceState.get('lastTaskId', '');
    if (!id) return;
    const config = vscode.workspace.getConfiguration('shuimu');
    const token = process.env.SHUIMU_LOCAL_TOKEN
      || await context.secrets.get('local-token:' + config.get('server'));
    if (!token) return;
    showState(delegationState(await runClient(config, token, ['delegation', '--task-id', id])));
  }
  // “检查受托任务”真实执行一次检查事件（POST
  // delegations/check，可能投递复验），然后回读授权状态刷新状态条。
  // 它只能触发既有授权范围内的检查；创建/续期/停止授权仍在页面。
  register('shuimu.checkDelegated', async ({config, token}) => {
    const id = context.workspaceState.get('lastTaskId', '') || await taskId(); if (!id) return;
    const outcome = await runClient(config, token,
      ['delegation-check', '--task-id', id, '--reason', 'vscode_check']);
    await context.workspaceState.update('lastTaskId', id);
    vscode.window.showInformationMessage(`受托任务 ${id}：${checkOutcomeMessage(outcome)}`);
    const state = delegationState(
      await runClient(config, token, ['delegation', '--task-id', id]));
    showState(state);
  });
  const poll = setInterval(() => { refreshDelegation().catch(() => {}); }, 30000);
  context.subscriptions.push(new vscode.Disposable(() => clearInterval(poll)));
}
module.exports = {activate, runClient, delegationState};
