"""
engine.py - game logic for the collaborative storytelling bot. No Discord imports.

Everything is plain dicts/lists so a whole game serializes to JSON. Player ids are
strings (Discord user ids). Functions that change the game return a list of
"events" (small dicts) that the Discord layer turns into posts.

SEEDING (runs forever in the background; blind relay -> cards -> developments)
  ROOT          answer a general present-moment question -> starts a chain
  ASK / ANSWER  blind relay: asker sees only the last answer, answerer only the question
  WEAVE         someone reads the finished chain and turns it into a named card
  CROSS_ASK     two cards get a linking question...
  CROSS_ANSWER  ...and someone answers it
  OPEN          someone writes the hook for that pairing -> a "development"
  Hand-off      a finished chain's last answer also seeds a brand-new chain

THE WORLD AND ITS STORYLINES (several storylines at once, one shared world, each one ends)
  The cards are shared by the whole world. Storylines are parallel threads of story that all
  take place in it. The first finished hooks each start a storyline (up to `max_storylines` open
  at once). A storyline lasts `storyline_length` scenes (default 8) and then ENDS; its slot is
  freed and the next hook starts a fresh storyline. Hooks that arrive while every slot is taken
  are woven into a running storyline as "developments".
  SCENE         write the next scene of ONE open storyline (never two in a row by one player in
                the same storyline). Each open storyline has exactly one open scene task. The
                prompt puts a few cards in the spotlight, says what is happening in the other
                open storylines ("meanwhile, elsewhere") and echoes the endings of earlier
                storylines. The last scene is told it is the finale.
                Afterwards the author may add to cards, create a new card, and leave a hook
                (not on the finale).
  RECAP         chapter recaps (`chapter_length`) and, when a storyline ends, its ENDING: a title,
                a recap, and the aftermath (what is different in the world now). The aftermath is
                written into the log of the storyline's two founding cards, so every later
                storyline that uses them sees it, and the ending is echoed in later scene prompts.
  APPEND        standalone slot: add a line to a thin card (cards are shared, so the change shows
                up in every storyline the card is in)
  Intersections every storyline gets a guaranteed crossover around its midpoint (a card from
                another storyline, preferring ones still running), plus random wildcards:
                new developments, "arrival" cards from the background seeding, extra
                "crossover" cards (also from ended storylines), and word constraints.

NOTIFICATIONS  due_notifications() says who should be pinged about new tasks (opt-out per player,
               with a cooldown so a burst of tasks becomes one ping).

Saves from older versions (v1: separate story threads, v2: one big story, v3: endless parallel
storylines) are upgraded
automatically by migrate().
"""
import random
import time

SAVE_VERSION = 4

ROOT_QUESTIONS = [
    ("How are you feeling right now?", "character"),
    ("How does your body feel right now?", "character"),
    ("Who's nearby, or on your mind, right now?", "character"),
    ("What are you doing right now?", "character"),
    ("Where are you right now?", "place"),
    ("What's within arm's reach right now?", "place"),
    ("What can you hear right now?", "place"),
]

YES_NO_STARTS = {"is", "are", "was", "were", "am", "do", "does", "did", "can", "could",
                 "will", "would", "should", "have", "has", "had"}
KIND_MAP = {"c": "character", "p": "place", "t": "thing"}
KIND_LABEL = {"character": "character", "place": "place", "thing": "thing/concept/situation"}

LABELS = {
    "ROOT": "Answer a seed question",
    "ASK": "Ask a follow-up question",
    "ANSWER": "Answer a question",
    "WEAVE": "Weave a finished chain into a card",
    "CROSS_ASK": "Link two cards with a question",
    "CROSS_ANSWER": "Answer a linking question",
    "OPEN": "Write a development hook",
    "SCENE": "Write the next scene",
    "RECAP": "Recap a finished chapter or ending",
    "APPEND": "Add to a card",
}

DEFAULTS = {
    "hops": 2,                    # question/answer pairs per chain
    "roots": 3,                   # seed questions at the start
    "handoffs": 1,                # chains seeded from another chain's last answer (at the start)
    "goal": 2,                    # opening hooks before the start-up seeding counts as "done"
    "handoff_context": True,      # people continuing a hand-off chain see where it came from
    "max_storylines": 3,          # storylines open at once
    "storyline_length": 8,        # scenes before a storyline ends (0 = storylines never end)
    "chapter_length": 8,          # scenes per chapter (per storyline)
    "beat_gap": 3,                # minimum scenes in a storyline between new developments
    "spotlight_size": 3,          # cards put in the spotlight for each scene
    "max_pipeline": 3,            # developments waiting (paired cards + queued hooks) before pairing pauses
    "claim_hours": 24,            # how long a claimed task is held for you
    "fresh_grace_hours": 24,      # "fresh eyes" is a preference: after this long, anyone may take the task
    "append_slots": 2,            # standalone "add to a card" slots kept on the board
    "ongoing_handoff_prob": 0.4,  # after the start, chance a finished chain hands off
    "notify_cooldown_min": 30,    # at most one ping per player this often (new tasks are batched)
    "inactive_days": 30,          # stop pinging players who haven't opened the board for this long
}


# ---------------------------------------------------------------- helpers --

def clip(text, n=56):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def nid(s):
    i = s["next_id"]
    s["next_id"] += 1
    return i


def get_chain(s, i):
    return s["chains"][str(i)]


def get_card(s, i):
    return s["cards"][str(i)]


def get_link(s, i):
    return s["links"][str(i)]


def get_scene(s, i):
    return s["scenes"][str(i)]


def get_opening(s, i):
    return next(o for o in s["openings"] if o["id"] == i)


def piece(role, text, author, **extra):
    d = {"role": role, "text": text, "author": author}
    d.update(extra)
    return d


def fmt_card(c):
    return f"{c['name']} ({KIND_LABEL.get(c['kind'], c['kind'])}): {c['text']}"


def open_tasks(s):
    return [t for t in s["tasks"] if t["open"]]


def find_task(s, task_id):
    return next((t for t in s["tasks"] if t["id"] == task_id), None)


def find_card(s, name):
    name = name.strip().lower()
    exact = [c for c in s["cards"].values() if c["name"].lower() == name]
    if exact:
        return exact[0]
    part = [c for c in s["cards"].values() if name in c["name"].lower()]
    return part[0] if len(part) == 1 else None


def name_of(s, uid):
    return s["names"].get(uid, "someone")


def join(s, uid, display_name=None):
    """Add a player. Returns True if they are new."""
    uid = str(uid)
    is_new = uid not in s["players"]
    if is_new:
        s["players"].append(uid)
    if display_name:
        s["names"][uid] = display_name
    prefs(s, uid)
    return is_new


def chapter_of(s, n):
    """Chapter that scene number n (1-based) belongs to."""
    return (n - 1) // s["settings"]["chapter_length"] + 1


# ------------------------------------------------------------- new game --

def new_game(channel_id=None, **overrides):
    st = dict(DEFAULTS)
    st.update({k: v for k, v in overrides.items() if k in DEFAULTS and v is not None})
    s = {
        "version": SAVE_VERSION,
        "players": [], "names": {},
        "settings": st,
        "next_id": 1,
        "chains": {}, "cards": {}, "links": {},
        "openings": [], "tasks": [], "scenes": {},
        "storylines": {},
        "words": [],
        "handoffs_done": 0, "seeding_done": False,
        "channel_id": channel_id, "board_message_id": None,
    }
    pool = ROOT_QUESTIONS[:]
    random.shuffle(pool)
    chosen = []
    for kind in ("character", "place"):  # make sure both kinds are represented
        for q in pool:
            if q[1] == kind and q not in chosen:
                chosen.append(q)
                break
    for q in pool:
        if len(chosen) >= st["roots"]:
            break
        if q not in chosen:
            chosen.append(q)
    for question, kind in chosen[: st["roots"]]:
        add_task(s, "ROOT", question=question, kind=kind)
    return s


