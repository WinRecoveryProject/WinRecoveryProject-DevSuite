const path = require('node:path');
const vscode = require('vscode');
const { readReport, selectedSkill, validRequest } = require('./runner.cjs');

const product = 'WinRecoveryProject DevSuite';

function activate(context) {
  const coreRoot = path.join(context.extensionPath, 'core');
  const output = vscode.window.createOutputChannel(product);
  let busy = false;

  async function invoke(command) {
    const folders = vscode.workspace.workspaceFolders ?? [];
    if (!vscode.workspace.isTrusted || folders.some(folder => folder.uri.scheme !== 'file')) {
      vscode.window.showWarningMessage(product + ' requires a trusted local workspace.');
      return;
    }
    if (busy) {
      vscode.window.showInformationMessage(product + ' is already running.');
      return;
    }
    busy = true;
    try {
      let request;
      if (command === 'route') {
        request = await vscode.window.showInputBox({
          prompt: 'Describe the work you need to do',
          validateInput: value => validRequest(value) ? undefined : 'Enter a request of 1–4,000 characters.',
        });
        if (request === undefined) return;
      }
      await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: product,
        cancellable: true,
      }, async (_, token) => {
        const cancellationController = new AbortController();
        const cancellation = token.onCancellationRequested(() => cancellationController.abort());
        if (token.isCancellationRequested) cancellationController.abort();
        try {
          const report = await readReport(command, {
            coreRoot,
            workspaceRoots: folders.map(folder => folder.uri.fsPath),
            configuredPython: vscode.workspace.getConfiguration('wrpDevSuite').get('pythonPath'),
            request,
            signal: cancellationController.signal,
          });
          if (cancellationController.signal.aborted) return;
          output.appendLine(JSON.stringify(report, null, 2));
          if (command === 'route') {
            const documentUri = vscode.Uri.file(path.resolve(coreRoot, selectedSkill(report)));
            const document = await vscode.workspace.openTextDocument(documentUri);
            await vscode.window.showTextDocument(document, { preview: true });
          } else {
            output.show(true);
          }
        } catch (error) {
          if (!cancellationController.signal.aborted) {
            vscode.window.showErrorMessage(product + ' — ' + error.message);
          }
        } finally {
          cancellation.dispose();
        }
      });
    } finally {
      busy = false;
    }
  }

  context.subscriptions.push(output,
    vscode.commands.registerCommand('wrpDevSuite.route', () => invoke('route')),
    vscode.commands.registerCommand('wrpDevSuite.doctor', () => invoke('doctor')),
  );
}

module.exports = { activate };
