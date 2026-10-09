# Security Policy

## Reporting a Vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities.

Instead, open a [GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository, or contact the maintainer via a private channel listed on the maintainer's GitHub profile.

We aim to acknowledge reports within 5 business days.

## Data handling — what this software records

If you enable the optional hook collector (`install.sh` + `hooks/trace-hook.sh`), it records the following per Kiro CLI session to local files under `~/agent-trace/traces/<session-id>/`:

- **`userPromptSubmit` events** — the full user prompt text.
- **`preToolUse` / `postToolUse` events** — the tool name, tool arguments (including file paths, command strings, and content), and tool output (stdout/stderr) up to the collector's size limit.
- **`agentSpawn` / `stop` events** — session metadata (cwd, timestamps).

These files can contain:

- Source code contents (from `read` tool)
- Command outputs which may include environment variables, credentials, or other secrets
- User conversation content

**The collector never uploads data anywhere.** All trace files stay on your local filesystem. You are responsible for their access control and retention.

### Reducing what is captured

- `KIRO_TRACE_DIR` — direct traces to a specific path (e.g. an encrypted volume).
- `config/policy.json` — the collector honors `preToolUse` denials, so blocking a tool prevents its capture.
- To disable the collector entirely, do not run `install.sh` (or remove the hooks from your agent config). Evaluation still works using Kiro's built-in session records via the `--official` flag.

### Cleaning up

```bash
kiro-trace clean       # interactive prompt
rm -rf ~/agent-trace/traces/<session-id>   # manual
```

## Threat model

**In scope**:
- Vulnerabilities in the evaluator code (evalkit, goal) that could execute arbitrary code from crafted trace input.
- Injection issues in the hook collector shell scripts.
- Path traversal / arbitrary file read in the loaders.

**Out of scope**:
- The security posture of the agents being evaluated. Trace files reflect whatever the agent did — evaluation itself does not sandbox or filter this content.
- Local privilege escalation through file permissions on the trace directory (this is a filesystem administration matter).
- Third-party tools invoked by hook scripts (`jq`, `kiro-cli`).

## Supported versions

Only the latest release on `master` is actively maintained.
