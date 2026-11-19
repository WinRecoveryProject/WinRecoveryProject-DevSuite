const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { pythonPath, readReport, selectedSkill, validRequest } = require('./runner.cjs');

const coreRoot = process.env.WRP_TEST_DEVSUITE_CORE || path.resolve(__dirname, '..', '..');

test('route paths and request limits are checked before use', () => {
  const valid = { skill: 'devsuite-tasks', entrypoint: 'skills/devsuite-tasks/SKILL.md', executionAuthorized: false, externalActionAuthorized: false };
  assert.equal(selectedSkill(valid), valid.entrypoint);
  assert.throws(() => selectedSkill({ ...valid, entrypoint: '../outside.md' }), /unexpected/);
  assert.throws(() => selectedSkill({ ...valid, executionAuthorized: true }), /unexpected/);
  assert.throws(() => selectedSkill({ ...valid, skill: 'unknown' }), /unexpected/);
  assert.equal(validRequest(''), false);
  assert.equal(validRequest('x'.repeat(4001)), false);
  assert.equal(validRequest('nul\x00arg'), false);
  assert.equal(validRequest('--help'), true);
});

test('Python discovery ignores project executables and relative PATH', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'wrp-devsuite-trust-'));
  const executable = path.join(root, process.platform === 'win32' ? 'python.exe' : 'python3');
  await fs.writeFile(executable, 'must never run');
  t.after(async () => {
    await fs.rm(executable);
    await fs.rmdir(root);
  });
  await assert.rejects(pythonPath('', [root], { PATH: ['.', root].join(path.delimiter) }), /not found/);
  await assert.rejects(pythonPath(executable, [root]), /not found/);
  await assert.rejects(pythonPath('./python', [root]), /absolute/);
});

test('actual DevSuite route and doctor run through the bridge', async () => {
  const options = { coreRoot, workspaceRoots: [] };
  const danish = await readReport('route', { ...options, request: 'vis mine gemte Codex-cloudmiljøer' });
  assert.equal(danish.skill, 'devsuite-tasks');
  assert.equal(danish.executionAuthorized, false);
  assert.equal(danish.externalActionAuthorized, false);
  const review = await readReport('route', { ...options, request: 'review this patch' });
  assert.equal(review.skill, 'devsuite-review');
  const literal = await readReport('route', { ...options, request: '--help' });
  assert.equal(literal.skill, 'devsuite');
  const report = await readReport('doctor', options);
  assert.equal(report.status, 'PASS', JSON.stringify(report.errors));
  assert.deepEqual(report.errors, []);
  assert.equal(report.providersExecuted, false);
});
