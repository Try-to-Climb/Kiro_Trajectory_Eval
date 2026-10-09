"""Declarative intent rules -> compiler (desugar) targeting the 7 low-level checkers.

Users write "intents" (reads/runs/write/dispatches/pipeline/before/never_*/
if_claims...then_*/judge); this module compiles them into low-level check
dicts that the existing checkers understand, and hands them to the engine.

Design points:
  - **Non-destructive**: a rule that already carries `type` is passed through
    unchanged (advanced users / low-level rules keep working).
  - **Mechanism-agnostic**: `reads` compiles into a "pure regex Exists" (not
    bound to read_file); a shell `cat` also counts -- welding shut a hole we
    stepped in before, so users can't write it wrong.
  - **glob replaces regex**: `*` -> anything, `?` -> single char; users never
    touch backslashes. `regex:` is kept as an escape hatch.
  - **Visible failure**: unrecognized intents compile into __dsl_error__ so
    run_check raises the error visibly, never silently.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_SEV = {"required", "recommended", "forbidden", "optional"}

# -- intent -> action mapping (overridable via rules/intent_map.json, user-editable) --
_DEFAULT_MAP = {
    "read_actions": ["read_file", "list_dir", "read_image", "search_content", "search_files"],
    "read_shell_verbs": ["cat", "bat", "sed", "awk", "grep", "egrep", "rg", "head", "tail",
                          "less", "more", "nl", "wc", "xxd", "od", "jq", "yq"],
    "read_idioms": [r"json\.load", r"\.read\(", r"\.readlines\(", r"open\([^),]*\)"],
    "run_action": "run_command",
    "run_prefixes": [r"timeout\s+\d+\s+", r"sudo\s+", r"env\s+", r"\w+=\S+\s+",
                     r"nice\s+(?:-n\s+\d+\s+)?", r"exec\s+", r"stdbuf\s+\S+\s+", r"python3?\s+-m\s+"],
    "write_actions": ["create_file", "modify_file", "append_file"],
    "write_shell_ops": [">>", ">", "tee", "dd"],
    "write_idioms": [r"open\([^)]*['\"][wa]", r"\.write\(", r"\.writelines\(",
                     r"json\.dump\(", r"\.to_csv\(", r"\.to_json\("],
    "dispatch_action": "spawn_subagent",
}
_MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "rules", "intent_map.json")
_MAP_CACHE: dict | None = None


def load_intent_map(path: str | None = None) -> dict:
    """Load the intent-to-action map: rules/intent_map.json overrides the
    built-in defaults; if missing/corrupt, fall back to defaults."""
    p = path or _MAP_PATH
    m = dict(_DEFAULT_MAP)
    try:
        cfg = json.load(open(p, encoding="utf-8"))
        for k, v in cfg.items():
            if not k.startswith("_"):
                m[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    return m


def _map() -> dict:
    global _MAP_CACHE
    if _MAP_CACHE is None:
        _MAP_CACHE = load_intent_map()
    return _MAP_CACHE


def glob_to_regex(g: str) -> str:
    """glob -> regex (unanchored, meant for re.search). `*` -> `.*`, `?` -> `.`,
    everything else is escaped."""
    out = []
    for ch in g:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(s)).strip("_")[:40] or "x"


def reads_regex(target_glob: str) -> str:
    """Build the regex for "read target" (based on the editable map):
       1. read-semantic actions (read_file/list_dir/...) mention target; or
       2. run_command uses a read-verb (cat/grep/...) touching target; or
       3. the command contains both a "read idiom" (json.load / .read() /
          open(single arg)) and the target.
    Excludes writes (create/modify/open(...,'w')), deletes, and mere mentions.
    """
    t = glob_to_regex(target_glob)
    m = _map()
    acts = "|".join(re.escape(a) for a in m["read_actions"])
    verbs = "|".join(re.escape(v) for v in m["read_shell_verbs"])
    parts = [
        rf"(?s:^(?:{acts})\b.*{t})",          # 1. read-tool actions (. crosses newlines to path)
        rf"(?:\b(?:{verbs})\b[^\n]*{t})",      # 2. shell read-verb + target on same line
    ]
    idioms = m.get("read_idioms") or []
    if idioms:
        idi = "|".join(idioms)                 # idioms are themselves regex, do not escape
        # 3. within the same action, a read idiom and the target both appear
        #    (use [\s\S] to cross newlines, allowing multi-line Python).
        parts.append(rf"(?=[\s\S]*(?:{idi}))(?=[\s\S]*{t})")
    return "|".join(parts)


def run_program_regex(target_glob: str) -> str:
    """Build the program regex for "executed target": after optionally
    stripping prefixes (timeout/sudo/...), the subcommand starts with target.
    Avoids "mentioned in command" or substring false matches."""
    t = glob_to_regex(target_glob)
    prefixes = _map().get("run_prefixes") or []
    pref = rf"(?:{'|'.join(prefixes)})*" if prefixes else ""
    return pref + t


def writes_regex(target_glob: str) -> str:
    """Build the regex for "wrote target" (Forbidden rules need full coverage,
    so we cover many syntactic forms):
       1. tool writes (create_file/modify_file/append_file) mention target; or
       2. shell redirection writes: > / >> / tee / dd followed on same line by target; or
       3. write idioms: open(...,'w') / .write() / json.dump( etc. together with target.
    Residual blind spot: writes done inside a script (target does not appear
    in the command) cannot be caught.
    """
    t = glob_to_regex(target_glob)
    m = _map()
    wacts = "|".join(re.escape(a) for a in m["write_actions"])
    ops = m.get("write_shell_ops") or [">>", ">", "tee", "dd"]
    op_alt = "|".join(re.escape(o) if o in (">", ">>") else rf"\b{re.escape(o)}\b" for o in ops)
    parts = [
        rf"(?s:^(?:{wacts})\b.*{t})",          # 1. tool writes
        rf"(?:{op_alt})[^\n]*{t}",              # 2. shell redirection write + target on same line
    ]
    idioms = m.get("write_idioms") or []
    if idioms:
        idi = "|".join(idioms)
        parts.append(rf"(?=[\s\S]*(?:{idi}))(?=[\s\S]*{t})")   # 3. write idiom + target
    return "|".join(parts)


def _severity(cp: dict, default: str) -> str:
    imp = cp.get("importance") or cp.get("severity")
    return imp if imp in _SEV else default


def _phrase_to_matcher(phrase: str) -> dict:
    """The embedded phrase `"<verb> <target>"` inside pipeline / before ->
    one matcher dict.

    landmark (reads/write/sees...) -> pure regex; runs -> run_command;
    dispatches -> spawn.
    """
    parts = str(phrase).strip().split(None, 1)
    verb = parts[0] if parts else ""
    target = parts[1].strip() if len(parts) > 1 else "*"
    rx = glob_to_regex(target) if target and target != "*" else None
    if verb in ("runs", "run"):
        m = {"action": "run_command"}
        if rx:
            m["program"] = run_program_regex(target)
        return m
    if verb in ("dispatches", "dispatch", "spawns"):
        m = {"action": "spawn_subagent"}
        if rx:
            m["regex"] = rx
        return m
    if verb in ("reads", "read", "sees"):
        return {"regex": reads_regex(target if (target and target != "*") else "")}
    # Anything else (write/...) is treated as a "landmark": some content just
    # needs to appear, mechanism-agnostic.
    return {"regex": rx} if rx else {"regex": glob_to_regex(verb)}


def _err(cid: str, msg: str) -> dict:
    return {"id": cid or "?", "type": "__dsl_error__", "_msg": msg}


def desugar_check(cp: dict) -> dict:
    """Compile one intent rule into a low-level check dict. Rules that already
    carry `type` are returned unchanged."""
    if not isinstance(cp, dict):
        return _err("?", f"rule must be an object, got {type(cp).__name__}")
    if "type" in cp:                     # low-level checker, pass through
        return cp

    cid = cp.get("id")
    reason = cp.get("as") or cp.get("reason_tmpl")

    def build(extra: dict, default_sev: str, cid_hint: str, force_sev: str | None = None) -> dict:
        out: dict[str, Any] = {
            "id": cid or cid_hint,
            "severity": force_sev or _severity(cp, default_sev),
        }
        if reason:
            out["reason_tmpl"] = reason
        out.update(extra)
        return out

    # -- single-verb intents --
    if "reads" in cp:
        g = cp["reads"]
        return build({"type": "Exists", "match": {"regex": reads_regex(g)}},
                     "recommended", "reads_" + _slug(g))

    if "touches" in cp:                  # loose: file appears anywhere in the run (read/write/mention all count), used as a landmark
        g = cp["touches"]
        return build({"type": "Exists", "match": {"regex": glob_to_regex(g)}},
                     "recommended", "touches_" + _slug(g))

    if "runs" in cp:
        g = cp["runs"]
        return build({"type": "Exists", "match": {"action": "run_command", "program": run_program_regex(g)}},
                     "recommended", "runs_" + _slug(g))

    if "write" in cp:
        name = cp["write"]
        return build({"type": "Produces", "name": name, "min_count": cp.get("at_least", 1)},
                     "recommended", "write_" + _slug(name))

    if "dispatches" in cp:
        x = cp["dispatches"]
        match = {"action": "spawn_subagent"}
        if x and x != "*":
            match["regex"] = glob_to_regex(x)
        # If only at_most is given (no at_least), lower bound = 0 (i.e. "<= N",
        # allowing 0 occurrences); if neither is given -> lower bound = 1
        # (must dispatch).
        if "at_least" in cp:
            lo = cp["at_least"]
        elif "at_most" in cp:
            lo = 0
        else:
            lo = 1
        extra = {"type": "Count", "match": match, "min_count": lo}
        if "at_most" in cp:
            extra["max_count"] = cp["at_most"]
        return build(extra, "required", "dispatches_" + _slug(x))

    if "pipeline" in cp:
        steps = cp["pipeline"]
        if not (isinstance(steps, list) and steps):
            return _err(cid, "pipeline must be a non-empty list of \"<verb> <target>\" phrases")
        return build({"type": "Milestone", "steps": [_phrase_to_matcher(s) for s in steps]},
                     "required", "pipeline")

    if "before" in cp:
        pair = cp["before"]
        if not (isinstance(pair, list) and len(pair) == 2):
            return _err(cid, "before must be a list of exactly two phrases [\"do first\", \"do after\"]")
        return build({"type": "Before", "a": _phrase_to_matcher(pair[0]),
                      "b": _phrase_to_matcher(pair[1])},
                     "required", "before_" + _slug(pair[0]))

    # -- never_* (forbidden, severity is always forbidden) --
    for key, mk in (("never_runs", lambda g: {"action": "run_command", "regex": glob_to_regex(g)}),
                    ("never_reads", lambda g: {"regex": glob_to_regex(g)}),
                    ("never_writes", lambda g: {"regex": writes_regex(g)}),
                    ("never_dispatches", lambda g: {"action": "spawn_subagent", "regex": glob_to_regex(g)})):
        if key in cp:
            extra = {"type": "Forbidden", "match": mk(cp[key])}
            if "except" in cp:
                extra["exclude"] = {"regex": glob_to_regex(cp["except"])}
            return build(extra, "forbidden", key + "_" + _slug(cp[key]), force_sev="forbidden")

    # -- if_claims ... then_* (catch fabrication / conditional implication) --
    if "if_claims" in cp:
        a = {"regex": glob_to_regex(cp["if_claims"])}
        if "then_runs" in cp:
            b = {"action": "run_command", "regex": glob_to_regex(cp["then_runs"])}
        elif "then_dispatches" in cp:
            b = {"action": "spawn_subagent", "regex": glob_to_regex(cp["then_dispatches"])}
        elif "then_write" in cp:
            b = {"regex": glob_to_regex(cp["then_write"])}
        elif "then_reads" in cp:
            b = {"regex": glob_to_regex(cp["then_reads"])}
        else:
            return _err(cid, "if_claims must be paired with one of then_runs / then_dispatches / then_write / then_reads")
        return build({"type": "IfThen", "a": a, "b": b}, "recommended",
                     "ifclaims_" + _slug(cp["if_claims"]))

    # -- LLM judge --
    if "judge" in cp:
        extra = {"type": "LLMJudge", "dimension": cp["judge"],
                 "pass_threshold": cp.get("pass_threshold", 0.75)}
        return build(extra, "recommended", "judge_" + _slug(cp["judge"]))

    return _err(cid, "unrecognized intent (no type and no known intent keyword). Known: "
                     "reads/runs/write/dispatches/pipeline/before/"
                     "never_runs/never_reads/never_writes/never_dispatches/if_claims+then_*/judge")


def desugar_checks(checks: list) -> list:
    return [desugar_check(c) for c in (checks or [])]
