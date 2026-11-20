#!/usr/bin/env python3
"""Record Claude Code token usage per git commit as git notes, and show it as a GitLab MR footer.

Usage:
  mr_ai_cost.py report [--repo PATH] [--branch NAME]   # print the footer; write nothing
  mr_ai_cost.py update [--repo PATH] [--branch NAME]   # add notes for your commits, push them, write the footer
  mr_ai_cost.py update --rebuild ...                   # same, but recompute your own existing notes
  mr_ai_cost.py doctor [--repo PATH]                   # check this machine/repo is set up
  mr_ai_cost.py hook                                   # PostToolUse hook entry (JSON on stdin)

Attribution: in each Claude Code session (main log + its subagent logs merged into one
timeline), every token spent since the previous successful `git commit` call goes to the
commit that call made. Per-commit results live in refs/notes/ai-cost (JSON), shared via the
remote; each person only writes notes for commits they authored, and never overwrites one.
"""
import argparse, collections, datetime, glob, json, os, re, shlex, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.expanduser("~/.claude/projects")
START, END = "<!-- ai-cost:start -->", "<!-- ai-cost:end -->"
COMMIT_RE = re.compile(r"^\[[^\]\n]+? ([0-9a-f]{7,40})\] ", re.M)
# `git [-C dir] [-c k=v] commit|cherry-pick|revert|merge` as an actual command: at the start of the
# command line or after a shell separator, not merely mentioned inside a string or a heredoc.
GIT_COMMIT_RE = re.compile(
    r"(?:^|&&|\|\||[;|\n(]|\b(?:do|then|else)\b)\s*git"
    r"""(?:\s+-[Cc]\s+(?:[^\s"']|"[^"]*"|'[^']*')+)*"""  # -C dir / -c key="value with spaces"
    r"\s+(?:commit|cherry-pick|revert|merge)\b")
TOKEN_FIELDS = ("input", "output", "cache_w", "cache_r")


# ---------- session logs -> per-commit usage ----------

def _blank():
    return {"input": 0, "output": 0, "cache_w": 0, "cache_r": 0, "usd": 0.0, "models": set(), "unpriced": set()}


def _cost(usage, model, pricing):
    p = pricing["models"].get(model)
    if p is None:
        return None
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    w5m = cc.get("ephemeral_5m_input_tokens")
    if w5m is None:  # older logs without the TTL breakdown: treat all writes as 5-minute
        w5m = (usage.get("cache_creation_input_tokens", 0) or 0) - w1h
    inp = usage.get("input_tokens", 0) or 0
    rd = usage.get("cache_read_input_tokens", 0) or 0
    if "long_prompt_threshold" in p and inp + rd + w5m + w1h > p["long_prompt_threshold"]:
        p = p["long_prompt"]
    usd = (inp * p["input"] + (usage.get("output_tokens", 0) or 0) * p["output"]
           + w5m * p["cache_write_5m"] + w1h * p["cache_write_1h"] + rd * p["cache_read"]) / 1e6
    if usage.get("speed") == "fast":
        usd *= pricing.get("fast_mode_multiplier", 1)
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests", 0) or 0
    return usd + searches * pricing.get("web_search_per_request", 0)


