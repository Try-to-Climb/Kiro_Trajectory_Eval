# evalkit/examples/ — Ready-to-run sample data

Pre-normalized action sequences so you can run `evalkit` immediately without installing hooks or having a real Kiro session.

## Files

### `sample.normalized.json`

A hand-crafted `TraceIR` payload with 3 actions (one `read_file`, one `run_command`, one `summarize`), which happens to match all 3 rules in `evalkit/rules/example-minimal.checks.json`.

**Structure**:
```json
{
  "actions": [
    { "idx": 0, "action": "read_file",   "path": "/home/user/project/README.md", ... },
    { "idx": 1, "action": "run_command", "command": "python3 build.py",           ... },
    { "idx": 2, "action": "summarize",   ...                                       }
  ]
}
```

Full field spec lives in [`evalkit/normalize/README.md`](../normalize/README.md) and [`LLM_GUIDE.md`](../LLM_GUIDE.md).

## Usage

```bash
cd evalkit
python3 -m rule.runner rules/example-minimal.checks.json examples/sample.normalized.json
```

Expected: `PASS` / health `1.0`.

## Extending

To feed richer samples to your own rules, two paths:
1. **Dump a real session**: `python3 -m normalize.cli dump --official <session-id> > my.normalized.json`, then sanitize manually.
2. **Hand-write**: mimic `sample.normalized.json` — each entry in `actions[]` needs `idx`, `action`, and the required fields for that action type (`read_file` needs `path`, `run_command` needs `command`, etc.).
