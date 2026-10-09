# Comment prepared for tree-sitter/tree-sitter-bash#346

<!-- LOCAL NOTES — above the `---` is ours, not part of the comment.

Target: https://github.com/tree-sitter/tree-sitter-bash/issues/346
Status: not posted yet
Body:   sed -n '/^---$/,$p' comment.md | tail -n +2

Five rows measured on tree-sitter-bash 0.25.1 / tree-sitter 0.26.0. bash -n
accepts all of them.
-->

---

A redirect before the pipe fails the same way. This issue lists
`cat <<'EOF' | tr a-z A-Z` as parsing correctly, and it does -- but not once a
redirect comes first:

```bash
cat <<'EOF' 2>&1 | tail -5
hello
EOF
```

On 0.25.1, body `hello` / `EOF`, only the first line varying:

| first line | parses |
|---|---|
| `cat <<'EOF' \| tail -5 \| wc -l` | yes — pipes alone |
| `cat <<'EOF' > f > g` | yes — redirects alone |
| `cat <<'EOF' > f && echo x` | yes |
| `cat <<'EOF' 2>&1 \| tail -5` | **no** |
| `cat <<'EOF' > f \| tail -5` | **no** |

So a redirect followed by a pipe fails, as it does followed by `;`. Followed by
`&&`, or by another redirect, is fine.

We hit it parsing recorded terminal sessions: 135 of 5613 commands, nearly all of
them `python3 - <<'EOF' 2>&1 | head -30`. The shortest is 315 characters, so length
is not involved.