def _epoch(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _session_events(paths):
    """Return time-ordered (ts, kind, payload) for one session tree, de-duplicated.

    kind 0 = usage (model, usage); kind 1 = a successful `git ... commit` call
    (start_epoch, end_epoch, hashes printed in its output).
    """
    usage = {}    # message id -> (ts, model, usage); last occurrence wins
    commits = {}  # tool_use id -> start ts of a Bash call that ran git commit
    results = {}  # tool_use id -> (ts, text, is_error)
    for path in paths:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if d.get("type") == "assistant":
                    if msg.get("usage") and msg.get("model") != "<synthetic>":
                        usage[msg.get("id") or d.get("uuid")] = (d.get("timestamp", ""), msg.get("model"), msg["usage"])
                    for b in content if isinstance(content, list) else []:
                        cmd = str((b.get("input") or {}).get("command", "")) if b.get("type") == "tool_use" else ""
                        if GIT_COMMIT_RE.search(cmd):
                            commits[b.get("id")] = d.get("timestamp", "")
                elif d.get("type") == "user" and isinstance(content, list):
                    for b in content:
                        if b.get("type") == "tool_result":
                            c = b.get("content")
                            text = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                            results[b.get("tool_use_id")] = (d.get("timestamp", ""), text, bool(b.get("is_error")))
    events = [(ts, 0, (model, u)) for ts, model, u in usage.values() if ts]
    for tid, start in commits.items():
        if tid in results and start and not results[tid][2]:  # failed commits don't reset the count
            ts, text, _ = results[tid]
            events.append((ts, 1, (_epoch(start), _epoch(ts), set(COMMIT_RE.findall(text)))))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def commit_usage(pricing):
    """List of commit calls across all Claude Code sessions: {start, end, hashes, bucket}."""
    sessions = collections.defaultdict(list)
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        sessions[path[:-6]].append(path)
    for path in glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl")):
        sessions[os.path.dirname(os.path.dirname(path))].append(path)
    out = []
    for paths in sessions.values():
        bucket = _blank()
        for _, kind, payload in _session_events(paths):
            if kind == 0:
                model, u = payload
                bucket["input"] += u.get("input_tokens", 0) or 0
                bucket["output"] += u.get("output_tokens", 0) or 0
                bucket["cache_w"] += u.get("cache_creation_input_tokens", 0) or 0
                bucket["cache_r"] += u.get("cache_read_input_tokens", 0) or 0
                bucket["models"].add(model)
                usd = _cost(u, model, pricing)
                if usd is None:
                    bucket["unpriced"].add(model)
                else:
                    bucket["usd"] += usd
            else:
                start, end, hashes = payload
                out.append({"start": start, "end": end, "hashes": hashes, "bucket": bucket})
                bucket = _blank()
    return out


def attribute(commits, calls):
    """Map full sha -> bucket. A commit belongs to the call that printed its hash, or whose
    time window contains its author date. A call that made several commits splits evenly."""
    matched = collections.defaultdict(list)  # call index -> [sha]
    for sha, author_ts, *_ in commits:
        by_hash = [i for i, c in enumerate(calls) if any(sha.startswith(h) for h in c["hashes"])]
        by_time = [i for i, c in enumerate(calls) if c["start"] - 2 <= author_ts <= c["end"] + 2]
        # prefer a printed hash; then a call not yet used (parallel calls can share the same second)
        pick = by_hash or [i for i in by_time if i not in matched] or by_time
        if pick:
            matched[pick[0]].append(sha)
    out = {}
    for i, shas in matched.items():
        b, n = calls[i]["bucket"], len(shas)
        share = {k: b[k] / n for k in (*TOKEN_FIELDS, "usd")}
        share.update(models=b["models"], unpriced=b["unpriced"])
        for sha in shas:
            out[sha] = share
    return out


# ---------- git / GitLab ----------

def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def open_mrs(repo, branch):
    return json.loads(sh(["glab", "mr", "list", "--source-branch", branch, "-F", "json"], repo) or "[]")


def branch_remote(repo, branch):
    return sh(["git", "config", f"branch.{branch}.remote"], repo, check=False) or "origin"


def mr_commits(repo, remote, branch, target):
    """Commits in the MR: on the pushed source branch but not on the target, oldest first.
    Each is (sha, author_epoch, date, subject, author_email)."""
    sh(["git", "fetch", "--quiet", remote, target, branch], repo, check=False)
    log = sh(["git", "log", "--reverse", "--format=%H%x1f%at%x1f%ad%x1f%s%x1f%ae", "--date=short",
              f"{remote}/{target}..{remote}/{branch}"], repo)
    out = []
    for line in log.splitlines():
        sha, at, date, subject, email = line.split("\x1f", 4)
        out.append((sha, int(at), date, subject, email.lower()))
    return out


# ---------- git notes: the shared per-commit record ----------

NOTES = "refs/notes/ai-cost"
REMOTE_NOTES = "refs/notes/ai-cost-remote"


def ref_sha(repo, ref):
    return sh(["git", "rev-parse", "--verify", "--quiet", ref], repo, check=False) or None


def fetch_notes(repo, remote):
    """Fetch the remote's notes into REMOTE_NOTES (left untouched if the remote has none)."""
    sh(["git", "fetch", "--quiet", remote, f"+{NOTES}:{REMOTE_NOTES}"], repo, check=False)


def merge_notes(repo):
    """Bring the fetched remote notes into the local notes ref. On the same commit, keep ours."""
    remote, local = ref_sha(repo, REMOTE_NOTES), ref_sha(repo, NOTES)
    if not remote or remote == local:
        return
    if not local:
        sh(["git", "update-ref", NOTES, remote], repo)
    else:
        sh(["git", "notes", f"--ref={NOTES}", "merge", "--quiet", "-s", "ours", REMOTE_NOTES], repo)


def read_notes(repo, ref):
    """All notes on a ref: {commit sha: dict}. Unreadable notes are skipped."""
    if not ref_sha(repo, ref):
        return {}
    pairs = [l.split() for l in sh(["git", "notes", f"--ref={ref}", "list"], repo).splitlines() if l]
    if not pairs:
        return {}
    r = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, capture_output=True, text=True,
                       input="\n".join(blob for blob, _ in pairs) + "\n")
    out, data = {}, r.stdout
    for blob, sha in pairs:
        header, _, data = data.partition("\n")
        size = int(header.split()[2])
        body, data = data[:size], data[size + 1:]
        try:
            out[sha] = json.loads(body)
        except ValueError:
            pass
    return out


