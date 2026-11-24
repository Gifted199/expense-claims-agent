---
description: Review the current diff against a phase's "Done when" in docs/PLAN.md (bugs, security, tests, secrets)
argument-hint: [phase-number]
allowed-tools: Bash(git status *), Bash(git diff *), Bash(git log *), Read, Grep, Glob
---

# Review Phase $0

## Current state
Branch and changed files:
!`git status --short --branch`

Diff of everything since the last commit, staged and unstaged:
!`git diff HEAD`

## Your task
Use the **reviewer** subagent if it's available, or review directly. Do not edit any files.

1. Read `docs/PLAN.md` → "Phase $0", especially its **Done when** line. Read the matching rows in `docs/SPEC.md` (§3 requirements, §4 tools, §5 guardrails) and the rules in `CLAUDE.md`.
2. Read new untracked files too, because `git diff HEAD` doesn't show them. Use `git status` to find them.
3. Report in this order:
   - **Done when:** list each criterion as ✅ met, ❌ not met or ⏳ manual or needs cloud, with evidence (file, command or test name).
   - **Bugs:** logic errors, edge cases, wrong types, broken imports. Give `file:line` and say why.
   - **Security:**
     - SQL built from strings instead of parameters
     - identity or approval decisions taken from LLM output
     - over-broad IAM or ports
     - missing guardrails
     - unsafe subprocess or deserialisation
   - **Tests:** is new behaviour covered? Do any tests hit real AWS or Azure (they must be mocked)?
   - **Secrets:** keys, tokens, passwords, connection strings or account IDs in the diff, `.env` files, or large or binary files.
   - **Git workflow:** are we on a `phase-*` branch and not `main`?
4. End with a verdict: **Ready to commit/PR** or **Fix first**, followed by a numbered fix list, most severe first.

Explain findings in plain words.
