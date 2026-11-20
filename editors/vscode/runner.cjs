const { execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const { promisify } = require('node:util');

const processFile = promisify(execFile);
const pythonFlags = ['-I', '-B', '-X', 'utf8'];
const knownSkills = new Set([
  'devsuite', 'devsuite-convert', 'devsuite-debug', 'devsuite-environment', 'devsuite-handoff',
  'devsuite-implement', 'devsuite-jobs', 'devsuite-planning',
  'devsuite-review', 'devsuite-security-assess', 'devsuite-security-fix', 'devsuite-tasks', 'devsuite-test',
]);

function contained(root, file) {
  const relative = path.relative(root, file);
  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep));
}

async function pythonPath(configured, workspaceRoots, env = process.env) {
  if (configured && !path.isAbsolute(configured)) {
    throw new Error('Configure an absolute Python executable outside the workspace.');
  }
  const roots = await Promise.all(workspaceRoots.map(async root => {
    try { return await fs.realpath(root); } catch { return path.resolve(root); }
  }));
  const outsideWorkspace = candidate => !roots.some(root => contained(root, candidate));
  const pathKey = Object.keys(env).find(key => key.toUpperCase() === 'PATH') || 'PATH';
  const names = process.platform === 'win32' ? ['python.exe', 'python3.exe'] : ['python3', 'python'];
  const systemPaths = (env[pathKey] || '').split(path.delimiter).filter(entry => path.isAbsolute(entry));
  const candidates = configured ? [configured] : systemPaths.flatMap(entry => names.map(name => path.join(entry, name)));
  for (const candidate of candidates) {
    if (!outsideWorkspace(candidate)) continue;
    if (process.platform === 'win32' && /[\\/]Microsoft[\\/]WindowsApps[\\/]/i.test(candidate)) continue;
    try {
      const physical = await fs.realpath(candidate);
      if (outsideWorkspace(physical) && (await fs.stat(physical)).isFile()) return physical;
    } catch { /* An unavailable PATH entry does not supply Python. */ }
  }
  throw new Error('Python 3.10 or later was not found outside the workspace.');
}

function validRequest(request) {
  return typeof request === 'string' && request.trim() !== '' && request.length <= 4000 && !request.includes('\x00');
}

function selectedSkill(report) {
  const entrypoint = 'skills/' + report?.skill + '/SKILL.md';
  if (!knownSkills.has(report?.skill) || report.entrypoint !== entrypoint ||
      report.executionAuthorized !== false || report.externalActionAuthorized !== false) {
    throw new Error('The bundled router returned an unexpected workflow.');
  }
  return entrypoint;
}

async function readReport(command, { coreRoot, workspaceRoots, configuredPython, request, signal }) {
  if (!['route', 'doctor'].includes(command)) throw new Error('Unsupported DevSuite command.');
  if (command === 'route' && !validRequest(request)) throw new Error('Enter a request of 1–4,000 characters.');
  const executable = await pythonPath(configuredPython, workspaceRoots);
  const options = { cwd: coreRoot, signal, windowsHide: true, timeout: 30000, maxBuffer: 2 * 1024 * 1024 };
  let version;
  try {
    version = (await processFile(executable, [...pythonFlags, '--version'], options)).stdout.trim().match(/^Python (\d+)\.(\d+)\./);
  } catch {
    throw new Error('The configured Python executable could not start.');
  }
  if (!version || Number(version[1]) < 3 || (Number(version[1]) === 3 && Number(version[2]) < 10)) {
    throw new Error('WinRecoveryProject DevSuite requires Python 3.10 or later.');
  }
  const args = [...pythonFlags, path.join(coreRoot, 'scripts', 'devsuite.py'), command];
  if (command === 'route') args.push('--', request);
  let result;
  try {
    result = { ...(await processFile(executable, args, options)), code: 0 };
  } catch (error) {
    if (command !== 'doctor' || error.code !== 1 || !error.stdout || signal?.aborted) {
      throw new Error('The bundled DevSuite command could not run.');
    }
    result = error;
  }
  let report;
  try { report = JSON.parse(result.stdout); } catch { throw new Error('DevSuite returned an invalid report.'); }
  if (command === 'route') {
    selectedSkill(report);
  } else if (report?.status !== (result.code === 0 ? 'PASS' : 'FAIL') ||
             !Array.isArray(report.errors) || report.providersExecuted !== false) {
    throw new Error('DevSuite returned an unexpected package check result.');
  }
  return report;
}

module.exports = { pythonPath, readReport, selectedSkill, validRequest };
