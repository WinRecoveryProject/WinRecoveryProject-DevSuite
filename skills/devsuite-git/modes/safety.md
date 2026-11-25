# Safety mode

Follow the package policy linked from this skill's router before acting.

<a id="src-git-safety-guardrails-intro"></a>

<a id="src-git-safety-guardrails-git-safety-guardrails"></a>
## Git Safety Guardrails

Install and maintain deterministic pre-execution guardrails that intercept and block dangerous, destructive, or history-rewriting Git commands before autonomous agents can execute them.

---

<a id="src-git-safety-guardrails-core-invariants"></a>
### Core Invariants

1. **Deterministic Interception**: Automatically block destructive Git commands (`git push --force`, `git reset --hard`, `git clean -f/-fd`, `git branch -D`, `git checkout .`, `git restore .`) before execution, including equivalent long options, reordered flags, force refspecs, aliases, Git global options, and chained commands.
2. **Explicit Authority Gate**: Intercepted commands return an explicit non-zero exit code notifying the agent that it lacks authority to execute destructive operations.
3. **Scope Clarification**: Always ask the user whether to apply guardrails locally to the project (`.claude/settings.json`) or globally (`~/.claude/settings.json`).
4. **Settings Merge Safety**: Seamlessly merge guardrail hooks into existing `PreToolUse` configurations without overwriting other tools or settings.
5. **Mandatory Interception Test**: Verify that the safety hook triggers correctly on simulated forbidden commands before concluding setup.
6. **Fail-Closed Classification**: Missing dependencies, malformed hook payloads, unknown Git aliases, and shell syntax the classifier cannot safely interpret return exit code 2.

---

<a id="src-git-safety-guardrails-architecture--map-of-content-moc"></a>
### Architecture & Map of Content (MOC)

```
[ Agent Tool Call (Bash/Git) ] ──► [ PreToolUse Hook: `block-dangerous-git.sh` ]
                                                  │
                        ┌─────────────────────────┴─────────────────────────┐
                        ▼                                                   ▼
            [ Safe Git Operation ]                              [ Destructive Command ]
            - `git status`, `git diff`                          - `git push --force`, `reset --hard`
            - ALLOWED to execute                                - BLOCKED (Exit Code 2)
```

| Component | Responsibility | Location |
|---|---|---|
| **Classifier Hook Script** | Locate the bundled parser and map every dependency or parser failure to exit code 2 | `safety/scripts/block-dangerous-git.sh` |
| **Python Command Parser** | Validate hook JSON and classify shell chains and normalized Git arguments | `safety/scripts/classify_git_command.py` |
| **Settings Integration** | Hook registration in agent environment | `.claude/settings.json` or `~/.claude/settings.json` |

---

<a id="src-git-safety-guardrails-step-by-step-procedure-twi"></a>
### Step-by-Step Procedure (TWI)

<a id="src-git-safety-guardrails-step-1-confirm-guardrail-scope"></a>
#### Step 1: Confirm Guardrail Scope
- **Action**: Ask the user whether to configure guardrails for this project only or globally.
- **Key Point**: Default to project-level `.claude/settings.json` unless the user specifies global.
- **Why**: Scoped installation avoids unexpected side-effects across external personal repositories.

<a id="src-git-safety-guardrails-step-2-copy-hook-script--make-executable"></a>
#### Step 2: Copy Hook Scripts & Make Executable
- **Action**: Copy both files from `safety/scripts/` to `.claude/hooks/`, then run `chmod +x .claude/hooks/block-dangerous-git.sh`.
- **Key Point**: Keep `classify_git_command.py` beside the shell hook and confirm `command -v python3` succeeds before registration. The hook blocks if either dependency is unavailable.
- **Inline Checklist**:
  - [ ] Target directory created
  - [ ] Shell hook copied and executable permissions set (`chmod +x`)
  - [ ] Python classifier copied into the same directory
  - [ ] `python3` dependency present

<a id="src-git-safety-guardrails-step-3-register-hook-in-settings-configuration"></a>
#### Step 3: Register Hook in Settings Configuration
- **Action**: Add the PreToolUse hook entry to `.claude/settings.json`, merging into existing arrays if present.
- **Key Point**: Use `"$CLAUDE_PROJECT_DIR"/.claude/hooks/block-dangerous-git.sh` path expansion.
- **Why**: Relative path expansions ensure the hook functions across different working directory contexts.

<a id="src-git-safety-guardrails-step-4-verify-guardrail-interception-test-gate"></a>
#### Step 4: Verify Guardrail Interception (Test Gate)
- **Action**: Test the hook with blocked, malformed, and allowed simulations:
  ```bash
  printf '%s\n' '{"tool_input":{"command":"git clean --force -d"}}' | .claude/hooks/block-dangerous-git.sh
  printf '%s\n' 'not-json' | .claude/hooks/block-dangerous-git.sh
  printf '%s\n' '{"tool_input":{"command":"git status"}}' | .claude/hooks/block-dangerous-git.sh
  ```
- **Key Point**: Verify the first two commands exit with code 2 and descriptive blocked messages, while `git status` exits zero.
- **Why**: The three cases establish destructive syntax and invalid state fail closed without preventing routine read-only Git use.

---

<a id="src-git-safety-guardrails-anti-rationalization-guardrails"></a>
### Anti-Rationalization Guardrails

| Tempting Rationalization | Binding Rule | Engineering Rationale |
|---|---|---|
| *"Allow `git reset --hard` if the working tree has uncommitted bugs."* | **Block all hard resets; require explicit stashes or reverts.** | Hard resets permanently delete uncommitted code and worktree context. |
| *"Allow force pushes on feature branches."* | **Block all force pushes by default.** | Force pushing can overwrite teammate commits and destroy branch history. |
| *"Skip verifying the hook script with simulated input."* | **Mandatory simulated test pass.** | Syntax errors in hook scripts cause them to fail open, leaving the repo unprotected. |
