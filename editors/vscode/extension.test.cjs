const assert = require('node:assert/strict');
const path = require('node:path');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');
const { selectedSkill, validRequest } = require('./runner.cjs');

const filename = path.join(__dirname, 'extension.cjs');

test('commands route a literal request to an allowlisted document and show the local doctor verdict', async () => {
  const root = path.resolve('workspace fixture');
  const literal = '--help "data"; vis mine gemte Codex-cloudmiljøer';
  const handlers = new Map();
  const calls = [];
  const opened = [];
  const output = [];
  const messages = [];
  const workspace = {
    isTrusted: false, workspaceFolders: [{ uri: { scheme: 'file', fsPath: root } }],
    getConfiguration: () => ({ get: () => '' }),
    async openTextDocument(file) { opened.push(file); return file; },
  };
  const vscode = {
    workspace,
    Uri: { file: value => value },
    ProgressLocation: { Notification: 1 },
    commands: { registerCommand: (name, callback) => { handlers.set(name, callback); return {}; } },
    window: {
      createOutputChannel: () => ({ appendLine: message => output.push(message), show: () => {} }),
      showWarningMessage: message => messages.push(message),
      showErrorMessage: message => { throw Error(message); },
      showTextDocument: async () => {},
      showInputBox: async options => {
        assert.equal(options.validateInput(literal), undefined);
        return literal;
      },
      withProgress: (_, callback) => callback(null, { onCancellationRequested: () => ({ dispose: () => {} }) }),
    },
  };
  const context = { extensionPath: path.dirname(filename), subscriptions: [] };
  const mod = { exports: {} };
  const runner = {
    selectedSkill, validRequest,
    async readReport(command, options) {
      calls.push(command);
      assert.equal(options.coreRoot, path.join(context.extensionPath, 'core'));
      assert.equal(options.workspaceRoots[0], root);
      if (command === 'route') {
        assert.equal(options.request, literal);
        return { skill: 'devsuite-tasks', entrypoint: 'skills/devsuite-tasks/SKILL.md', executionAuthorized: false, externalActionAuthorized: false };
      }
      return { status: 'PASS', errors: [], providersExecuted: false };
    },
  };
  vm.runInNewContext(readFileSync(filename, 'utf8'), {
    require: id => id === 'vscode' ? vscode : id === './runner.cjs' ? runner : require(id),
    module: mod, AbortController,
  }, { filename });
  mod.exports.activate(context);
  await handlers.get('wrpDevSuite.route')();
  assert.deepEqual(calls, []);
  assert.match(messages[0], /trusted local workspace/);
  workspace.isTrusted = true;
  await handlers.get('wrpDevSuite.route')();
  assert.deepEqual(calls, ['route']);
  assert.deepEqual(opened, [path.join(context.extensionPath, 'core', 'skills', 'devsuite-tasks', 'SKILL.md')]);
  assert.equal(JSON.parse(output[0]).executionAuthorized, false);
  await handlers.get('wrpDevSuite.doctor')();
  assert.deepEqual(calls, ['route', 'doctor']);
  assert.equal(JSON.parse(output[1]).status, 'PASS');
});
