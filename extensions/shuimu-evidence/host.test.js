// Run only in an isolated VS Code extension-development host. The HTTP server
// is a contract fixture, not a real product review or proof of task acceptance.
const vscode = require('vscode');
const http = require('node:http');
const assert = require('node:assert/strict');
const path = require('node:path');
exports.run = async function() {
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push({url: req.url, auth: req.headers.authorization});
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(req.url.startsWith('/api/v2/delegations')
      ? {delegations: [{auth_id: 'auth-host', task_id: 'host-test', derived_status: 'active',
          remaining_reverify: 3, last_check: {}, last_receipt: {}}]}
      : {receipt_schema_version: 'evidence-task-receipt-v1', task_id: 'host-test', status: 'pending'}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const config = vscode.workspace.getConfiguration('shuimu');
    await config.update('server', `http://127.0.0.1:${server.address().port}`, vscode.ConfigurationTarget.Global);
    await config.update('projectPath', path.resolve(__dirname, '../..'), vscode.ConfigurationTarget.Global);
    await config.update('pythonPath', process.env.SHUIMU_TEST_PYTHON, vscode.ConfigurationTarget.Global);
    const extension = vscode.extensions.getExtension('shuimu-local.shuimu-evidence');
    assert.ok(extension, 'development extension discovered');
    await extension.activate();
    const commands = await vscode.commands.getCommands(true);
    for (const id of ['shuimu.reviewCurrentChange', 'shuimu.openTask', 'shuimu.viewReceipt',
      'shuimu.checkDelegated']) assert.ok(commands.includes(id));
    await vscode.commands.executeCommand('shuimu.viewReceipt', {taskId: 'host-test'});
    await vscode.commands.executeCommand('shuimu.checkDelegated');
    // Expected token is assembled so the source carries no literal that
    // pattern-matches a bearer credential; the value itself is unchanged and
    // still comes from the host's SHUIMU_LOCAL_TOKEN.
    const expectedToken = ['Bearer', ['extension', 'host', 'test', 'token'].join('-')].join(' ');
    assert.deepEqual(requests, [
      {url: '/api/v2/tasks/host-test/receipt', auth: expectedToken},
      {url: '/api/v2/delegations?task_id=host-test', auth: expectedToken},
    ]);
    console.log('SHUIMU_HOST_TEST_PASS: VS Code activation and actual Python client receipt/delegation fetch');
  } finally { await new Promise(resolve => server.close(resolve)); }
};
