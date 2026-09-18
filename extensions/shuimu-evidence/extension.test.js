'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const original = Module._load;
const commands = new Map();
const calls = [];
const saved = new Map();
const secrets = new Map();
const config = {get: key => ({projectPath: '/tmp/shuimu package', pythonPath: '/python', server: 'http://127.0.0.1:8787'})[key]};
let output = '';
const vscode = {
  Disposable: class { constructor(fn) { this.fn = fn; } dispose() { this.fn && this.fn(); } },
  StatusBarAlignment: {Left: 1},
  workspace: {isTrusted: true, getConfiguration: () => config},
  commands: {registerCommand: (name, fn) => { commands.set(name, fn); return {dispose() {}}; }},
  window: {
    createOutputChannel: () => ({clear() {output = '';}, appendLine(text) {output += text;}, show() {}, dispose() {}}),
    createStatusBarItem: () => ({text: '', tooltip: '', command: '', show() {this.shown = true;}, dispose() {}}),
    showInputBox: async options => options.password ? 'secret-token' : options.value !== undefined ? options.value : 'Verify current change',
    showQuickPick: async choices => choices[0],
    showInformationMessage: async () => {},
    showErrorMessage: async message => {throw new Error(message);},
  },
};
const context = {subscriptions: [],
  workspaceState: {get: (key, fallback) => saved.get(key) || fallback, update: async (key, value) => saved.set(key, value)},
  secrets: {get: async key => secrets.get(key), store: async (key, value) => secrets.set(key, value), delete: async key => secrets.delete(key)},
};
Module._load = function(name, ...rest) {
  if (name === 'vscode') return vscode;
  if (name === 'node:child_process') return {execFile: (python, argv, options, callback) => {
    calls.push({python, argv, options});
    const command = argv[4];
    const result = command === 'repos' ? {repos: [{repo_id: 'r1', display_name: 'Example'}]} :
      command === 'start' ? {task: {task_id: 't1'}} :
      command === 'delegation' ? {delegations: [{auth_id: 'auth-1', task_id: 't1', derived_status: 'active',
        remaining_reverify: 2, last_check: {outcome: 'no_change'},
        last_receipt: {job_id: 'job-1', receipt_status: 'supported', receipt_sha256: 'x'}}]} :
      {task_id: 't1', outcome: 'incomplete'};
    callback(null, JSON.stringify(result), '');
  }};
  return original.call(this, name, ...rest);
};
const {activate, runClient, delegationState} = require('./extension');
Module._load = original;

test.after(() => { for (const item of context.subscriptions.splice(0)) item.dispose(); });

test('four commands call the shared client and preserve raw receipt status', async () => {
  activate(context);
  assert.equal(commands.size, 4);
  await commands.get('shuimu.reviewCurrentChange')();
  assert.equal(saved.get('lastTaskId'), 't1');
  await commands.get('shuimu.openTask')();
  await commands.get('shuimu.viewReceipt')();
  await commands.get('shuimu.checkDelegated')();
  // R3：checkDelegated 真实执行检查（delegation-check POST），随后回读状态。
  assert.deepEqual(calls.map(x => x.argv[4]),
    ['repos', 'start', 'open', 'receipt', 'delegation-check', 'delegation']);
  assert.deepEqual(calls.at(-2).argv.slice(4),
    ['delegation-check', '--task-id', 't1', '--reason', 'vscode_check']);
  assert.deepEqual(calls.at(-1).argv.slice(4), ['delegation', '--task-id', 't1']);
  assert.match(output, /incomplete/);
  for (const call of calls) {
    assert.equal(call.options.env.SHUIMU_LOCAL_TOKEN, 'secret-token');
    assert.ok(!call.argv.includes('secret-token'));
    assert.equal(call.options.cwd, '/tmp/shuimu package');
    assert.equal(call.options.shell, undefined);
  }
});
test('arguments are sent without shell interpretation', async () => {
  const goal = 'a; $(do-not-run)';
  await runClient(config, 'private', ['start', '--goal', goal], (python, argv, options, done) => {
    assert.equal(argv.at(-1), goal); assert.equal(options.shell, undefined); done(null, '{}', '');
  });
});
test('delegation state derivation stays read-only and honest about the boundary', () => {
  // 委托有效≠验收通过：active 授权只给"待复验"，不替回执宣布通过。
  const active = delegationState({delegations: [{derived_status: 'active', remaining_reverify: 2,
    last_check: {outcome: 'no_change'}, last_receipt: {receipt_status: 'supported'}}]});
  assert.equal(active.label, '待复验');
  assert.match(active.detail, /委托有效≠验收通过/);
  // 已投递但还没有回执：运行中，作业完成也不等于验收通过。
  assert.equal(delegationState({delegations: [{derived_status: 'active', remaining_reverify: 1,
    last_check: {outcome: 'dispatched'}, last_receipt: {}}]}).label, '运行中');
  for (const derived of ['needs_reconfirm', 'expired', 'exhausted']) {
    assert.equal(delegationState({delegations: [{derived_status: derived, remaining_reverify: 0}]}).label,
      '需用户决定', derived);
  }
  assert.equal(delegationState({delegations: [{derived_status: 'stopped'}]}).label, '已停止');
  assert.equal(delegationState({delegations: []}).label, '未委托');
  // R4：主状态由当前（最新）授权决定，历史 stopped 不压过当前 active。
  const mixed = delegationState({delegations: [
    {derived_status: 'active', remaining_reverify: 2},
    {derived_status: 'stopped'}, {derived_status: 'expired'}]});
  assert.equal(mixed.label, '待复验');
  assert.match(mixed.detail, /2 份历史授权/);
  // 空对象/缺字段按"没有记录"处理，不猜状态。
  assert.equal(delegationState({}).label, '未委托');
});
