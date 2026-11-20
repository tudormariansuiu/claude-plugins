import os, subprocess, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'skills', 'mr-ai-cost', 'scripts'))
import mr_ai_cost as m
import tempfile, atexit, shutil; T = tempfile.mkdtemp(prefix='mr-ai-cost-test-'); os.chdir(T); atexit.register(shutil.rmtree, T, True)
def g(repo, *a, env=None):
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(['git', *a], cwd=repo, capture_output=True, text=True, env=e)
    assert r.returncode == 0, (a, r.stderr); return r.stdout.strip()
CALLS = []
m.commit_usage = lambda pricing: CALLS
def bucket(usd): b = m._blank(); b.update(input=10, output=100, cache_w=1000, cache_r=10000, usd=usd, models={'claude-opus-5-5'}); return b
def commit(repo, name, at):
    open(f'{repo}/{name}', 'w').write(name); g(repo, 'add', name)
    d = f'@{at} +0000'
    g(repo, 'commit', '-q', '-m', name, env={'GIT_AUTHOR_DATE': d, 'GIT_COMMITTER_DATE': d})
    return g(repo, 'rev-parse', 'HEAD')
def notes_on_remote():
    return m.read_notes('remote.git', m.NOTES)
ok = lambda c, msg: print(('PASS ' if c else 'FAIL ') + msg) or (c or sys.exit(1))

g(T, 'init', '-q', '--bare', 'remote.git')
g(T, 'clone', '-q', 'remote.git', 'me'); g('me', 'config', 'user.email', 'Tudor@Example.com'); g('me', 'config', 'user.name', 'me')
commit('me', 'base', 1000); g('me', 'branch', '-M', 'main'); g('me', 'push', '-q', '-u', 'origin', 'main')
g('me', 'checkout', '-q', '-b', 'feat')
c1 = commit('me', 'c1', 2000); c2 = commit('me', 'c2', 3000); g('me', 'push', '-q', '-u', 'origin', 'feat')
CALLS[:] = [{'start': 1990, 'end': 2010, 'hashes': set(), 'bucket': bucket(1.0)},
            {'start': 2990, 'end': 3010, 'hashes': set(), 'bucket': bucket(2.0)}]

# 1. my commits get notes, pushed to the remote
f, msg = m.process('me', 'origin', 'feat', 'main', write=True)
r = notes_on_remote()
ok(r.get(c1, {}).get('usd') == 1.0 and r.get(c2, {}).get('usd') == 2.0, f'my notes pushed ({msg})')
ok(r[c1]['author'] == 'tudor@example.com', 'author email stored lower-cased')

# 2. report writes nothing
CALLS.append({'start': 3990, 'end': 4010, 'hashes': set(), 'bucket': bucket(4.0)})
c4 = commit('me', 'c4', 4000); g('me', 'push', '-q')
f, msg = m.process('me', 'origin', 'feat', 'main', write=False, project_url='https://gl.example/g/p')
ok(c4 not in notes_on_remote() and '$7.00' in f, 'report shows c4 in footer but writes no note')
ok(f'| [{c1[:8]}](https://gl.example/g/p/-/commit/{c1}) |' in f and f'| [{c4[:8]}](https://gl.example/g/p/-/commit/{c4}) |' in f,
   'footer links each 8-char sha to its commit page')

# 3. teammate adds a commit; their run adds their note, keeps mine, and leaves c4 (mine) alone
g(T, 'clone', '-q', 'remote.git', 'mate'); g('mate', 'config', 'user.email', 'mate@example.com'); g('mate', 'config', 'user.name', 'mate')
g('mate', 'checkout', '-q', 'feat')
c5 = commit('mate', 'c5', 5000); g('mate', 'push', '-q')
CALLS_ME = list(CALLS); CALLS[:] = [{'start': 4990, 'end': 5010, 'hashes': set(), 'bucket': bucket(8.0)}]
f, msg = m.process('mate', 'origin', 'feat', 'main', write=True)
r = notes_on_remote()
ok(r[c5]['usd'] == 8.0 and r[c1]['usd'] == 1.0 and c4 not in r, f'teammate adds only their own note ({msg})')
ok('3 of 4 commits' in f and 'tudor@example.com (1)' in f, 'footer lists my commit c4 as waiting')

# 4. push collision: my local notes ref is stale when I push, retry merges theirs
CALLS[:] = CALLS_ME
g('me', 'pull', '-q', '--rebase')   # get c5 into my branch (no rewrite of my commits)
m.write_note('me', c4, m.make_note(bucket(4.0), 'tudor@example.com', 4000, 'c4'))   # without fetching first
m.push_notes('me', 'origin')
r = notes_on_remote()
ok(r.get(c4, {}).get('usd') == 4.0 and r.get(c5, {}).get('usd') == 8.0, 'stale push retried and merged: both notes on remote')

# 5. never overwrite: my logs now say something else, notes stay
CALLS[0]['bucket'] = bucket(99.0)
f, msg = m.process('me', 'origin', 'feat', 'main', write=True)
ok(notes_on_remote()[c1]['usd'] == 1.0, 'existing note not overwritten')
f, msg = m.process('me', 'origin', 'feat', 'main', write=True, rebuild=True)
r = notes_on_remote()
ok(r[c1]['usd'] == 99.0 and r[c5]['usd'] == 8.0, '--rebuild recomputes only my notes')

# 6. rebase: new shas, no session data at all -> notes come from pre-rebase twins
CALLS[:] = []
commit_main = None
g('me', 'checkout', '-q', 'main'); commit('me', 'main2', 6000); g('me', 'push', '-q')
g('me', 'checkout', '-q', 'feat'); g('me', 'rebase', '-q', 'main'); g('me', 'push', '-q', '-f')
new = g('me', 'log', '--format=%H', 'main..feat').split()
ok(c1 not in new, 'rebase produced new hashes')
f, msg = m.process('me', 'origin', 'feat', 'main', write=True)
ok('4 of 4 commits' in f and '$113.00' in f, f'rebased commits keep their notes via twins ({msg})')

# 6b. two commits in the same second (parallel calls), then rebase: each keeps its own note
CALLS[:] = [{'start': 7990, 'end': 8001, 'hashes': set(), 'bucket': bucket(16.0)},
            {'start': 7995, 'end': 8001, 'hashes': set(), 'bucket': bucket(0.5)}]
s1 = commit('me', 's1', 8000); s2 = commit('me', 's2', 8000); g('me', 'push', '-q', '-f')
f, msg = m.process('me', 'origin', 'feat', 'main', write=True)
r = notes_on_remote()
ok({r[s1]['usd'], r[s2]['usd']} == {16.0, 0.5}, f'same-second commits get one call each ({msg})')
CALLS[:] = []
g('me', 'rebase', '-q', '--force-rebase', 'main'); g('me', 'push', '-q', '-f')
f, msg = m.process('me', 'origin', 'feat', 'main', write=True)
ok('$129.50' in f, f'same-second commits keep their own notes after rebase ({msg})')

# 7. block: MR of only someone else's commits, no notes
g('mate', 'fetch', '-q'); g('mate', 'checkout', '-q', '-b', 'other', 'origin/main')
commit('mate', 'x1', 7000); g('mate', 'push', '-q', '-u', 'origin', 'other')
g('me', 'fetch', '-q')
f, msg = m.process('me', 'origin', 'other', 'main', write=True)
ok(f is None and 'none of these commits are yours' in msg, f'blocked: {msg}')
print(f'\nfinal footer for feat:\n' + m.process('me', 'origin', 'feat', 'main', write=False)[0])
