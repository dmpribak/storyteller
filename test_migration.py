"""Checks that saves from older versions upgrade cleanly and keep working.
  v1: separate story threads        -> each thread becomes its own storyline
  v2: one big story                 -> the story becomes the first storyline
  v3: endless parallel storylines   -> each finishes its current stretch, then ends
  (resolved v1 threads become ended storylines)
Run: python test_migration.py"""
import copy, glob, json, os, random, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine as E
from test_engine import step


def common_checks(path, before, s):
    name = os.path.basename(path)
    assert s["version"] == E.SAVE_VERSION and "threads" not in s and "story" not in s
    assert all("at" in t for t in s["tasks"]), "tasks from old saves get a creation time"
    stamp = [t["at"] for t in s["tasks"]]
    snapshot = json.dumps(s, sort_keys=True)
    assert E.migrate(s) is False, "migration must be idempotent"
    assert json.dumps(s, sort_keys=True) == snapshot and [t["at"] for t in s["tasks"]] == stamp, \
        "re-running must change nothing"
    json.dumps(s)
    # nothing players wrote was lost or changed
    assert set(s["cards"]) == set(before["cards"])
    for cid, c in before["cards"].items():
        for k in ("text", "name", "state", "log", "kind", "author"):
            assert s["cards"][cid][k] == c[k], (name, cid, k)
    assert set(s["chains"]) == set(before["chains"])
    assert set(s["links"]) >= set(before["links"])
    assert set(s["scenes"]) == set(before["scenes"])
    for sid, sc in before["scenes"].items():
        assert s["scenes"][sid]["text"] == sc["text"] and s["scenes"][sid]["author"] == sc["author"]
    assert [o["hook"] for o in s["openings"]] == [o["hook"] for o in before["openings"]]
    # the board is consistent: one open scene task per storyline, recaps owed are queued
    lines = E.lines_of(s)
    assert sorted(t["ref"]["line"] for t in E.open_tasks(s) if t["type"] == "SCENE") == sorted(l["id"] for l in E.open_lines(s))
    for l in lines:
        assert l["status"] in ("open", "closed") and l["length"] is not None
        if l["status"] == "open":
            assert l["length"] > len(l["scenes"]) or not l["length"], "a running storyline still has room"
        cl = s["settings"]["chapter_length"]
        n = len(l["scenes"])
        due = -(-n // cl) if l["status"] == "closed" else n // cl
        owed = {t["ref"]["chapter"] for t in E.open_tasks(s) if t["type"] == "RECAP" and t["ref"]["line"] == l["id"]}
        assert owed | {r["chapter"] for r in l["recaps"]} == set(range(1, due + 1)), (name, l["title"])
        nums = [E.get_scene(s, sid)["number"] for sid in l["scenes"]]
        assert nums == list(range(1, len(nums) + 1)), "scenes are numbered 1..n within each storyline"
    for o in s["openings"]:
        assert o.get("used") or o.get("line") is not None, "every finished hook has a home"
    return lines


def play_on(path, s, before_scenes):
    random.seed(7)
    players = list(s["players"])
    stats = {"types": {}, "events": {}}
    for _ in range(500):
        if not step(s, players, stats):
            break
        for t in E.open_tasks(s):
            assert E.eligible(s, t), f"stuck task {t}"
        json.dumps(s)
    lines = E.lines_of(s)
    assert sorted(t["ref"]["line"] for t in E.open_tasks(s) if t["type"] == "SCENE") == sorted(l["id"] for l in E.open_lines(s))
    assert len(E.open_lines(s)) <= s["settings"]["max_storylines"]
    new_scenes = len(s["scenes"]) - before_scenes
    assert lines and new_scenes > 0, "the upgraded game should play on"
    print(f"   played on: {new_scenes} new scene(s) across {len(lines)} storyline(s), "
          f"{sum(len(l['recaps']) for l in lines)} recap(s), {stats['events'].get('beat', 0)} development(s) woven in, "
          f"{stats['events'].get('storyline_end', 0)} storyline(s) ended")


def check_v1(path):
    s = json.load(open(path))
    assert s["version"] == 1
    before = copy.deepcopy(s)
    old_threads = before.get("threads", {})
    assert E.needs_migration(s) and E.migrate(s) is True
    assert s["legacy_threads"] == old_threads, "old thread records must be kept"
    lines = common_checks(path, before, s)
    if old_threads:
        by_thread = sorted(old_threads.values(), key=lambda t: t["id"])
        # a thread becomes a storyline: same scenes in the same order, same cast, same Discord thread
        titles = []
        for th in by_thread:
            line = next(l for l in lines if l["started_with"] == th["opening"])
            assert line["scenes"] == th["scenes"] and line["cards"] == th["cards"]
            assert line["discord_thread"] == th["discord_thread"], "keeps posting in the existing Discord thread"
            if th["status"] == "open":
                assert line["hook"] == th["hook"] and line["status"] == "open", "a pending hook survives"
                assert line["length"] == s["settings"]["storyline_length"] == before["settings"]["thread_length"] or line["length"] > len(line["scenes"])
            else:
                assert line["status"] == "closed", "a resolved thread is an ended storyline"
            titles.append(line["title"])
        assert len(lines) >= len(old_threads), "one storyline per old thread (plus any started from waiting hooks)"
    else:
        assert not lines, "no story yet means none is invented"
    print(f"{os.path.basename(path)}: OK - {len(old_threads)} thread(s) -> {len(lines)} storyline(s); "
          f"{len(before['scenes'])} scene(s) kept")
    play_on(path, s, len(before["scenes"]))


def check_v2(path):
    s = json.load(open(path))
    assert s["version"] == 2
    before = copy.deepcopy(s)
    story = before.get("story")
    unused = [o for o in before["openings"] if not o.get("used")]
    assert E.needs_migration(s) and E.migrate(s) is True
    lines = common_checks(path, before, s)
    if story:
        first = next(l for l in lines if l["scenes"] == story["scenes"])
        assert first["cards"][:len(story["cards"])] == story["cards"], "the cast carries over"
        assert first["recaps"] == story["recaps"], "recaps carry over"
        assert first["hook"] == story["hook"] and first["discord_thread"] == story["discord_thread"]
        assert first["last_beat_scene"] == story["last_beat_scene"]
        # hooks that were waiting for the old single story now start new storylines (while there is room)
        room = max(0, s["settings"]["max_storylines"] - 1)
        started = min(len(unused), room)
        assert len(lines) == 1 + started, (len(lines), started)
        assert sum(1 for l in lines for _ in l["queued"]) == len(unused) - started
    else:
        assert not lines
    print(f"{os.path.basename(path)}: OK - {len(before['scenes'])} scene(s) kept, {len(unused)} waiting hook(s) "
          f"-> {len(lines)} storyline(s)")
    play_on(path, s, len(before["scenes"]))


def check_v3(path):
    s = json.load(open(path))
    assert s["version"] == 3
    before = copy.deepcopy(s)
    assert E.needs_migration(s) and E.migrate(s) is True
    lines = common_checks(path, before, s)
    L = s["settings"]["storyline_length"]
    for l in lines:
        b = before["storylines"][str(l["id"])]
        assert l["scenes"] == b["scenes"] and l["cards"] == b["cards"] and l["recaps"] == b["recaps"]
        assert l["status"] == "open" and l["length"] % L == 0 and l["length"] > len(l["scenes"]), \
            "a running storyline finishes its current stretch of scenes and then ends"
        assert l["length"] - len(l["scenes"]) <= L
    print(f"{os.path.basename(path)}: OK - {len(lines)} storyline(s) now have a lifespan")
    play_on(path, s, len(before["scenes"]))
    assert E.closed_lines(s) or len(before["scenes"]) > 200, "upgraded storylines reach their ending"


def check_cross_version_restart(path):
    """A save upgraded and saved, then loaded again by a later run, is left alone."""
    s = json.load(open(path))
    E.migrate(s)
    again = json.loads(json.dumps(s))
    assert E.migrate(again) is False and again == s


paths_v1 = sorted(glob.glob(os.path.join(HERE, "fixtures", "save_v1_*.json")))
paths_v2 = sorted(glob.glob(os.path.join(HERE, "fixtures", "save_v2_*.json")))
paths_v3 = sorted(glob.glob(os.path.join(HERE, "fixtures", "save_v3_*.json")))
assert paths_v1 and paths_v2 and paths_v3, "fixtures are missing"
for p in paths_v1:
    check_v1(p)
for p in paths_v2:
    check_v2(p)
for p in paths_v3:
    check_v3(p)
for p in paths_v1 + paths_v2 + paths_v3:
    check_cross_version_restart(p)
print("ALL MIGRATIONS OK")
