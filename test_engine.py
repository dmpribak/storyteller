"""Headless simulation of the whole game with the current engine. Run: python test_engine.py"""
import json, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E


def fill(s, t, spec):
    v = {}
    for f in spec["fields"]:
        k = f["key"]
        if k == "kind":
            v[k] = random.choice("cpt")
        elif k == "name":
            v[k] = f"Card{t['id']}"
        elif f["validator"] == "question":
            v[k] = f"What happened around item {t['id']}?"
        elif k == "state":
            v[k] = random.choice(["", "changed", "hurt"])
        else:
            v[k] = f"text-{t['id']}-{k}"
    return v


def step(s, players, stats, follow=True):
    """One random player takes one random open task. Returns False if nothing could be done."""
    vis = {p: E.visible_tasks(s, p) for p in players}
    board = [(t, [p for p in players if t in vis[p]]) for t in E.open_tasks(s)]
    board = [(t, ps) for t, ps in board if ps]
    if not board:
        return False
    t, ps = random.choice(board)
    p = random.choice(ps)
    claimed, err = E.claim_task(s, p, t["id"])
    assert claimed and not err, err
    spec = E.describe(s, t)
    E.preview(s, t)
    v = fill(s, t, spec)
    assert E.validate(spec, v) is None, E.validate(spec, v)
    stats["types"][t["type"]] = stats["types"].get(t["type"], 0) + 1
    for ev in E.perform(s, p, t, v):
        stats["events"][ev["type"]] = stats["events"].get(ev["type"], 0) + 1
        if ev["type"] == "scene" and follow:
            sid = ev["scene"]
            if random.random() < 0.7:
                E.append_to_card(s, p, sid, random.choice(E.scene_cards(s, sid)), "an added fact", random.choice([None, "new state"]))
            if random.random() < 0.25:
                E.promote_card(s, p, sid, "Newbie", "invented in a scene", random.choice("cpt"))
            if random.random() < 0.6:
                if E.get_line(s, ev["line"])["status"] == "closed":
                    try:
                        E.set_hook(s, p, sid, "too late")
                        raise AssertionError("an ended storyline takes no hook")
                    except ValueError:
                        pass
                else:
                    E.set_hook(s, p, sid, "A hook for the next writer?")
    return True


def play(nplayers, seed, steps=700, **over):
    random.seed(seed)
    s = E.new_game(channel_id=1, **over)
    players = [f"u{i}" for i in range(nplayers)]
    for p in players:
        E.join(s, p, p)
    stats = {"types": {}, "events": {}}
    for _ in range(steps):
        if not step(s, players, stats):
            break
        for t in E.open_tasks(s):
            assert E.eligible(s, t), f"stuck task {t}"
        scene_tasks = [t["ref"]["line"] for t in E.open_tasks(s) if t["type"] == "SCENE"]
        assert sorted(scene_tasks) == sorted(l["id"] for l in E.open_lines(s)), "exactly one open scene task per running storyline"
        assert len(E.open_lines(s)) <= max(s["settings"]["max_storylines"], 1)
        for l in E.lines_of(s):
            if l["length"]:
                assert len(l["scenes"]) <= l["length"]
                assert (l["status"] == "closed") == (len(l["scenes"]) == l["length"]), "ends exactly at its length"
            if l["status"] == "closed":
                assert not l["queued"], "ended storylines hold no queued developments"
        json.dumps(s)
    lines = E.lines_of(s)
    kinds = {}
    for sc in s["scenes"].values():
        k = (sc["injection"] or {}).get("kind")
        if k:
            kinds[k] = kinds.get(k, 0) + 1
    print(f"{nplayers} players {over}\n   storylines {len(lines)} ({len(E.closed_lines(s))} ended, {len(E.open_lines(s))} running), "
          f"{len(s['scenes'])} scenes, {sum(len(l['recaps']) for l in lines)} recaps, {len(s['cards'])} cards; "
          f"developments waiting {len(E.pending_openings(s))}; wildcards/developments in scenes {kinds}")
    return s