def add_task(s, ttype, **ref):
    t = {"id": nid(s), "type": ttype, "ref": ref, "open": True, "claim": None, "at": time.time()}
    s["tasks"].append(t)
    return t


def new_chain(s, kind, root_question, first_piece, parent=None):
    cid = nid(s)
    s["chains"][str(cid)] = {"id": cid, "kind": kind, "root_question": root_question,
                             "pieces": [first_piece], "done": False, "card": None,
                             "parent": parent}
    return s["chains"][str(cid)]


def make_card(s, name, text, kind, author, chain=None, in_play=False):
    cid = nid(s)
    s["cards"][str(cid)] = {"id": cid, "name": name, "text": text, "kind": kind,
                            "chain": chain, "author": author, "linked": False,
                            "state": "", "log": [], "in_play": in_play}
    return s["cards"][str(cid)]


# --------------------------------------------------- saves from older versions --

def _fill_defaults(s):
    for k, v in DEFAULTS.items():
        s["settings"].setdefault(k, v)
    now = time.time()
    for t in s.get("tasks", []):
        t.setdefault("at", now)   # saves from before task times existed: the clock starts now


def needs_migration(s):
    return s.get("version", 1) < SAVE_VERSION


def _line_title(s, card_ids, fallback="A storyline"):
    names = [get_card(s, c)["name"] for c in card_ids[:2] if str(c) in s["cards"]]
    return clip(" & ".join(names), 80) if names else fallback


def _new_line(s, title, cards, scenes, **extra):
    lid = nid(s)
    line = {"id": lid, "title": title, "cards": list(cards), "scenes": list(scenes), "hook": None,
            "hook_author": None, "stale": 0, "used_words": [], "recaps": [],
            "last_beat_scene": len(scenes), "discord_thread": None, "started_with": None, "queued": [],
            "status": "open", "length": None, "aftermath": None, "closed_at": None}
    line.update(extra)
    s["storylines"][str(lid)] = line
    return line


def _from_threads(s):
    """v1 -> v3. Every old story thread becomes a storyline of its own (they all keep running)."""
    st = s["settings"]
    for o in s.get("openings", []):
        o["used"] = True      # every v1 opening already started a thread
    used_links = {o["link"] for o in s.get("openings", [])}
    for l in s.get("links", {}).values():
        l["used"] = l["id"] in used_links   # links still awaiting a hook stay in the pipeline
    legacy = s.pop("threads", {}) or {}
    s.pop("story", None)
    line_of = {}
    for th in sorted(legacy.values(), key=lambda t: t["id"]):
        ids = [sid for sid in th.get("scenes", []) if str(sid) in s["scenes"]]
        live = th.get("status") == "open"
        line = _new_line(s, _line_title(s, th["cards"]), th["cards"], ids,
                         hook=th.get("hook") if live else None,
                         hook_author=th.get("hook_author") if live else None,
                         status="open" if live else "closed",
                         length=None if live else len(ids),
                         closed_at=None if live else (s["scenes"][str(ids[-1])].get("at") if ids else time.time()),
                         stale=th.get("stale", 0), used_words=list(th.get("used_words", [])),
                         discord_thread=th.get("discord_thread"), started_with=th.get("opening"))
        line_of[th["id"]] = line["id"]
        for n, sid in enumerate(ids, 1):
            sc = s["scenes"][str(sid)]
            sc["legacy_thread"] = sc.pop("thread", th["id"])
            sc["legacy_number"] = sc.get("number")
            sc["line"] = line["id"]
            sc["number"] = n
            sc["chapter"] = chapter_of(s, n)
            sc.setdefault("touched", [])
            sc["spotlight"] = list(sc.get("cards", []))[: st["spotlight_size"]]
    for o in s.get("openings", []):
        for th in legacy.values():
            if th.get("opening") == o["id"]:
                o["line"] = line_of[th["id"]]
    s["legacy_threads"] = legacy
    kept = set()
    for t in [x for x in open_tasks(s) if x["type"] == "SCENE"]:
        lid = line_of.get(t["ref"].get("thread"))
        if lid is not None and lid not in kept and s["storylines"][str(lid)]["status"] == "open":
            kept.add(lid)
            t["ref"] = {"line": lid, "injection": t["ref"].get("injection")}
        else:
            t["open"] = False
            t["claim"] = None


def _from_single_story(s):
    """v2 -> v3. The one big story becomes the first storyline."""
    story = s.pop("story", None)
    for o in s.get("openings", []):
        o.setdefault("used", False)
        o.setdefault("line", None)
    if not story:
        return
    title = "The Story"
    o = next((x for x in s["openings"] if x["id"] == story.get("started_with")), None)
    if o and str(o["link"]) in s["links"]:
        l = get_link(s, o["link"])
        title = _line_title(s, [l["a"], l["b"]], "The Story")
    line = _new_line(s, title, story["cards"], story["scenes"], hook=story.get("hook"),
                     hook_author=story.get("hook_author"), stale=story.get("stale", 0),
                     used_words=list(story.get("used_words", [])), recaps=story.get("recaps", []),
                     last_beat_scene=story.get("last_beat_scene", len(story["scenes"])),
                     discord_thread=story.get("discord_thread"), started_with=story.get("started_with"))
    for sid in story["scenes"]:
        sc = s["scenes"][str(sid)]
        sc["line"] = line["id"]
        sc.pop("story", None)
    for o in s["openings"]:
        if o.get("used"):
            o["line"] = line["id"]
    for t in open_tasks(s):
        if t["type"] in ("SCENE", "RECAP"):
            t["ref"]["line"] = line["id"]