def write_note(repo, sha, note):
    sh(["git", "notes", f"--ref={NOTES}", "add", "-f", "-m", json.dumps(note, sort_keys=True), sha], repo)


def push_notes(repo, remote):
    """Push the local notes ref; if someone else pushed first, merge theirs and retry once."""
    for _ in range(2):
        if ref_sha(repo, NOTES) in (None, ref_sha(repo, REMOTE_NOTES)):
            return
        r = subprocess.run(["git", "push", "--quiet", remote, f"{NOTES}:{NOTES}"], cwd=repo,
                           capture_output=True, text=True)
        if r.returncode == 0:
            sh(["git", "update-ref", REMOTE_NOTES, NOTES], repo)
            return
        fetch_notes(repo, remote)
        merge_notes(repo)
    raise RuntimeError(f"could not push {NOTES}: {r.stderr.strip()}")


def note_subject(repo, sha, note):
    """The commit subject a note belongs to: stored in newer notes, else read from the old commit."""
    return note.get("subject") or sh(["git", "log", "-1", "--format=%s", sha], repo, check=False)


def make_note(bucket, email, author_ts, subject):
    return {"v": 1, "author": email, "author_ts": author_ts, "subject": subject,
            **{k: round(bucket[k]) for k in TOKEN_FIELDS}, "usd": round(bucket["usd"], 4),
            "models": sorted(bucket["models"]), "unpriced": sorted(bucket["unpriced"])}


def fill_notes(repo, commits, notes, me, rebuild):
    """Decide the notes to add for this MR's commits. Returns {sha: note} of new notes.

    - a commit that already has a note keeps it (only `rebuild` recomputes your own)
    - a rebased commit takes the note of its pre-rebase twin (same author + author date)
    - your own commits without a note are computed from your local session logs
    - other people's commits are left for their authors
    """
    twins = collections.defaultdict(list)  # (author, author date) -> [(noted sha, note)]
    for noted, n in notes.items():
        twins[(n.get("author"), n.get("author_ts"))].append((noted, n))
    todo = [c for c in commits if c[0] not in notes or (rebuild and c[4] == me)]
    new = {}
    for sha, at, _, subject, email in todo:
        if rebuild and email == me:  # --rebuild recomputes your own commits from the logs instead
            continue
        cands = twins.get((email, at), [])
        if len(cands) > 1:  # same author and second (e.g. parallel commits): also match the subject
            cands = [(s, n) for s, n in cands if note_subject(repo, s, n) == subject]
        # repeated rebases leave identical copies behind; those are one candidate, not ambiguity
        distinct = {json.dumps({k: v for k, v in n.items() if k != "subject"}, sort_keys=True): n for _, n in cands}
        if len(distinct) == 1:  # ambiguous -> no guess; computed from logs below if it's yours
            new[sha] = next(iter(distinct.values()))
    mine = [c for c in todo if c[4] == me and c[0] not in new]
    if mine:
        pricing = json.load(open(os.path.join(HERE, "..", "pricing.json")))
        # attribute over all your MR commits, so a call that made several commits splits correctly
        usage = attribute([c for c in commits if c[4] == me], commit_usage(pricing))
        for sha, at, _, subject, email in mine:
            if sha in usage:
                new[sha] = make_note(usage[sha], email, at, subject)
    return new