def check_fresh_grace():
    """'Fresh eyes' is a preference: fresh players get first claim, and after fresh_grace_hours
    anyone (except hard exclusions) can take the task, so absent fresh players can't stall the story."""
    def to_weave(players, **over):
        random.seed(1)
        s = E.new_game(hops=1, **over)
        for p in players:
            E.join(s, p, p)
        for who, ty in (("a", "ROOT"), ("b", "ASK"), ("c", "ANSWER")):
            t = next(x for x in E.open_tasks(s) if x["type"] == ty)
            E.claim_task(s, who, t["id"])
            E.perform(s, who, t, fill(s, t, E.describe(s, t)))
        return s, next(x for x in E.open_tasks(s) if x["type"] == "WEAVE")

    # four players: a, b, c wrote the chain, only d is fresh
    s, weave = to_weave(["a", "b", "c", "d"])
    assert E.eligible(s, weave) == {"d"}, E.eligible(s, weave)
    assert E.claim_task(s, "a", weave["id"])[0] is None, "a contributed, so not yet"
    weave["at"] -= 25 * 3600     # d has been away for a day
    assert E.eligible(s, weave) == {"a", "b", "c", "d"}, "after the grace period anyone may weave it"
    t, err = E.claim_task(s, "a", weave["id"])
    assert t and not err, err

    # everyone contributed: nobody is fresh, so it is open immediately
    s, weave = to_weave(["a", "b", "c"])
    assert E.eligible(s, weave) == {"a", "b", "c"}

    # a longer grace period is respected
    s, weave = to_weave(["a", "b", "c", "d"], fresh_grace_hours=72)
    weave["at"] -= 25 * 3600
    assert E.eligible(s, weave) == {"d"}, "still inside a 72h grace period"
    weave["at"] -= 48 * 3600
    assert "a" in E.eligible(s, weave)

    # hard exclusions survive the grace period: you never answer your own linking question
    random.seed(2)
    s = E.new_game(hops=1)
    for p in "abcd":
        E.join(s, p, p)
    link = {"id": 900, "a": 1, "b": 2, "question": "Q?", "answer": None, "q_author": "a",
            "a_author": None, "used": False}
    s["links"]["900"] = link
    t = E.add_task(s, "CROSS_ANSWER", link=900)
    t["at"] -= 100 * 3600
    assert "a" not in E.eligible(s, t) and {"b", "c", "d"} <= E.eligible(s, t)

    # recaps: if the only fresh writer is away, the recap still gets written eventually
    random.seed(3)
    s = E.new_game(chapter_length=2)
    players = ["a", "b", "c", "d"]
    for p in players:
        E.join(s, p, p)
    line = E._new_line(s, "Test line", [], [])
    for n, who in enumerate(["a", "b"], 1):   # a and b wrote chapter 1; c and d are fresh
        sid = E.nid(s)
        s["scenes"][str(sid)] = {"id": sid, "line": line["id"], "text": f"scene {n}", "author": who, "number": n,
                                 "chapter": 1, "spotlight": [], "injection": None, "touched": []}
        line["scenes"].append(sid)
    E.ensure_recaps(s)
    recap = next(x for x in E.open_tasks(s) if x["type"] == "RECAP")
    assert E.eligible(s, recap) == {"c", "d"}
    recap["at"] -= 25 * 3600
    assert E.eligible(s, recap) == set(players), "if c and d never show up, a and b may recap"
    print("fresh-eyes grace period: ok")


