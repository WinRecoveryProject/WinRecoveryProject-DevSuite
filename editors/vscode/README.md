# WinRecoveryProject DevSuite for Visual Studio Code

Works in desktop VS Code 1.90 or later with Python 3.10 or later. The VSIX includes DevSuite 0.6.0 and its 14 workflow guides.

- **Find Workflow for Request** maps a request to the bundled guide and opens its Markdown file. The text is passed as one literal argument to DevSuite’s local router. This command does not run the guide or send the request to a service.
- **Check Local Package** displays the DevSuite package doctor result in the **WinRecoveryProject DevSuite** Output channel. Its result describes local files, workflow links and package contracts. Provider readiness requires separate evidence.

Use the Command Palette to run either command. VS Code uses Python available on the host PATH. If it cannot find the expected version, set `wrpDevSuite.pythonPath` in User Settings to an absolute Python executable outside the workspace.

These commands work locally without a cloud account, AI request, task submission or background job. Requests about Codex cloud environments open the relevant guide. This VSIX does not create a cloud connection or list remote environments. Learn about the full Codex plugin and the existing provider connections in the [DevSuite repository](https://github.com/WinRecoveryProject/WinRecoveryProject-DevSuite).

## Build

From the repository root, provide an existing copy of Microsoft's `@vscode/vsce` 3.7.1. The packager runs the native DevSuite build, checks that its released files are contained in the VSIX and normalizes ZIP member order and timestamps.

```text
python -I -B editors/vscode/package-vsix.py --vsce "<path to @vscode/vsce/vsce>" --out "<destination>/winrecoveryproject-devsuite-0.2.7.vsix"
node --test editors/vscode/runner.test.cjs
```