# ---------- rendering ----------

def human(n):
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


def short_model(m):
    return m.replace("claude-", "")


def render(commits, notes, project_url=None):
    rows, total, have, unpriced, waiting = [], _blank(), 0, set(), collections.Counter()
    for sha, _, _, subject, email in commits:
        n = notes.get(sha)
        subj = subject if len(subject) <= 50 else subject[:47] + "..."
        subj = subj.replace("|", "\\|")
        ref = f"[{sha[:8]}]({project_url}/-/commit/{sha})" if project_url else sha[:8]
        if not n:
            waiting[email] += 1
            rows.append(f"| {ref} | {subj} | - | - | - | - | - | - |")
            continue
        have += 1
        for k in (*TOKEN_FIELDS, "usd"):
            total[k] += n.get(k, 0)
        unpriced |= set(n.get("unpriced", []))
        models = ", ".join(short_model(m) for m in n.get("models", [])) or "-"
        rows.append(f"| {ref} | {subj} | {models} | {human(n['input'])} | {human(n['output'])} "
                    f"| {human(n['cache_w'])} | {human(n['cache_r'])} | ${n['usd']:.2f} |")
    tokens = sum(total[k] for k in TOKEN_FIELDS)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [START, "---",
             f"**🤖 AI usage:** {human(tokens)} tokens · **${total['usd']:.2f}** (API-equivalent) · "
             f"{have} of {len(commits)} commits"]
    if waiting:
        who = ", ".join(f"{e} ({k})" for e, k in sorted(waiting.items()))
        lines.append(f"\nNo usage recorded yet for commits by: {who}. Made without Claude Code, "
                     "or the author hasn't run mr-ai-cost.")
    if unpriced:
        lines.append(f"\n⚠️ No price for: {', '.join(sorted(unpriced))}. Those tokens are counted but not in the USD total.")
    lines += ["", "<details><summary>Per-commit breakdown</summary>", "",
              "| Commit | Subject | Model | Input | Output | Cache W | Cache R | USD |",
              "|---|---|---|---|---|---|---|---|", *rows, "", "</details>",
              f"<!-- updated {now} -->", END]
    return "\n".join(lines)


def splice(description, footer):
    description = description or ""
    if not description.strip():
        return footer
    if START in description and END in description:
        head, rest = description.split(START, 1)
        return head + footer + rest.split(END, 1)[1]
    return description.rstrip() + "\n\n" + footer


# ---------- commands ----------

def process(repo, remote, branch, target, write, rebuild=False, project_url=None):
    """Everything for one MR except talking to GitLab. Returns (footer or None, message)."""
    me = sh(["git", "config", "user.email"], repo, check=False).lower()
    fetch_notes(repo, remote)
    if write:
        merge_notes(repo)
    commits = mr_commits(repo, remote, branch, target)
    notes = {**read_notes(repo, REMOTE_NOTES), **read_notes(repo, NOTES)}
    shas = {c[0] for c in commits}
    new = fill_notes(repo, commits, notes, me, rebuild)
    notes.update(new)
    if not any(c[0] in notes for c in commits) and not any(c[4] == me for c in commits):
        authors = ", ".join(sorted({c[4] for c in commits})) or "nobody"
        return None, (f"mr-ai-cost: none of these commits are yours ({authors}) and none have usage "
                      "recorded, footer not written")
    if write and new:
        for sha, note in new.items():
            if sha in shas:
                write_note(repo, sha, note)
    if write:
        push_notes(repo, remote)
    footer = render(commits, notes, project_url)
    added = f"{len(new)} new note(s)" if write else f"would add {len(new)} note(s)"
    return footer, f"{footer.splitlines()[2]} · {added}"