def check_world():
    """Several storylines share one world; each one ends and leaves marks on what follows."""
    # max_storylines=1: one storyline at a time, each ending before the next begins
    one = play(5, 11, max_storylines=1, storyline_length=4, chapter_length=4, steps=900)
    assert len(E.lines_of(one)) >= 3 and all(len(E.open_lines(one)) <= 1 for _ in [0])

    # endless mode still works (storyline_length=0)
    endless = play(4, 12, storyline_length=0, chapter_length=4, steps=700)
    assert not E.closed_lines(endless)

    many = play(6, 4, storyline_length=6, chapter_length=3, goal=3, roots=4, handoffs=2, steps=2500)
    lines = E.lines_of(many)
    closed = E.closed_lines(many)
    assert len(closed) >= 3, "storylines end and make room for new ones"
    assert len(lines) > many["settings"]["max_storylines"], "new storylines replaced the ones that ended"
    assert len(E.open_lines(many)) <= many["settings"]["max_storylines"]
    for l in closed:
        assert len(l["scenes"]) == l["length"] == 6
        assert not any(t["type"] == "SCENE" and t["ref"]["line"] == l["id"] for t in E.open_tasks(many))
    # every running storyline got (or will get) its guaranteed crossover; every ended one had one if there was anything to borrow
    crossed = [l for l in closed if "last_cross" in l]
    assert len(crossed) >= len(closed) * 0.6, (len(crossed), len(closed))
    cross = [sc for sc in many["scenes"].values() if (sc["injection"] or {}).get("kind") == "crossover"]
    assert cross
    sc = cross[0]; inj = sc["injection"]
    assert inj["card"] in E.get_line(many, sc["line"])["cards"] and inj["card"] in E.get_line(many, inj["from"])["cards"]
    print("   crossover scenes:", len(cross), "| storylines with a crossover:", len(crossed), "of", len(closed), "ended")
    beats = [sc for sc in many["scenes"].values() if (sc["injection"] or {}).get("kind") == "beat"]
    print("   developments woven in:", len(beats))

    # the ending: a recap with an aftermath, which marks the founding cards
    ended = [l for l in closed if E.ending_of(l)]
    assert ended, "some endings have been written"
    l = ended[0]
    e = E.ending_of(l)
    assert e["aftermath"] and l["aftermath"] == e["aftermath"]
    for cid in E._founders(many, l):
        assert any(("Aftermath of" in x["text"] and e["aftermath"] in x["text"]) for x in E.get_card(many, cid)["log"]), "aftermath is in the founding cards' logs"
    # later storylines hear about it
    later = [m for m in E.open_lines(many) + [x for x in closed if x["closed_at"] and x["closed_at"] > l["closed_at"]]]
    probe = E.open_lines(many)[0]
    task = next(t for t in E.open_tasks(many) if t["type"] == "SCENE" and t["ref"]["line"] == probe["id"])
    text = "\n".join(x if isinstance(x, str) else x[1] for x in E.describe(many, task)["lines"])
    assert "Echoes of storylines that already ended" in text and "Aftermath:" in text
    assert "Meanwhile, elsewhere in the world:" in text and "Scene " in text
    # the final scene knows it is the finale
    fake = {"id": 0, "type": "SCENE", "ref": {"line": probe["id"], "injection": None}, "open": True, "at": 0}
    keep = probe["scenes"][:]
    probe["scenes"] = (keep + [keep[-1] if keep else 1] * 6)[: probe["length"] - 1] if keep else probe["scenes"]
    if keep:
        ftxt = "\n".join(x if isinstance(x, str) else x[1] for x in E.describe(many, fake)["lines"])
        assert "THIS IS THE FINAL SCENE" in ftxt
    probe["scenes"] = keep

    # shared cards: a change made in one storyline shows up in the prompts of the others
    runs = E.open_lines(many)
    shared = next((cid for a in runs for cid in a["cards"] if sum(1 for m in runs if cid in m["cards"]) > 1), None)
    if shared is not None:
        in_lines = [m for m in runs if shared in m["cards"]]
        E.add_append(many, "someone", shared, "UNIQUE-SHARED-FACT-123", "changed everywhere")
        for m in in_lines:
            task = next(t for t in E.open_tasks(many) if t["type"] == "SCENE" and t["ref"]["line"] == m["id"])
            task["ref"]["spotlight"] = [shared] + [c for c in E.spotlight(many, m) if c != shared][:2]
            text = "\n".join(x if isinstance(x, str) else x[1] for x in E.describe(many, task)["lines"])
            assert "UNIQUE-SHARED-FACT-123" in text
    print("   endings, aftermath, echoes, finale prompt, shared cards: ok")


def check_ending_flow():
    """Walk one storyline to its end by hand and check the freed slot is reused."""
    random.seed(5)
    s = E.new_game(max_storylines=1, storyline_length=3, chapter_length=2, goal=1, roots=2, handoffs=1)
    players = [f"u{i}" for i in range(4)]
    for p in players:
        E.join(s, p, p)
    stats = {"types": {}, "events": {}}
    for _ in range(600):
        if not step(s, players, stats):
            break
        if len(E.closed_lines(s)) >= 2:
            break
    assert stats["events"].get("storyline_end", 0) >= 2, stats["events"]
    assert stats["events"].get("storyline_start", 0) >= 2, "a freed slot starts a new storyline"
    assert len(E.open_lines(s)) <= 1
    print("   ending flow: ok", {k: v for k, v in stats["events"].items() if "storyline" in k})


def main():
    s = play(3, 1, chapter_length=4, beat_gap=2)
    s = play(5, 2, chapter_length=5)
    s = play(2, 3, hops=1, roots=2, handoffs=1, goal=1, chapter_length=4)
    check_world()
    check_ending_flow()

    # claims
    s2 = E.new_game()
    for p in ("a", "b", "c"):
        E.join(s2, p, p)
    t = E.open_tasks(s2)[0]
    print("claim:", E.claim_task(s2, "a", t["id"])[1], "|", E.claim_task(s2, "b", t["id"])[1])
    t["claim"]["at"] -= 25 * 3600
    print("expired:", E.release_expired(s2), "| reclaim:", E.claim_task(s2, "b", t["id"])[1])
    check_fresh_grace()
    print("ALL OK")


if __name__ == "__main__":
    main()
