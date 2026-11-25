# Worktree mode

Follow the package policy linked from this skill's router before acting.

<a id="src-using-git-worktrees-intro"></a>

<a id="src-using-git-worktrees-using-git-worktrees"></a>
## Using Git Worktrees

<a id="src-using-git-worktrees-overview"></a>
### Overview

Ensure work happens in an isolated workspace. Prefer your platform's native worktree tools. Fall back to manual git worktrees only when no native tool is available.

On Windows run all snippets in a Bash-capable shell such as Git Bash; do not translate them to PowerShell.

**Core principle:** Detect existing isolation first. Then use native tools. Then fall back to git. Never fight the harness.

**Security boundary:** Consent to create or use a worktree authorizes checkout isolation only. It does not authorize dependency installation, compilation, tests, other repository code, network access, or credential use. A worktree is not a process, network, or credential sandbox.

**Announce at start:** "I'm using worktree mode to set up an isolated workspace."

<a id="src-using-git-worktrees-step-0-detect-existing-isolation"></a>
### Step 0: Detect Existing Isolation

**Before creating anything, check if you are already in an isolated workspace.**

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
BRANCH=$(git branch --show-current)
```

**Submodule guard:** As a belt-and-braces check before concluding "already in a worktree," confirm you are not inside a git submodule:

```bash
# If this returns a path, you're in a submodule, not a worktree — treat as normal repo
git rev-parse --show-superproject-working-tree 2>/dev/null
```

**If `GIT_DIR != GIT_COMMON` (and not a submodule):** You are already in a linked worktree. Skip to Step 2 (Setup Authority Review). Do NOT create another worktree.

Report with branch state:
- On a branch: "Already in isolated workspace at `<path>` on branch `<name>`."
- Detached HEAD: "Already in isolated workspace at `<path>` (detached HEAD, externally managed). Branch creation needed at finish time."

**If `GIT_DIR == GIT_COMMON` (or in a submodule):** You are in a normal repo checkout.

Has the user already indicated their worktree preference in your instructions? If not, ask for consent before creating a worktree:

> "Would you like me to set up an isolated worktree? It protects your current branch from changes."

Honor any existing declared preference without asking. If the user declines consent, work in place and skip to Step 2.

<a id="src-using-git-worktrees-step-1-create-isolated-workspace"></a>
### Step 1: Create Isolated Workspace

**You have two mechanisms. Try them in this order.**

<a id="src-using-git-worktrees-1a-native-worktree-tools-preferred"></a>
#### 1a. Native Worktree Tools (preferred)

The user has asked for an isolated workspace (Step 0 consent). Do you already have a way to create a worktree? It might be a tool with a name like `EnterWorktree`, `WorktreeCreate`, a `/worktree` command, or a `--worktree` flag. If you do, use it and skip to Step 2.

Native tools handle directory placement, branch creation, and cleanup automatically. Using `git worktree add` when you have a native tool creates phantom state your harness can't see or manage.

Only proceed to Step 1b if you have no native worktree tool available.

<a id="src-using-git-worktrees-1b-git-worktree-fallback"></a>
#### 1b. Git Worktree Fallback

**Only use this if Step 1a does not apply** — you have no native worktree tool available. Create a worktree manually using git.

<a id="src-using-git-worktrees-directory-selection"></a>
##### Directory Selection

Follow this priority order. Explicit user preference always beats observed filesystem state.

1. **Check your instructions for a declared worktree directory preference.** If the user has already specified one, use it without asking.

2. **Check for an existing project-local worktree directory:**
   ```bash
   ls -d .worktrees 2>/dev/null     # Preferred (hidden)
   ls -d worktrees 2>/dev/null      # Alternative
   ```
   If found, use it. If both exist, `.worktrees` wins.

3. **If there is no other guidance available**, default to `.worktrees/` at the project root.

<a id="src-using-git-worktrees-safety-verification-project-local-directories-only"></a>
##### Safety Verification (project-local directories only)

**MUST verify the chosen directory is ignored before creating worktree** — test a child path so the check works before the directory exists and with both `dir` and `dir/` gitignore styles:

```bash
# $LOCATION is the worktree parent chosen in Directory Selection
git check-ignore -q "$LOCATION/x"
```

**If NOT ignored:** Add to .gitignore, stage the change, and ask the user whether to commit it before proceeding.

**Why critical:** Prevents accidentally committing worktree contents to repository.

<a id="src-using-git-worktrees-create-the-worktree"></a>
##### Create the Worktree

```bash
# Determine path based on chosen location
path="$LOCATION/$BRANCH_NAME"