def _to_v4(s):
    """Storylines get a lifespan. A running storyline finishes its current stretch: it ends at the next
    multiple of `storyline_length` scenes (a v1 thread keeps its original length)."""
    L = s["settings"]["storyline_length"]
    for line in lines_of(s):
        line.setdefault("status", "open")
        line.setdefault("aftermath", None)
        line.setdefault("closed_at", None)
        if line.get("length") is None:
            n = len(line["scenes"])
            line["length"] = (n // L + 1) * L if L > 0 else 0


def migrate(s):
    """Upgrade a saved game in place. Returns True if anything changed.

    v1 (separate story threads): each thread becomes a storyline of its own, with its scenes,
        hook and Discord thread. Resolved threads become ended storylines (their ending recap is owed).
    v3 (endless storylines): each storyline finishes its current stretch and then ends.
    v2 (one big story): the story becomes the first storyline. Hooks that were queued for it
        now start new storylines while there is room (`max_storylines`).
    Old thread records are kept under "legacy_threads". Nothing players wrote is changed.
    """
    if not needs_migration(s):
        _fill_defaults(s)
        s.setdefault("storylines", {})
        return False
    ver = s.get("version", 1)
    st = s.setdefault("settings", {})
    if "chapter_length" not in st:
        st["chapter_length"] = st.get("thread_length", DEFAULTS["chapter_length"])
    if "storyline_length" not in st and "thread_length" in st:
        st["storyline_length"] = st["thread_length"]
    _fill_defaults(s)
    s.setdefault("storylines", {})
    if ver < 2:
        _from_threads(s)
    elif ver == 2:
        _from_single_story(s)
    _to_v4(s)
    place_openings(s)
    s["version"] = SAVE_VERSION
    ensure_scene_tasks(s)
    ensure_recaps(s)
    refresh_board(s)
    return True


# ---------------------------------------------------------- notifications --

NOTIFY_MODES = ("dm", "channel", "off")


def prefs(s, uid):
    """A player's notification settings. New players start with DMs on, and are only
    told about tasks created from now on (not everything already on the board)."""
    allp = s.setdefault("prefs", {})
    p = allp.get(uid)
    if p is None:
        p = allp[uid] = {"notify": "dm", "last_ping": 0, "notified_upto": s["next_id"] - 1,
                         "last_seen": time.time()}
    return p


def touch(s, uid, now=None):
    """The player is looking at the board, so they have seen everything created so far."""
    p = prefs(s, uid)
    p["last_seen"] = now or time.time()
    p["notified_upto"] = s["next_id"] - 1


def set_notify(s, uid, mode):
    if mode not in NOTIFY_MODES:
        raise ValueError(f"mode must be one of {NOTIFY_MODES}")
    prefs(s, uid)["notify"] = mode


def due_notifications(s, now=None):
    """{player id: [new tasks]} for everyone who should be pinged right now.

    A player is pinged about tasks they can take that were created since they last looked
    (or were last pinged), excluding tasks their own action created. Players who opted out,
    went quiet, or were pinged within the cooldown are skipped; their tasks are saved up
    and arrive together in the next ping."""
    now = now or time.time()
    st = s["settings"]
    out = {}
    for uid in s["players"]:
        p = prefs(s, uid)
        if p["notify"] == "off":
            continue
        if now - p["last_seen"] > st["inactive_days"] * 86400:
            continue
        if now - p["last_ping"] < st["notify_cooldown_min"] * 60:
            continue
        new = [t for t in visible_tasks(s, uid) if t["id"] > p["notified_upto"] and t.get("by") != uid]
        if new:
            out[uid] = new
    return out


def mark_notified(s, uid, now=None):
    p = prefs(s, uid)
    p["last_ping"] = now or time.time()
    p["notified_upto"] = s["next_id"] - 1


def notify_summary(tasks):
    """e.g. '2 x Ask a follow-up question, Write the next scene'. Labels only, so blind steps stay blind."""
    counts = {}
    for t in tasks:
        counts[t["type"]] = counts.get(t["type"], 0) + 1
    return ", ".join(f"{n} x {LABELS[ty]}" if n > 1 else LABELS[ty] for ty, n in counts.items())


def _stamp(s, start, uid):
    """Record who caused each task created since id `start` (so they aren't pinged about their own work)."""
    for t in reversed(s["tasks"]):
        if t["id"] < start:
            break
        t["by"] = uid


# ----------------------------------------------------------------- claims --

def claimed_by(s, t, now=None):
    c = t.get("claim")
    if not c:
        return None
    now = now or time.time()
    if now - c["at"] < s["settings"]["claim_hours"] * 3600:
        return c["by"]
    return None


def release_expired(s):
    n = 0
    for t in open_tasks(s):
        if t.get("claim") and claimed_by(s, t) is None:
            t["claim"] = None
            n += 1
    return n


def my_claim(s, uid):
    return next((t for t in open_tasks(s) if claimed_by(s, t) == uid), None)


def claim_task(s, uid, task_id):
    """Reserve a task for a player. Returns (task, error_message)."""
    t = find_task(s, task_id)
    if not t or not t["open"]:
        return None, "That task is no longer open."
    if uid not in eligible(s, t):
        hrs = s["settings"]["fresh_grace_hours"]
        return None, ("That one isn't available to you right now: you wrote the piece before it, or it's "
                      f"reserved for someone with fresh eyes for its first {hrs} hours.")
    holder = claimed_by(s, t)
    if holder and holder != uid:
        return None, "Someone else is working on that one right now."
    for other in open_tasks(s):  # one claim at a time
        if other["id"] != t["id"] and claimed_by(s, other) == uid:
            other["claim"] = None
    t["claim"] = {"by": uid, "at": time.time()}
    if t["type"] == "SCENE" and not t["ref"].get("spotlight"):
        t["ref"]["spotlight"] = spotlight(s, get_line(s, t["ref"]["line"]), t["ref"].get("injection"))  # freeze what the writer sees
    return t, None


def release_claim(s, uid):
    n = 0
    for t in open_tasks(s):
        if claimed_by(s, t) == uid:
            t["claim"] = None
            n += 1
    return n


def visible_tasks(s, uid):
    """Open tasks this player may take (not held by someone else)."""
    out = []
    for t in open_tasks(s):
        holder = claimed_by(s, t)
        if holder and holder != uid:
            continue
        if uid in eligible(s, t):
            out.append(t)
    return out


# ------------------------------------------------------------ eligibility --

def fresh(s, excluded, hard, relaxed=False):
    """Players not in `excluded` ("fresh eyes"). It is a preference, not a rule: if nobody
    qualifies (everyone has contributed), or the task has waited past `fresh_grace_hours`
    (the fresh players are away), anyone except the `hard` exclusions may take it."""
    if not relaxed:
        allowed = {p for p in s["players"] if p not in excluded}
        if allowed:
            return allowed
    return {p for p in s["players"] if p not in hard}


def waited_long(s, t, now=None):
    """Has this task been open longer than the fresh-eyes grace period?"""
    if "at" not in t:
        return False
    return (now or time.time()) - t["at"] > s["settings"]["fresh_grace_hours"] * 3600


def eligible(s, t):
    ty, r = t["type"], t["ref"]
    everyone = set(s["players"])
    if len(everyone) < 2:  # solo testing: no blind rules possible
        return everyone
    if ty in ("ROOT", "APPEND"):
        return everyone
    late = waited_long(s, t)
    if ty in ("ASK", "ANSWER"):  # never two consecutive steps in the same chain
        return everyone - {get_chain(s, r["chain"])["pieces"][-1]["author"]}
    if ty == "WEAVE":
        contributors = {p["author"] for p in get_chain(s, r["chain"])["pieces"]}
        return fresh(s, contributors, hard=set(), relaxed=late)
    if ty == "CROSS_ASK":
        a, b = get_card(s, r["a"]), get_card(s, r["b"])
        return fresh(s, {a["author"], b["author"]}, hard=set(), relaxed=late)
    if ty == "CROSS_ANSWER":
        q = get_link(s, r["link"])["q_author"]
        return fresh(s, {q}, hard={q}, relaxed=late)
    if ty == "OPEN":
        l = get_link(s, r["link"])
        return fresh(s, {l["q_author"], l["a_author"]}, hard={l["a_author"]}, relaxed=late)
    if ty == "SCENE":  # never two scenes in a row by the same player in the same storyline
        line = get_line(s, r["line"])
        if line["scenes"]:
            return everyone - {get_scene(s, line["scenes"][-1])["author"]}
        return everyone
    if ty == "RECAP":  # preferably someone who didn't write that chapter; anyone after the grace period
        authors = {sc["author"] for sc in chapter_scenes(s, get_line(s, r["line"]), r["chapter"])}
        return fresh(s, authors, hard=set(), relaxed=late)
    return set()


# --------------------------------------------------------- what you can see --

def chain_lines(s, c, level=1):
    out = []
    if c["root_question"]:
        out.append((level, f"Seed question: {c['root_question']}"))
    for p in c["pieces"]:
        out.append((level, ("A: " if p["role"] == "answer" else "Q: ") + p["text"]))
    return out


def related_cards(s, c):
    """Cards linked through hand-offs: the chain this one grew out of, and chains that grew out of it."""
    if not c["chain"]:
        return []
    chain = get_chain(s, c["chain"])
    found = []
    if chain["parent"]:
        pc = get_chain(s, chain["parent"])["card"]
        if pc:
            found.append(("grew out of", get_card(s, pc)))
    for other in s["chains"].values():
        if other["parent"] == chain["id"] and other["card"]:
            found.append(("led to", get_card(s, other["card"])))
    return found


def card_context(s, c, title, log_n=3):
    """A card laid out with everything behind it."""
    lines = [f"{title}: {fmt_card(c)}"]
    if c["state"]:
        lines.append((1, f"Current state: {c['state']}"))
    if c["chain"]:
        lines.append((1, "How it came to be:"))
        lines += chain_lines(s, get_chain(s, c["chain"]), 2)
    else:
        lines.append((1, "Introduced during the story."))
    for entry in c["log"][-log_n:]:
        lines.append((1, f"Added later: {entry['text']}"))
    for how, other in related_cards(s, c):
        lines.append((1, f"Linked card ({how}): {fmt_card(other)}"))
    lines.append("")
    return lines


def card_brief(s, c, log_n=2):
    lines = [fmt_card(c)]
    if c["state"]:
        lines.append((1, f"Now: {c['state']}"))
    for entry in c["log"][-log_n:]:
        lines.append((1, f"+ {entry['text']}"))
    return lines


def parent_context(s, chain):
    """Background for people continuing a chain that grew out of another one."""
    if not chain["parent"] or not s["settings"].get("handoff_context", True):
        return []
    parent = get_chain(s, chain["parent"])
    lines = ["Background - this chain grew out of an earlier one:"]
    if parent["card"]:
        lines.append((1, fmt_card(get_card(s, parent["card"]))))
    else:
        lines += chain_lines(s, parent, 1)
    lines.append("")
    return lines


def F(key, label, validator=None, default=None, long=False, required=True, max=None):
    """A form field. `label` must be short (Discord modal labels max out at 45 chars)."""
    return {"key": key, "label": label, "validator": validator, "default": default,
            "long": long, "required": required, "max": max}


def check_question(text):
    if not text.endswith("?"):
        return "Please phrase that as a question (ending with '?')."
    first = text.split()[0].lower().strip("\"'")
    if first in YES_NO_STARTS:
        return "That looks like a yes/no question. Ask for something more specific."
    return None


def check_kind(text):
    return None if text[:1].lower() in KIND_MAP else "Type c (character), p (place) or t (thing/concept/situation)."


VALIDATORS = {"question": check_question, "kind": check_kind}
SOFT_VALIDATORS = {"question"}  # the player may override these


def validate(spec, values):
    """Returns None, or (field_key, message, soft) for the first problem."""
    for f in spec["fields"]:
        v = (values.get(f["key"]) or "").strip()
        if not v:
            if f["required"]:
                return f["key"], f"'{f['label']}' can't be empty.", False
            continue
        fn = VALIDATORS.get(f["validator"])
        msg = fn(v) if fn else None
        if msg:
            return f["key"], msg, f["validator"] in SOFT_VALIDATORS
    return None


def opening_lines(s, o):
    """An opening laid out in full: both cards, the link, and the hook."""
    l = get_link(s, o["link"])
    a, b = get_card(s, l["a"]), get_card(s, l["b"])
    return [fmt_card(a), fmt_card(b), f"Link: {l['question']}  ->  {l['answer']}", f"Hook: {o['hook']}"]


def story_so_far(s, line, max_scenes=3):
    """Lines for a storyline's latest recap(s) plus its most recent scenes since them."""
    lines = []
    recaps = line["recaps"][-2:]
    for r in recaps:
        lines.append(f"Chapter {r['chapter']} - {r['title']}: {clip(r['text'], 700)}")
    last_recapped = recaps[-1]["chapter"] if recaps else 0
    since = [get_scene(s, sid) for sid in line["scenes"]
             if get_scene(s, sid).get("chapter", 1) > last_recapped]
    shown = since[-max_scenes:]
    if len(since) > len(shown):
        lines.append(f"({len(since) - len(shown)} earlier scene(s) not shown)")
    for sc in shown:
        lines.append(f"Scene {sc['number']} ({name_of(s, sc['author'])}): {clip(sc['text'], 600)}")
    return lines or ["(nothing has happened yet)"]


def elsewhere_lines(s, line, limit=3):
    """A one-line digest of what is going on in the other storylines (the glue between them)."""
    others = [l for l in open_lines(s) if l["id"] != line["id"]]
    others.sort(key=lambda l: -(get_scene(s, l["scenes"][-1]).get("at", 0) if l["scenes"] else 0))
    out = []
    for l in others[:limit]:
        if l["scenes"]:
            text = get_scene(s, l["scenes"][-1])["text"]
        elif l["recaps"]:
            text = l["recaps"][-1]["text"]
        else:
            text = l["hook"] or "just beginning"
        out.append(f"{l['title']}: {clip(text, 220)}")
    return out


def history_lines(s, line, limit=3):
    """Echoes of storylines that already ended: the ones sharing cards with this storyline first,
    then the most recent. This is how a finished storyline affects the ones that come after it."""
    mine = set(line["cards"])
    rows = []
    for l in closed_lines(s):
        e = ending_of(l)
        if l["id"] == line["id"] or not e:
            continue
        shared = len(mine & set(l["cards"]))
        rows.append((-shared, -(l.get("closed_at") or 0), l, e, shared))
    rows.sort(key=lambda x: x[:2])
    out = []
    for _, _, l, e, shared in rows[:limit]:
        names = [get_card(s, c)["name"] for c in l["cards"] if c in mine][:3]
        tag = f" (involved: {', '.join(names)})" if shared else ""
        text = f'"{e["title"]}" ({l["title"]}){tag}: {clip(e["text"], 260)}'
        if l.get("aftermath"):
            text += f" Aftermath: {clip(l['aftermath'], 220)}"
        out.append(text)
    return out


def describe(s, t):
    """The isolation rules live here: what this task shows, and what it asks for."""
    ty, r = t["type"], t["ref"]
    if ty == "ROOT":
        return dict(lines=[r["question"]],
                    tip="Answer about yourself, right now. A word or a short phrase is plenty.",
                    fields=[F("text", "Your answer", max=200)])
    if ty == "ASK":
        c = get_chain(s, r["chain"])
        last = c["pieces"][-1]
        return dict(lines=parent_context(s, c) +
                    [f'All you can see of this chain is the last answer:  "{last["text"]}"'],
                    tip="The next person sees ONLY your question - not this answer. "
                        "Include whatever they need, and ask for something more specific "
                        "than what is already here.",
                    fields=[F("text", "Your question", "question", long=True, max=400)])
    if ty == "ANSWER":
        c = get_chain(s, r["chain"])
        last = c["pieces"][-1]
        return dict(lines=parent_context(s, c) +
                    [f'All you can see of this chain is the last question:  "{last["text"]}"'],
                    tip=None, fields=[F("text", "Your answer", long=True, max=600)])
    if ty == "WEAVE":
        c = get_chain(s, r["chain"])
        lines = chain_lines(s, c, 0)
        if c["parent"]:
            parent = get_chain(s, c["parent"])
            lines += ["", "Context - this chain grew out of an earlier one:"]
            if parent["card"]:
                lines.append((1, fmt_card(get_card(s, parent["card"]))))
            else:
                lines += chain_lines(s, parent, 1)
        for other in s["chains"].values():
            if other["parent"] == c["id"] and other["card"]:
                lines += ["", "A chain that grew out of this one became:",
                          (1, fmt_card(get_card(s, other["card"])))]
        return dict(lines=lines,
                    tip="This chain is a pile of answers from different people, so it may "
                        "contradict itself. Decide what it is describing, then fill in three "
                        "fields. NAME: a short title (one to three words), like 'Marrow', "
                        "'the Salt Flats' or 'the Stopped Tide'. DESCRIPTION: one or two "
                        "sentences pulling the details together; keep contradictions as "
                        "secrets or mysteries. KIND: c = character, p = place, t = thing - "
                        "where 'thing' also covers a concept or a situation, i.e. an idea "
                        "about the world such as a problem, a rule or a tension.",
                    fields=[F("name", "Name (short title)", max=60),
                            F("text", "Description (1-2 sentences)", long=True, max=600),
                            F("kind", "Kind: c, p or t", "kind",
                              default=(c["kind"] or "character")[0], max=1)])
    if ty == "CROSS_ASK":
        a, b = get_card(s, r["a"]), get_card(s, r["b"])
        return dict(lines=card_context(s, a, "Card 1") + card_context(s, b, "Card 2"),
                    tip="Write ONE question about how these two are connected - for example "
                        "what one wants from the other, or what happened between them. "
                        "The next person will answer it.",
                    fields=[F("text", "Your question", "question", long=True, max=400)])
    if ty == "CROSS_ANSWER":
        l = get_link(s, r["link"])
        a, b = get_card(s, l["a"]), get_card(s, l["b"])
        return dict(lines=card_context(s, a, "Card 1") + card_context(s, b, "Card 2") +
                    [f'The question: "{l["question"]}"'],
                    tip=None, fields=[F("text", "Your answer", long=True, max=600)])
    if ty == "OPEN":
        l = get_link(s, r["link"])
        a, b = get_card(s, l["a"]), get_card(s, l["b"])
        return dict(lines=card_context(s, a, "Card 1") + card_context(s, b, "Card 2") +
                    ["The link between them:", (1, f"Q: {l['question']}"), (1, f"A: {l['answer']}")],
                    tip="Write the hook for this pairing: one or two sentences setting up a "
                        "moment, a problem, or a question. It will either start a new storyline "
                        "or arrive in an existing one as a new development, so think of it as "
                        "something happening in a world that already exists.",
                    fields=[F("text", "Your hook", long=True, max=600)])
    if ty == "SCENE":
        line = get_line(s, r["line"])
        n = len(line["scenes"]) + 1
        cl = s["settings"]["chapter_length"]
        L = line.get("length") or 0
        ch, pos = chapter_of(s, n), (n - 1) % cl + 1
        if L:
            lines = [f"Storyline: {line['title']}", f"Scene {n} of {L}"]
            if n == L:
                lines.append("THIS IS THE FINAL SCENE - bring the storyline to its ending.")
            elif n == L - 1:
                lines.append("One more scene after this one: start steering toward the ending.")
        else:
            lines = [f"Storyline: {line['title']}",
                     f"Chapter {ch} - scene {pos} of {cl}   (scene {n} of this storyline)"]
        if pos == 1 and ch > 1 and cl < (L or 10**9):
            lines.append("A new chapter begins.")
        lines.append("")
        if line["hook"]:
            lines.append(f'Hook from {name_of(s, line["hook_author"])}: "{line["hook"]}"')
        else:
            lines.append("No hook was left - continue from the story so far in any direction you like.")
        inj = r.get("injection")
        if inj and inj["kind"] == "beat":
            lines += ["", "A NEW DEVELOPMENT enters the story:"]
            lines += [(1, x) for x in opening_lines(s, get_opening(s, inj["opening"]))]
        elif inj and inj["kind"] == "arrival":
            lines += ["", "WILDCARD - a card enters the story:"] + card_brief(s, get_card(s, inj["card"]))
        elif inj and inj["kind"] == "crossover":
            origin = get_line(s, inj["from"])["title"] if str(inj.get("from")) in s["storylines"] else "another storyline"
            lines += ["", f"WILDCARD - a card crosses over from {origin}:"] + card_brief(s, get_card(s, inj["card"]))
        elif inj and inj["kind"] == "word":
            lines += ["", f'WILDCARD - work this into the scene somewhere: "{inj["text"]}"']
        spot = r.get("spotlight") or spotlight(s, line, inj)
        lines += ["", "In the spotlight:"]
        for cid in spot:
            lines += [(1, x) if isinstance(x, str) else (x[0] + 1, x[1])
                      for x in card_brief(s, get_card(s, cid))]
        others = [get_card(s, c)["name"] for c in line["cards"] if c not in spot]
        if others:
            more = f" (+{len(others) - 15} more)" if len(others) > 15 else ""
            lines += ["", "Also in this storyline: " + ", ".join(others[:15]) + more]
        elsewhere = elsewhere_lines(s, line)
        if elsewhere:
            lines += ["", "Meanwhile, elsewhere in the world:"] + [(1, x) for x in elsewhere]
        echoes = history_lines(s, line)
        if echoes:
            lines += ["", "Echoes of storylines that already ended (the world remembers):"] + [(1, x) for x in echoes]
        lines += ["", "The story so far:"] + [(1, x) for x in story_so_far(s, line)]
        if L and n == L:
            tip = ("Write the final scene (a paragraph or two): resolve what you can, and leave the "
                   "world changed. The next person recaps the ending and says what is different now. "
                   "You can still add to cards or create a new card afterwards.")
        else:
            tip = ("Write the next scene (a paragraph or two). This storyline is one thread of a "
                   "larger world that will end after a set number of scenes, so build on what's here and "
                   "feel free to nod to what's happening elsewhere. Afterwards you can add to the "
                   "cards, create a new card, and leave a hook for the next writer.")
        return dict(lines=lines, tip=tip,
                    fields=[F("text", "Your scene", long=True, max=3500)])
    if ty == "RECAP":
        line = get_line(s, r["line"])
        ch = r["chapter"]
        ending = line["status"] == "closed" and ch == final_chapter(s, line)
        if ending:
            lines = [f"Storyline: {line['title']} - THE ENDING", "The whole storyline:"]
            for sid in line["scenes"]:
                sc = get_scene(s, sid)
                lines.append((1, f"{sc['number']}. {clip(sc['text'], 450)}"))
            return dict(lines=lines,
                        tip="This storyline has ended. Give it a title, recap it in a few sentences for "
                            "people who weren't here, and then say what is different in the world now. "
                            "That aftermath is written into the founding cards, so later storylines "
                            "will carry it.",
                        fields=[F("title", "Title of the storyline", max=60),
                                F("text", "Recap (a few sentences)", long=True, max=900),
                                F("aftermath", "Aftermath: what has changed in the world?", long=True, max=500)])
        lines = [f"Storyline: {line['title']}", f"Chapter {ch} - the scenes:"]
        for sc in chapter_scenes(s, line, ch):
            lines.append((1, f"{sc['number']}. {clip(sc['text'], 600)}"))
        return dict(lines=lines,
                    tip="Give this chapter a short title and recap it in a few sentences for people "
                        "who are just joining: what happened, who's where, what's unresolved.",
                    fields=[F("title", "Chapter title", max=60),
                            F("text", "Recap (a few sentences)", long=True, max=900)])
    if ty == "APPEND":
        c = get_card(s, r["card"])
        return dict(lines=card_context(s, c, "Card", log_n=5),
                    tip="Add one line: a new fact, a secret, a change - something others can "
                        "build on. Optionally rewrite the card's one-line current state.",
                    fields=[F("text", "New fact or change", long=True, max=500),
                            F("state", "Current state (optional)", required=False, max=200)])
    raise ValueError(ty)


def preview(s, t):
    ty, r = t["type"], t["ref"]
    if ty == "ROOT":
        return r["question"]
    if ty in ("ASK", "ANSWER"):
        c = get_chain(s, r["chain"])
        return f'chain #{c["id"]}: "{clip(c["pieces"][-1]["text"], 50)}"'
    if ty == "WEAVE":
        c = get_chain(s, r["chain"])
        return f'chain #{c["id"]} ({len(c["pieces"])} pieces)'
    if ty == "CROSS_ASK":
        return f'{get_card(s, r["a"])["name"]} + {get_card(s, r["b"])["name"]}'
    if ty in ("CROSS_ANSWER", "OPEN"):
        l = get_link(s, r["link"])
        return f'{get_card(s, l["a"])["name"]} + {get_card(s, l["b"])["name"]}'
    if ty == "SCENE":
        line = get_line(s, r["line"])
        n = len(line["scenes"]) + 1
        inj = r.get("injection")
        tag = ""
        if inj:
            tag = " [new development]" if inj["kind"] == "beat" else " [wildcard]"
        return f"{clip(line['title'], 40)}: chapter {chapter_of(s, n)}, scene {n}{tag}"
    if ty == "RECAP":
        return f"{clip(get_line(s, r['line'])['title'], 40)}: chapter {r['chapter']}"
    if ty == "APPEND":
        return get_card(s, r["card"])["name"]
    return ""


# ------------------------------------------------------------- storylines --

def get_line(s, i):
    return s["storylines"][str(i)]


def lines_of(s):
    return list(s.get("storylines", {}).values())


def open_lines(s):
    return [l for l in lines_of(s) if l.get("status", "open") == "open"]


def closed_lines(s):
    return [l for l in lines_of(s) if l.get("status") == "closed"]


def ending_of(line):
    """The ending recap of an ended storyline (or None while it is still owed)."""
    if line.get("status") != "closed" or not line["scenes"]:
        return None
    return next((r for r in line["recaps"] if r.get("ending")), None)


def final_chapter(s, line):
    return chapter_of(s, len(line["scenes"]))


def chapter_scenes(s, line, chapter):
    return [get_scene(s, sid) for sid in line["scenes"]
            if get_scene(s, sid).get("chapter", 1) == chapter]


def spotlight(s, line, injection=None):
    """Which cards a scene prompt puts in the spotlight. Deterministic: new arrivals first,
    then one card the last scene changed (continuity), then whoever has been out of the
    spotlight the longest - so the whole cast of the storyline rotates through."""
    size = s["settings"]["spotlight_size"]
    required = []
    if injection and injection["kind"] in ("arrival", "crossover"):
        required = [injection["card"]]
    elif injection and injection["kind"] == "beat":
        l = get_link(s, get_opening(s, injection["opening"])["link"])
        required = [l["a"], l["b"]]
    last_seen = {}
    for idx, sid in enumerate(line["scenes"]):
        sc = get_scene(s, sid)
        for cid in list(sc.get("spotlight", [])) + list(sc.get("touched", [])):
            last_seen[cid] = idx
    pool = [c for c in line["cards"] if c not in required]
    carry = []
    if line["scenes"]:
        touched = get_scene(s, line["scenes"][-1]).get("touched", [])
        carry = [c for c in touched if c in pool][:1]
    rest = sorted((c for c in pool if c not in carry), key=lambda c: (last_seen.get(c, -1), c))
    return (required + carry + rest)[: max(size, len(required))]


def scene_cards(s, scene_id, limit=25):
    """Cards a scene's author can add to: the spotlight first, then the rest of the storyline's cast."""
    sc = get_scene(s, scene_id)
    line = get_line(s, sc["line"])
    ordered = list(sc.get("spotlight", []))
    ordered += [c for c in line["cards"] if c not in ordered]
    return [c for c in ordered if str(c) in s["cards"]][:limit]


def pending_openings(s, line=None):
    """Finished hooks not yet part of any storyline (optionally: those queued for one storyline)."""
    if line is not None:
        return [get_opening(s, i) for i in line["queued"]]
    return [o for o in s["openings"] if not o.get("used")]


def use_opening(s, o, line):
    """Bring an opening's two cards into a storyline."""
    o["used"] = True
    o["line"] = line["id"]
    l = get_link(s, o["link"])
    l["used"] = True
    for cid in (l["a"], l["b"]):
        get_card(s, cid)["in_play"] = True
        if cid not in line["cards"]:
            line["cards"].append(cid)


def start_line(s, o):
    """A new storyline, started by a finished hook."""
    l = get_link(s, o["link"])
    line = _new_line(s, _line_title(s, [l["a"], l["b"]]), [], [], hook=o["hook"],
                     hook_author=o["author"], started_with=o["id"],
                     length=s["settings"]["storyline_length"])
    use_opening(s, o, line)
    add_task(s, "SCENE", line=line["id"], injection=None)
    return line


def place_openings(s):
    """Give every finished hook a home: a new storyline while there is room, otherwise a
    development queued for the storyline that has seen the least action."""
    events = []
    for o in s["openings"]:
        if o.get("used") or o.get("line") is not None:
            continue
        lines = open_lines(s)
        if len(lines) < max(s["settings"]["max_storylines"], 1):
            line = start_line(s, o)
            events.append({"type": "storyline_start", "line": line["id"], "opening": o["id"]})
        else:
            line = min(lines, key=lambda l: (len(l["queued"]), len(l["scenes"]), l["id"]))
            o["line"] = line["id"]
            line["queued"].append(o["id"])
            events.append({"type": "opening_queued", "opening": o["id"], "line": line["id"]})
    return events


def crossover_candidates(s, line):
    """Cards that live in other storylines but not in this one: [(card id, origin line id)]."""
    mine = set(line["cards"])
    out, seen = [], set()
    for other in lines_of(s):
        if other["id"] == line["id"]:
            continue
        for cid in other["cards"]:
            if cid not in mine and cid not in seen:
                seen.add(cid)
                out.append((cid, other["id"]))
    return out


def _crossover(s, line, abroad):
    """Move a card from another storyline into this one. Cards from storylines still running are
    preferred, because those are real intersections."""
    live = {l["id"] for l in open_lines(s)}
    pool = [x for x in abroad if x[1] in live] or abroad
    cid, origin = random.choice(pool)
    line["last_cross"] = len(line["scenes"])
    line["cards"].append(cid)
    return {"kind": "crossover", "card": cid, "from": origin}


def maybe_inject(s, line):
    """Entropy: the staler a storyline, the likelier a wildcard shows up."""
    stale = line["stale"]
    if stale < 2:
        return None
    if random.random() >= min(0.75, 0.25 * (stale - 1)):
        return None
    unplaced = [c for c in s["cards"].values() if not c["in_play"] and not c["linked"]]
    n = len(line["scenes"])
    # crossovers are occasional, so storylines keep their own identity instead of blurring together
    cooling = n - line.get("last_cross", -99) < max(3, (line.get("length") or s["settings"]["chapter_length"]) // 2)
    abroad = [] if cooling else crossover_candidates(s, line)
    words = [w for w in s["words"] if w not in line["used_words"]]
    options = ["arrival"] * 3 if unplaced else []
    options += ["crossover"] * 2 if abroad else []
    options += ["word"] * 2 if words else []
    if not options:
        return None
    line["stale"] = 0
    pick = random.choice(options)
    if pick == "arrival":
        c = random.choice(unplaced)
        c["in_play"] = True
        line["cards"].append(c["id"])
        return {"kind": "arrival", "card": c["id"]}
    if pick == "crossover":
        return _crossover(s, line, abroad)
    w = random.choice(words)
    line["used_words"].append(w)
    return {"kind": "word", "text": w}


def queue_scene(s, line):
    """Create the next scene task for a storyline. A development queued for it is woven in once
    enough scenes have passed; otherwise it may get a wildcard. Returns the injection (or None)."""
    n = len(line["scenes"])
    pend = pending_openings(s, line)
    if pend and n - line["last_beat_scene"] >= s["settings"]["beat_gap"]:
        o = pend[0]
        line["queued"].remove(o["id"])
        use_opening(s, o, line)
        line["last_beat_scene"] = n
        line["stale"] = 0
        injection = {"kind": "beat", "opening": o["id"]}
    else:
        injection = None
        L = line.get("length") or 0
        if L and "last_cross" not in line and L // 2 <= n < L - 1:
            abroad = crossover_candidates(s, line)
            if abroad:   # guaranteed intersection around the midpoint
                injection = _crossover(s, line, abroad)
                line["stale"] = 0
        if injection is None:
            injection = maybe_inject(s, line)
    add_task(s, "SCENE", line=line["id"], injection=injection)
    return injection


def close_line(s, line):
    """The storyline has had its last scene: it ends. Hooks queued for it are re-homed (the freed
    slot starts a new storyline), and the ending recap (with the aftermath) is owed."""
    line["status"] = "closed"
    line["closed_at"] = time.time()
    line["hook"], line["hook_author"] = None, None
    for t in open_tasks(s):
        if t["type"] == "SCENE" and t["ref"].get("line") == line["id"]:
            t["open"] = False
            t["claim"] = None
    for oid in list(line["queued"]):
        get_opening(s, oid)["line"] = None
    line["queued"] = []
    events = [{"type": "storyline_end", "line": line["id"]}]
    events += place_openings(s)
    return events


def _founders(s, line):
    """The two cards a storyline began with (or its first two cards, for upgraded storylines)."""
    o = next((x for x in s["openings"] if x["id"] == line.get("started_with")), None)
    if o and str(o["link"]) in s["links"]:
        l = get_link(s, o["link"])
        return [l["a"], l["b"]]
    return list(line["cards"][:2])


def ensure_scene_tasks(s):
    """Every storyline always has exactly one open scene task."""
    have = {t["ref"].get("line") for t in open_tasks(s) if t["type"] == "SCENE"}
    for line in open_lines(s):
        if line["id"] not in have:
            queue_scene(s, line)


def ensure_recaps(s):
    """Every finished chapter of every storyline gets a recap task (unless it already has one)."""
    cl = s["settings"]["chapter_length"]
    for line in lines_of(s):
        n = len(line["scenes"])
        finished = -(-n // cl) if line.get("status") == "closed" else n // cl
        have = {r["chapter"] for r in line["recaps"]}
        queued = {t["ref"]["chapter"] for t in open_tasks(s)
                  if t["type"] == "RECAP" and t["ref"].get("line") == line["id"]}
        for ch in range(1, finished + 1):
            if ch not in have and ch not in queued:
                add_task(s, "RECAP", line=line["id"], chapter=ch)


# --------------------------------------------------------- state changes --

def score_pair(s, a, b):
    if {a["kind"], b["kind"]} == {"character", "place"}:
        score = 3
    elif a["kind"] != b["kind"]:
        score = 2
    else:
        score = 0
    ca, cb = get_chain(s, a["chain"]), get_chain(s, b["chain"])
    if ca["parent"] != cb["id"] and cb["parent"] != ca["id"]:
        score += 1
    return score


def pending_cards(s):
    open_roots = sum(1 for t in open_tasks(s) if t["type"] == "ROOT")
    unwoven = sum(1 for c in s["chains"].values() if c["card"] is None)
    return open_roots + unwoven


def try_crossings(s):
    """Pair up unlinked cards into developments. Waits for a good pairing (character + place)
    while more cards are still on the way, and pauses while enough developments are waiting."""
    events = []
    while True:
        waiting = sum(1 for l in s["links"].values() if not l.get("used"))
        if waiting >= s["settings"]["max_pipeline"]:
            break
        free = [c for c in s["cards"].values() if not c["linked"] and c["chain"] and not c["in_play"]]
        best, best_score = None, -1
        for i, a in enumerate(free):
            for b in free[i + 1:]:
                sc = score_pair(s, a, b)
                if sc > best_score:
                    best, best_score = (a, b), sc
        if not best or (best_score < 2 and pending_cards(s) > 0):
            break
        a, b = best
        a["linked"] = b["linked"] = True
        lid = nid(s)
        s["links"][str(lid)] = {"id": lid, "a": a["id"], "b": b["id"], "question": None,
                                "answer": None, "q_author": None, "a_author": None, "used": False}
        add_task(s, "CROSS_ASK", a=a["id"], b=b["id"], link=lid)
        events.append({"type": "crossing", "link": lid})
    return events


def refresh_board(s):
    """Keep the board stocked: recaps, append slots for thin cards, and a background seed question."""
    st = s["settings"]
    ensure_scene_tasks(s)
    ensure_recaps(s)
    if s.get("storylines"):
        open_app = [t for t in open_tasks(s) if t["type"] == "APPEND"]
        while len(open_app) < st["append_slots"]:
            taken = {t["ref"]["card"] for t in open_app}
            cands = [c for c in s["cards"].values() if c["id"] not in taken]
            if not cands:
                break
            thin = min(cands, key=lambda c: (len(c["log"]) + (1 if c["state"] else 0), random.random()))
            open_app.append(add_task(s, "APPEND", card=thin["id"]))
    if s["seeding_done"]:
        open_roots = [t for t in open_tasks(s) if t["type"] == "ROOT"]
        backlog = (sum(1 for c in s["chains"].values() if c["card"] is None) +
                   sum(1 for c in s["cards"].values() if not c["in_play"]))
        if not open_roots and backlog < 6:
            q, kind = random.choice(ROOT_QUESTIONS)
            add_task(s, "ROOT", question=q, kind=kind)


def perform(s, uid, t, f):
    """Apply a completed task. Returns a list of events."""
    ty, r = t["type"], t["ref"]
    holder = claimed_by(s, t)
    if holder and holder != uid:
        raise ValueError("task is claimed by someone else")
    if not t["open"]:
        raise ValueError("task already done")
    start = s["next_id"]
    events = []
    if ty == "ROOT":
        c = new_chain(s, r["kind"], r["question"], piece("answer", f["text"], uid))
        add_task(s, "ASK", chain=c["id"])
        word = f["text"].strip()
        if len(word) <= 60:
            s["words"].append(word)
    elif ty == "ASK":
        c = get_chain(s, r["chain"])
        c["pieces"].append(piece("question", f["text"], uid))
        add_task(s, "ANSWER", chain=c["id"])
    elif ty == "ANSWER":
        c = get_chain(s, r["chain"])
        c["pieces"].append(piece("answer", f["text"], uid))
        pairs = sum(1 for p in c["pieces"] if p["role"] == "question")
        if pairs >= s["settings"]["hops"]:
            c["done"] = True
            add_task(s, "WEAVE", chain=c["id"])
            events.append({"type": "reveal", "chain": c["id"]})
            do_handoff = s["handoffs_done"] < s["settings"]["handoffs"]
            if do_handoff:
                s["handoffs_done"] += 1
            elif s["seeding_done"] and random.random() < s["settings"]["ongoing_handoff_prob"]:
                do_handoff = True
            if do_handoff:
                first = piece("answer", f["text"], uid, handoff=True)
                child = new_chain(s, None, None, first, parent=c["id"])
                add_task(s, "ASK", chain=child["id"])
                events.append({"type": "handoff", "chain": child["id"], "parent": c["id"]})
        else:
            add_task(s, "ASK", chain=c["id"])
    elif ty == "WEAVE":
        c = get_chain(s, r["chain"])
        card = make_card(s, f["name"].strip(), f["text"].strip(),
                         KIND_MAP[f["kind"].strip()[0].lower()], uid, chain=c["id"])
        c["card"] = card["id"]
        events.append({"type": "card", "card": card["id"]})
    elif ty == "CROSS_ASK":
        l = get_link(s, r["link"])
        l["question"], l["q_author"] = f["text"], uid
        add_task(s, "CROSS_ANSWER", link=l["id"])
    elif ty == "CROSS_ANSWER":
        l = get_link(s, r["link"])
        l["answer"], l["a_author"] = f["text"], uid
        add_task(s, "OPEN", link=l["id"])
    elif ty == "OPEN":
        o = {"id": nid(s), "link": r["link"], "hook": f["text"], "author": uid, "used": False, "line": None}
        s["openings"].append(o)
        events += place_openings(s)
        if len(s["openings"]) >= s["settings"]["goal"] and not s["seeding_done"]:
            s["seeding_done"] = True
            events.append({"type": "seeding_done"})
    elif ty == "SCENE":
        line = get_line(s, r["line"])
        n = len(line["scenes"]) + 1
        sid = nid(s)
        s["scenes"][str(sid)] = {"id": sid, "line": line["id"], "text": f["text"], "author": uid,
                                 "at": time.time(), "number": n, "chapter": chapter_of(s, n),
                                 "spotlight": list(r.get("spotlight") or spotlight(s, line, r.get("injection"))),
                                 "injection": r.get("injection"), "touched": []}
        line["scenes"].append(sid)
        line["hook"], line["hook_author"] = None, None  # the next hook comes from this scene's author
        line["stale"] += 1
        events.append({"type": "scene", "scene": sid, "line": line["id"]})
        if line.get("length") and n >= line["length"]:
            events += close_line(s, line)
        else:
            if n % s["settings"]["chapter_length"] == 0:
                events.append({"type": "chapter_end", "line": line["id"], "chapter": chapter_of(s, n)})
            inj = queue_scene(s, line)
            if inj and inj["kind"] == "beat":
                events.append({"type": "beat", "line": line["id"], "opening": inj["opening"]})
    elif ty == "RECAP":
        line = get_line(s, r["line"])
        ending = line["status"] == "closed" and r["chapter"] == final_chapter(s, line)
        rec = {"chapter": r["chapter"], "title": f["title"].strip(),
               "text": f["text"].strip(), "author": uid, "at": time.time()}
        if ending:
            rec["ending"] = True
            rec["aftermath"] = f["aftermath"].strip()
            line["aftermath"] = rec["aftermath"]
            founders = [c for c in _founders(s, line) if str(c) in s["cards"]]
            for cid in founders:
                add_append(s, uid, cid, f'Aftermath of "{rec["title"]}": {rec["aftermath"]}')
        line["recaps"].append(rec)
        line["recaps"].sort(key=lambda x: x["chapter"])
        events.append({"type": "recap", "line": line["id"], "chapter": r["chapter"]})
    elif ty == "APPEND":
        add_append(s, uid, r["card"], f["text"], (f.get("state") or "").strip() or None)
        events.append({"type": "append", "card": r["card"]})
    t["open"] = False
    t["claim"] = None
    refresh_board(s)
    events += try_crossings(s)
    _stamp(s, start, uid)
    return events


# ----------------------------------------- follow-ups after writing a scene --

def add_append(s, uid, card_id, text, state=None, scene_id=None):
    c = get_card(s, card_id)
    c["log"].append({"text": text.strip(), "author": uid, "scene": scene_id, "at": time.time()})
    if state:
        c["state"] = state.strip()


def _own_scene(s, uid, scene_id):
    sc = get_scene(s, scene_id)
    if sc["author"] != uid:
        raise ValueError("only the scene's author can do that")
    return sc, get_line(s, sc["line"])


def append_to_card(s, uid, scene_id, card_id, text, state=None):
    sc, line = _own_scene(s, uid, scene_id)
    if card_id not in line["cards"]:
        raise ValueError("that card isn't part of this storyline yet")
    start = s["next_id"]
    add_append(s, uid, card_id, text, state, scene_id)
    if card_id not in sc["touched"]:
        sc["touched"].append(card_id)
    refresh_board(s)
    _stamp(s, start, uid)
    return [{"type": "append", "card": card_id, "line": line["id"]}]


def promote_card(s, uid, scene_id, name, text, kind_letter):
    """The scene invented something new: make it a card (shared by the whole world) and bring it into the storyline."""
    sc, line = _own_scene(s, uid, scene_id)
    start = s["next_id"]
    card = make_card(s, name.strip(), text.strip(), KIND_MAP[kind_letter.strip()[0].lower()],
                     uid, chain=None, in_play=True)
    card["log"].append({"text": f"Introduced in {line['title']}, scene {sc['number']}.", "author": uid,
                        "scene": scene_id, "at": time.time()})
    line["cards"].append(card["id"])
    sc["touched"].append(card["id"])
    line["stale"] = 0
    refresh_board(s)
    _stamp(s, start, uid)
    return card, [{"type": "card", "card": card["id"]}]


def set_hook(s, uid, scene_id, text):
    sc, line = _own_scene(s, uid, scene_id)
    if line.get("status") == "closed":
        raise ValueError("this storyline has ended - say what changed in the ending recap instead")
    if line["scenes"][-1] != scene_id:
        raise ValueError("a newer scene exists, so the hook can't be changed")
    line["hook"], line["hook_author"] = text.strip(), uid
    return [{"type": "hook", "line": line["id"]}]


# ----------------------------------------------------------------- views --

def status_lines(s):
    chains = list(s["chains"].values())
    lines = [f"Players: {len(s['players'])}",
             f"Seeding: {len(chains)} chains ({sum(1 for c in chains if c['done'])} finished), "
             f"{len(s['cards'])} cards, {len(s['openings'])} hooks"]
    ls = lines_of(s)
    if ls:
        total = sum(len(l["scenes"]) for l in ls)
        lines.append(f"The world: {len(open_lines(s))} running and {len(closed_lines(s))} ended storyline(s), {total} scene(s) written, "
                     f"{sum(1 for c in s['cards'].values() if c['in_play'])} card(s) in play, "
                     f"{len(pending_openings(s))} development(s) waiting")
        for l in ls:
            if l.get("status") == "closed":
                lines.append(f"  - {l['title']}: ENDED after {len(l['scenes'])} scene(s)")
            else:
                of = f" of {l['length']}" if l.get("length") else ""
                lines.append(f"  - {l['title']}: scene {len(l['scenes'])}{of}")
    else:
        lines.append("No storyline has started yet - the first begins with the first finished hook.")
    lines.append(f"Open tasks: {len(open_tasks(s))}")
    return lines
