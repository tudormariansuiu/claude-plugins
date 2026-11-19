# claude-plugins

Claude Code plugins for our team.

| Plugin | What it does |
|---|---|
| [`mr-ai-cost`](plugins/mr-ai-cost) | Records the Claude Code token usage and API-equivalent USD cost of each git commit, and shows it as a footer on the GitLab merge request |

## mr-ai-cost

After every `git push` that Claude makes, the plugin works out what each of your commits cost in Claude Code usage. It stores that as a **git note** on the commit, shared through the remote, and updates a footer at the bottom of the MR description:

> **🤖 AI usage:** 7.4M tokens · **$4.48** (API-equivalent) · 4 of 5 commits
> ▸ Per-commit breakdown (commit, subject, model, input/output/cache tokens, USD)

Each person records only **their own** commits, from **their own** Claude Code session logs. Nobody's numbers are overwritten, and on shared MRs everyone's notes add up.

### Requirements

- Claude Code
- `python3` (standard library only)
- [`glab`](https://gitlab.com/gitlab-org/cli#installation), logged in to the GitLab host your repos use: `glab auth login --hostname <your-gitlab-host>`
- `git config user.email` set to the address your commits use

### Install

In Claude Code, run:

```
/plugin marketplace add tudormariansuiu/claude-plugins
/plugin install mr-ai-cost@tudor-plugins
```

Answer `y` to add the marketplace, then pick the **user** scope so the plugin works in all your repos. The hook is active straight away, with nothing to add to `settings.json`.

Outside Claude Code: `claude plugin install mr-ai-cost --marketplace tudormariansuiu/claude-plugins`.

### Check your setup

Inside one of your GitLab repos, in Claude Code:

```
/mr-ai-cost doctor
```

```
✓ git repo: /home/you/repos/my-project
✓ git user.email: you@company.com (your commits must use this address)
✓ Claude Code session logs: /home/you/.claude/projects
✓ glab installed
✓ glab logged in to gitlab.example.com
All set.
```

### Use

Usually you don't need to do anything: the hook updates the MR after each push Claude makes. Pushes made outside Claude (terminal, lazygit, IDE) aren't seen by the hook; for those:

| Command | Does |
|---|---|
| `/mr-ai-cost` | Update the footer for the current branch (add notes for your new commits, push them, rewrite the footer) |
| `/mr-ai-cost report` | Print the footer, write nothing |
| `/mr-ai-cost update --rebuild` | Recompute **your own** notes, e.g. after a price fix |
| `/mr-ai-cost doctor` | Check the setup |

You can also just ask: "how much did this branch cost?"

### Good to know

- **Only your own commits.** Commits by people without the plugin show "-", and the footer names them.
- **The figure is API-equivalent.** It's what the usage would cost at Anthropic API list prices (`pricing.json`), not what a subscription actually charges.
- **Notes are visible to everyone with access to the repo:** `git fetch origin refs/notes/ai-cost:refs/notes/ai-cost && git log --notes=ai-cost`.
- **Rebases keep the numbers.** GitLab's "squash on merge" creates a new commit without a note; the numbers stay on the original commits.
- **Write the note soon after committing.** Claude Code deletes session logs after 30 days by default. Once a note is pushed it no longer needs the logs.

### Update / uninstall

```
/plugin marketplace update tudor-plugins
/plugin uninstall mr-ai-cost@tudor-plugins
```

### Development

```bash
python3 -I plugins/mr-ai-cost/tests/notes_test.py   # 13 checks against throwaway git repos, no network
```

Bump `version` in `plugins/mr-ai-cost/.claude-plugin/plugin.json` with each change, so `/plugin marketplace update` picks it up.