git worktree add "$path" -b "$BRANCH_NAME"
cd "$path"
```

**Sandbox fallback:** If `git worktree add` fails with a permission error (sandbox denial), tell the user the sandbox blocked worktree creation and you're working in the current directory instead. Continue to Step 2, but do not run setup or tests solely because of the fallback.

<a id="src-using-git-worktrees-step-2-project-setup"></a>
### Step 2: Review Project Setup Authority

Manifest presence tells you what to inspect, never what to execute. Before running any setup command, inspect the relevant files as data and identify:

- the exact proposed command and why it is needed;
- lockfiles, package sources, registries, downloads, and other network use;
- Node lifecycle scripts; Rust `build.rs`, procedural macros, and build dependencies; Python build backends, editable-install hooks, and VCS requirements; Go module replacements and tool directives; and equivalent executable hooks for other ecosystems;
- test commands, configuration, plugins, wrappers, fixtures, and generated-code steps that can execute repository or dependency code; and
- required credentials, environment variables, filesystem writes, global installs, services, or other host mutations.

Compare those effects with the user and host instructions. A request to create or use a worktree is not setup authority. A broader implementation or testing request may already authorize proportionate local setup and tests; honor that existing authority without asking again, but do not infer network access, credential exposure, global installation, or unrelated host mutation from repository files.

If the existing request does not authorize every required effect, stop and ask for the missing setup, execution, network, credential, or host-mutation decision. Show the exact proposed commands and the effects found during inspection. Do not run any of them while waiting.

When the effects are authorized, select only commands justified by the inspected project contract. Prefer locked, frozen, and offline modes where they satisfy the task. Use the host's least-privilege sandbox and a sanitized environment that exposes only required variables and credentials; do not disable sandboxing, pass unrelated secrets, change global package state, or treat the worktree as containment. Record the commands and effects actually authorized.

If setup is not authorized or not needed, skip it. Continue to Step 3 only to make its separate test-execution decision.

<a id="src-using-git-worktrees-step-3-verify-clean-baseline"></a>
### Step 3: Verify Clean Baseline

Baseline tests are valuable, but test runners, plugins, fixtures, and build steps execute repository-controlled code. A worktree-only request must not run tests. Inspect the test path as described in Step 2, determine the exact project-appropriate command, and run it only when the existing request authorizes its execution and effects or the user supplies the missing authority. Apply the same sandbox, environment, network, and credential controls.

If tests are not authorized, report that they were not run and why. The isolated checkout remains usable for inspection or the next authorized step.

**If tests fail:** Report failures, ask whether to proceed or investigate.

**If tests pass:** Report ready.

<a id="src-using-git-worktrees-report"></a>
#### Report

```
Worktree ready at <full-path>
Setup complete (<exact commands>)
Tests passing (<N> tests, 0 failures)
Ready to implement <feature-name>
```

When setup or tests were not run, replace the corresponding line with `Setup: not run — <reason>` or `Baseline tests: not run — <reason>`; never imply they passed.

<a id="src-using-git-worktrees-quick-reference"></a>
### Quick Reference

| Situation | Action |
|-----------|--------|
| Already in linked worktree | Skip creation (Step 0) |
| In a submodule | Treat as normal repo (Step 0 guard) |
| Native worktree tool available | Use it (Step 1a) |
| No native tool | Git worktree fallback (Step 1b) |
| `.worktrees/` exists | Use it (verify ignored) |
| `worktrees/` exists | Use it (verify ignored) |
| Both exist | Use `.worktrees/` |
| Neither exists | Check instruction file, then default `.worktrees/` |
| Directory not ignored | Add to .gitignore, stage, ask before commit |
| Permission error on create | Sandbox fallback, work in place; reassess setup/test authority |
| Worktree-only request | Create or use isolation; do not run setup or tests |
| Broader request already authorizes local execution | Inspect effects, then run only proportionate approved commands |
| Manifest or test configuration exists | Inspect hooks, dependencies, network, credentials, and exact commands |
| A required effect lacks authority | Stop and ask for that concrete decision |
| Tests fail during baseline | Report failures + ask |
| No recognized manifest (package.json/Cargo.toml/requirements.txt/pyproject.toml/go.mod) | Skip dependency install |

<a id="src-using-git-worktrees-common-rationalizations"></a>
### Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'm obviously not in a worktree — no need to check" | Run Step 0. Harness-created isolation fools eyeballing; the detection commands settle it. |
| "`git worktree add` is quicker than hunting for a native tool" | A native tool (e.g. `EnterWorktree`) owns placement, branching, and cleanup. Bypassing it is the #1 mistake — it creates phantom state your harness can't see or manage. |
| "The worktree directory is surely ignored already" | Run `git check-ignore`. An unignored worktree directory commits the whole tree into the repo. |
| "Any directory name works" | Explicit instructions beat an existing project-local directory, which beats the `.worktrees/` default. |
| "The manifest makes the setup command obvious" | Manifests and lockfiles are repository-controlled evidence. Inspect their executable and external effects before selecting a command. |
| "The workspace is fresh — run baseline tests now" | A baseline is useful only after its repository-code execution and other effects are authorized and inspected. |