def run(repo, branch, write, rebuild=False):
    repo = sh(["git", "rev-parse", "--show-toplevel"], repo)
    branch = branch or sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    mrs = open_mrs(repo, branch)
    if not mrs:
        return f"mr-ai-cost: no open MR for '{branch}', cost footer not written"
    remote, msgs = branch_remote(repo, branch), []
    for mr in mrs:
        footer, msg = process(repo, remote, branch, mr["target_branch"], write, rebuild,
                              mr["web_url"].split("/-/merge_requests/")[0])
        if footer is None:
            msgs.append(f"!{mr['iid']}: {msg}")
        elif not write:
            msgs.append(f"--- !{mr['iid']} ({mr['web_url']}) ---\n{footer}")
        else:
            sh(["glab", "api", "-X", "PUT", f"projects/:id/merge_requests/{mr['iid']}",
                "-f", f"description={splice(mr.get('description'), footer)}"], repo)
            msgs.append(f"mr-ai-cost: updated !{mr['iid']} — {msg}")
    return "\n".join(msgs)


def push_repo(command, cwd):
    """If a shell command contains `git push`, return the repo dir it pushed from."""
    for part in re.split(r"&&|\|\||;|\|", command):
        try:
            tok = shlex.split(part)
        except ValueError:
            tok = part.split()
        if "git" not in tok:
            continue
        i, repo = tok.index("git") + 1, cwd
        while i < len(tok) and tok[i].startswith("-"):
            if tok[i] == "-C" and i + 1 < len(tok):
                repo = os.path.join(repo, os.path.expanduser(tok[i + 1])); i += 2
            elif tok[i] == "-c":
                i += 2
            else:
                i += 1
        if i < len(tok) and tok[i] == "push":
            return repo
    return None


def hook():
    # Never break the tool call: report problems as a message, always exit 0.
    try:
        data = json.load(sys.stdin)
        cmd = (data.get("tool_input") or {}).get("command", "")
        cwd = data.get("cwd") or os.getcwd()
        if "push" not in cmd:
            return
        # a `cd X && git push` changes the directory before the push
        m = re.match(r"\s*cd\s+(\S+)\s*&&", cmd)
        if m:
            cwd = os.path.join(cwd, os.path.expanduser(m.group(1)))
        repo = push_repo(cmd, cwd)
        if not repo:
            return
        msg = run(repo, None, write=True)
    except Exception as e:  # noqa: BLE001
        msg = f"mr-ai-cost: footer not updated ({e})"
    print(json.dumps({"systemMessage": msg}))


def doctor(repo):
    """Check what this tool needs, and say how to fix each missing piece."""
    lines, ok = [], True

    def check(passed, good, fix):
        nonlocal ok
        ok &= passed
        lines.append(f"✓ {good}" if passed else f"✗ {fix}")

    top = sh(["git", "rev-parse", "--show-toplevel"], repo, check=False)
    check(bool(top), f"git repo: {top}", "not inside a git repo: run this from your project's folder")
    email = sh(["git", "config", "user.email"], top or repo, check=False)
    check(bool(email), f"git user.email: {email} (your commits must use this address)",
          "git user.email is not set: git config --global user.email you@company.com")
    check(os.path.isdir(PROJECTS), f"Claude Code session logs: {PROJECTS}",
          f"no Claude Code session logs at {PROJECTS} yet: use Claude Code in a repo first")
    has_glab = bool(shutil.which("glab"))
    check(has_glab, "glab installed", "glab not installed: https://gitlab.com/gitlab-org/cli#installation")
    if top and has_glab:
        branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], top, check=False)
        url = sh(["git", "remote", "get-url", branch_remote(top, branch)], top, check=False)
        m = re.match(r"(?:\w+://)?(?:[^@/]+@)?([^/:]+)", url)
        host = m.group(1) if m else ""
        authed = bool(host) and subprocess.run(["glab", "auth", "status", "--hostname", host],
                                               capture_output=True).returncode == 0
        check(authed, f"glab logged in to {host}", f"glab is not logged in to {host or 'the remote host'}: "
              f"glab auth login --hostname {host or '<gitlab host>'}")
    lines.append("All set." if ok else "Fix the ✗ items above, then run doctor again.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["report", "update", "hook", "doctor"])
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--branch")
    ap.add_argument("--rebuild", action="store_true", help="recompute notes on your own commits")
    a = ap.parse_args()
    if a.cmd == "hook":
        return hook()
    if a.cmd == "doctor":
        return print(doctor(a.repo))
    try:
        print(run(a.repo, a.branch, write=a.cmd == "update", rebuild=a.rebuild))
    except RuntimeError as e:
        sys.exit(f"mr-ai-cost: {e}")


if __name__ == "__main__":
    main()
