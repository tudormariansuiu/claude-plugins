---
name: mr-ai-cost
description: Calculates the Claude Code token usage and API-equivalent USD cost of each git commit, from the local Claude Code session logs, and writes it as a footer in the GitLab merge request description. Use whenever the user asks how many tokens or how much money a commit, branch or MR cost, wants the MR cost footer refreshed ("update the MR cost", "the footer is stale", "I pushed by hand"), or asks why a commit shows $0 or how the cost is attributed — even if they just say "AI cost", "token spend" or "how expensive was this".
argument-hint: "[report|update|doctor] [branch]"
---

# MR AI cost

`scripts/mr_ai_cost.py`, in this skill's base directory, does all the work. The plugin's `PostToolUse` hook runs it after every `git push` (`hook` mode). When the user asks directly, run it yourself. Below, `SCRIPT` stands for the script's full path: this skill's base directory followed by `/scripts/mr_ai_cost.py`.

## How it works

Each commit's usage is stored as a **git note** (JSON) in `refs/notes/ai-cost` and pushed to the remote. Notes are the permanent, shared record. The MR footer is only a view built from them. On each `update`:

1. Fetch the remote's notes and merge them into the local ones.
2. For each of the MR's commits that has no note:
   - If it's a rebased copy of a commit that has a note (same author email and author date), copy that note.
   - Otherwise, if it's **your** commit (author email equals `git config user.email`), compute its usage from your local session logs and add a note.
   - Commits by other people are left for their authors to fill in.
3. Never overwrite an existing note. `--rebuild` is the only exception, and it recomputes only your own commits.
4. Push the notes. If someone else pushed notes first, merge theirs and retry once.
5. Write the footer built from every note on the MR's commits.

If an MR has none of your commits and no notes, the script writes nothing and prints a message naming the authors.

## Commands

Run these from inside the repo, or pass `--repo PATH`. `--branch NAME` defaults to the current branch.

```bash
python3 SCRIPT report             # print the footer; write nothing anywhere
python3 SCRIPT update             # add notes, push them, write the footer
python3 SCRIPT update --rebuild   # also recompute your existing notes
python3 SCRIPT doctor             # check git email, glab install and login, session logs
```

If a command fails with a setup problem (glab missing or not logged in, no git email), run `doctor` and show its output: it names each fix.

Use `--rebuild` only when your own numbers are known to be wrong, e.g. after a fix to `pricing.json`.

## When invoked as `/mr-ai-cost`

Typing the command counts as the user's permission for the action it names, so run it without asking:

| Typed | Run |
|---|---|
| `/mr-ai-cost` | `update` on the current branch: the same thing the hook does after a push, for pushes it didn't see (terminal, lazygit, IDE) |
| `/mr-ai-cost update <branch>` | `update --branch <branch>` |
| `/mr-ai-cost update --rebuild [branch]` | `update --rebuild`, adding `--branch <branch>` if one was given |
| `/mr-ai-cost report [branch]` | `report`, adding `--branch <branch>` if one was given |
| `/mr-ai-cost doctor` | `doctor` |

Show the script's output line (or the printed footer for `report`) and stop.

## When loaded from a plain-language request

Use `report` when the user only wants the numbers. Use `update` when they ask for the MR to be changed ("update the MR cost", "the footer is stale"). `update` edits a shared MR and pushes notes, so if the request doesn't clearly ask for a change, report first and offer to update.

## How costs are attributed (so you can explain the numbers)

- **Source:** every Claude Code session log under `~/.claude/projects/`. Each session's main log and its subagent logs are merged into one timeline, so planning and review tokens in the main session count too.
- **Commit boundaries:** every Bash call that ran `git commit` (or `cherry-pick`, `revert` or `merge`) and succeeded. A commit gets every token spent in that session since the previous successful commit call. If a commit call fails, its tokens carry over to the next successful one. Tokens spent after a session's last commit aren't counted anywhere.
- **Matching a commit to a call:** a commit is matched when its hash is printed in the call's output, or when its *author date* falls inside the call's time window. Commits made with `git commit -q` print no hash, so the time window matters.
- **One call, several commits:** the call's tokens are split evenly between them.
- **Rebases:** a rebased commit gets its note from the pre-rebase commit with the same author and author date (plus the same subject, when two commits share the same second). If that is still ambiguous, no note is guessed; `--rebuild` recomputes your own from the logs. GitLab's "squash on merge" creates a new commit with no note.
- **Commits with no numbers ("-"):** either the commit was made without Claude Code, or its author hasn't run mr-ai-cost yet. The footer names those authors. Once a note is pushed, it no longer depends on anyone's local logs, which Claude Code deletes after 30 days by default.
- **Price:** `pricing.json` holds the API list prices per 1M tokens. Cache writes are priced separately for 5-minute and 1-hour caching, fast mode costs 2×, and web searches cost 1 cent (USD) each. The figure is what this usage *would* cost on the API, not what a subscription actually charged. A model missing from `pricing.json` shows a ⚠️ note and is left out of the USD total. If that happens, add the model's rates to `pricing.json` and don't guess.

Anyone can read the stored notes with `git fetch origin refs/notes/ai-cost:refs/notes/ai-cost && git log --notes=ai-cost`.

## The footer

The footer sits between `<!-- ai-cost:start -->` and `<!-- ai-cost:end -->`. Each run rebuilds it from the notes and replaces only that block. The rest of the description is left alone, and re-running is always safe. If a branch has no open MR, the script prints a note and writes nothing (not even notes). If a branch has several open MRs, it updates each of them.

## The hook

The plugin installs it (`hooks/hooks.json`); nothing goes into `settings.json`. It ignores every command that isn't a `git push`, and it always exits 0, so it can never block a tool call. Teammates who want to add their own numbers need the same plugin.
