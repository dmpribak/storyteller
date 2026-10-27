"""
bot.py - Discord front end for the collaborative storytelling game (discord.py 2.x).

How it feels in Discord
  * /setup posts a pinned "board" message in the game channel with buttons.
  * "My board" shows YOU the tasks you may take, privately (ephemeral messages), so
    the blind relay stays blind: each task only shows what that player may see.
  * Picking a task reserves it for you (claim_hours), shows its context, and opens a
    pop-up form (modal) for your writing.
  * Public posts: revealed chains, new cards, new developments, chapter recaps, and
    every scene. Several storylines run in parallel in one shared world, each in its own Discord thread;
    a storyline ends after a set number of scenes and its ending is carried into the ones after it.
  * After a scene you can add to cards, create a new card, and leave a hook.
  * New tasks ping the players who can take them (DM by default, or an @mention in the game
    channel, or off - see /notify). Pings are batched, so a burst of tasks is one message.

Run:  python bot.py        (needs DISCORD_TOKEN; see README.md)
"""
import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

import engine

log = logging.getLogger("storybot")
DATA_DIR = os.environ.get("STORYBOT_DATA", "data")
COLOR = 0x6C5CE7


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- storage --

class Store:
    """One JSON file per server, cached in memory, saved atomically."""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.games = {}
        self.locks = {}
        for fn in os.listdir(folder):
            if fn.endswith(".json") and fn[:-5].isdigit():
                gid = int(fn[:-5])
                path = os.path.join(folder, fn)
                with open(path) as fh:
                    state = json.load(fh)
                if engine.needs_migration(state):
                    backup = os.path.join(folder, f"{gid}.v{state.get('version', 1)}.backup.json")
                    if not os.path.exists(backup):
                        shutil.copyfile(path, backup)  # never lose the original save
                    engine.migrate(state)
                    self.games[gid] = state
                    self.save(gid)
                    log.info("Upgraded save for server %s (original kept as %s)", gid, backup)
                else:
                    engine.migrate(state)  # fills in any newer settings
                    self.games[gid] = state

    def get(self, gid):
        return self.games.get(gid)

    def put(self, gid, state):
        self.games[gid] = state
        self.save(gid)

    def save(self, gid):
        path = os.path.join(self.folder, f"{gid}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.games[gid], fh)
        os.replace(tmp, path)

    def lock(self, gid):
        if gid not in self.locks:
            self.locks[gid] = asyncio.Lock()
        return self.locks[gid]


# ---------------------------------------------------------------- helpers --

def lines_text(lines, limit=3800):
    out = []
    for line in lines:
        if isinstance(line, tuple):
            level, text = line
            out.append(text if level <= 0 else "  " * (level - 1) + "- " + text)
        else:
            out.append(line)
    text = "\n".join(out)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def task_embed(s, t):
    spec = engine.describe(s, t)
    e = discord.Embed(title=engine.LABELS[t["type"]], description=lines_text(spec["lines"]), color=COLOR)
    if spec["tip"]:
        e.add_field(name="What to do", value=spec["tip"][:1024], inline=False)
    return e


async def reply(interaction, content=None, **kw):
    """Send an ephemeral reply whether or not the interaction was already answered."""
    kw.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kw)
    else:
        await interaction.response.send_message(content, **kw)


HOWTO = (
    "**How it works**\n"
    "Everything in the story is written by players; the bot only deals out structure.\n\n"
    "**Seeding (always running in the background)**\n"
    "1. Answer a simple question about yourself, right now.\n"
    "2. Chains grow blind: one person asks a follow-up seeing only the last answer, the next "
    "answers seeing only that question. Nobody sees the whole chain until it's revealed.\n"
    "3. Someone weaves each revealed chain into a named card (a character, a place, or a "
    "thing/concept/situation).\n"
    "4. Two cards get linked by a question and an answer, then someone writes a hook for the pairing.\n\n"
    "**The story**\n"
    "There is one shared world and several storylines running in it at once, each in its own thread. "
    "A storyline lasts a set number of scenes (8 by default) and then ends; the next finished hook "
    "starts a fresh one. Cards belong to the world, so a change you make to a card shows up in every "
    "storyline it is in. Each storyline gets a crossover around its middle, when a character or place "
    "from another storyline walks in. Each scene puts a few cards in the spotlight, mentions what is "
    "happening elsewhere, and echoes storylines that already ended. After a scene you can add to "
    "cards, create new ones, and leave a hook. When a storyline ends, someone gives it a title, a recap "
    "and an **aftermath** (what is different in the world now); the aftermath is written into its "
    "founding cards, so later storylines inherit it. Now and then a wildcard drops in: a new card or "
    "a word to work in.\n\n"
    "**Using it**\n"
    "Press **My board** (or use /board) to see what you can take. Tasks you pick are held for you "
    "for a while, then released if you don't finish. I ping you when new tasks appear; use /notify "
    "to choose DM, an @mention in the channel, or nothing."
)


# ------------------------------------------------------------------ views --

class FormModal(discord.ui.Modal):
    """A pop-up form built from engine field specs."""

    def __init__(self, title, fields, on_values, prefill=None):
        super().__init__(title=title[:45])
        self.on_values = on_values
        self.inputs = {}
        for f in fields:
            maxlen = f["max"] or (3500 if f["long"] else 200)
            default = (prefill or {}).get(f["key"], f["default"]) or None
            if default:
                default = default[:maxlen]
            ti = discord.ui.TextInput(
                label=f["label"][:45],
                style=discord.TextStyle.paragraph if f["long"] else discord.TextStyle.short,
                required=f["required"], default=default, max_length=maxlen)
            self.add_item(ti)
            self.inputs[f["key"]] = ti

    async def on_submit(self, interaction):
        await self.on_values(interaction, {k: (ti.value or "").strip() for k, ti in self.inputs.items()})

    async def on_error(self, interaction, error):
        log.exception("modal error", exc_info=error)
        await reply(interaction, "Something went wrong saving that. Please try again.")


class TaskSelect(discord.ui.Select):
    def __init__(self, bot, options):
        super().__init__(placeholder="Pick a task…", options=options, min_values=1, max_values=1)
        self.bot = bot

    async def callback(self, interaction):
        await self.bot.open_task(interaction, int(self.values[0]))


class TaskSelectView(discord.ui.View):
    def __init__(self, bot, options):
        super().__init__(timeout=900)
        self.add_item(TaskSelect(bot, options))


class TaskActionView(discord.ui.View):
    def __init__(self, bot, task_id):
        super().__init__(timeout=1800)
        self.bot, self.task_id = bot, task_id

    @discord.ui.button(label="Write it", style=discord.ButtonStyle.primary)
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.open_form(interaction, self.task_id)

    @discord.ui.button(label="Put it back", style=discord.ButtonStyle.secondary)
    async def put_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.release(interaction)


class RetryView(discord.ui.View):
    """Shown when a field fails validation."""

    def __init__(self, bot, task_id, values, soft):
        super().__init__(timeout=900)
        self.bot, self.task_id, self.values = bot, task_id, values
        if not soft:
            self.remove_item(self.keep)

    @discord.ui.button(label="Edit it", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.open_form(interaction, self.task_id, prefill=self.values)

    @discord.ui.button(label="Keep as written", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.submit(interaction, self.task_id, self.values, force=True)


class CardSelect(discord.ui.Select):
    def __init__(self, bot, scene_id, options):
        super().__init__(placeholder="Which card?", options=options, min_values=1, max_values=1)
        self.bot, self.scene_id = bot, scene_id

    async def callback(self, interaction):
        await self.bot.open_append_form(interaction, self.scene_id, int(self.values[0]))


class CardSelectView(discord.ui.View):
    def __init__(self, bot, scene_id, options):
        super().__init__(timeout=900)
        self.add_item(CardSelect(bot, scene_id, options))


class SceneFollowupView(discord.ui.View):
    """Optional steps after writing a scene."""

    def __init__(self, bot, scene_id, ended=False):
        super().__init__(timeout=1800)
        self.bot, self.scene_id = bot, scene_id
        if ended:   # an ended storyline has no next writer to leave a hook for
            self.remove_item(self.hook)

    @discord.ui.button(label="Add to a card", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.pick_card_to_append(interaction, self.scene_id)

    @discord.ui.button(label="New card", style=discord.ButtonStyle.secondary)
    async def new_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.open_promote_form(interaction, self.scene_id)

    @discord.ui.button(label="Leave a hook", style=discord.ButtonStyle.secondary)
    async def hook(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.open_hook_form(interaction, self.scene_id)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="All set - thanks for writing!", view=self)
        self.stop()


class BoardView(discord.ui.View):
    """The persistent buttons on the pinned board message."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="My board", style=discord.ButtonStyle.primary, custom_id="storybot:board")
    async def board(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.show_board(interaction)

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, custom_id="storybot:status")
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.show_status(interaction)

    @discord.ui.button(label="How to play", style=discord.ButtonStyle.secondary, custom_id="storybot:howto")
    async def howto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(interaction, HOWTO)


# -------------------------------------------------------------------- bot --

class StoryBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = Store(DATA_DIR)

    async def setup_hook(self):
        self.add_view(BoardView(self))
        register_commands(self)
        dev = os.environ.get("DEV_GUILD_ID")
        if dev:  # instant command updates while developing
            guild = discord.Object(id=int(dev))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.maintenance.start()
        self.notifier.start()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        for gid in list(self.store.games):
            try:
                s = self.store.get(gid)
                for line in engine.open_lines(s or {}):
                    if not line.get("discord_thread"):
                        await self.ensure_line_thread(gid, line["id"])
            except Exception:
                log.exception("could not create storyline threads for %s", gid)

    @tasks.loop(minutes=15)
    async def maintenance(self):
        """Release claims nobody finished."""
        for gid in list(self.store.games):
            async with self.store.lock(gid):
                s = self.store.get(gid)
                if s and engine.release_expired(s):
                    self.store.save(gid)

    @tasks.loop(minutes=1)
    async def notifier(self):
        try:
            await self.run_notifications()
        except Exception:
            log.exception("notifier failed")

    @notifier.before_loop
    async def _notifier_ready(self):
        await self.wait_until_ready()

    async def run_notifications(self):
        """Ping players about tasks they can take that appeared since they last looked."""
        for gid in list(self.store.games):
            dms, channel_pings = [], []
            async with self.store.lock(gid):
                s = self.store.get(gid)
                if not s or not s.get("channel_id"):
                    continue
                engine.release_expired(s)
                for uid, new in engine.due_notifications(s).items():
                    mode = engine.prefs(s, uid)["notify"]
                    (dms if mode == "dm" else channel_pings).append((uid, new))
                    engine.mark_notified(s, uid)  # mark first so a failed send is never retried in a loop
                if dms or channel_pings:
                    self.store.save(gid)
                board_url = (f"https://discord.com/channels/{gid}/{s['channel_id']}/{s['board_message_id']}"
                             if s.get("board_message_id") else None)
            guild = self.get_guild(gid)
            guild_name = guild.name if guild else "the story server"
            for uid, new in dms:
                text = (f"**New on the Story Board in {guild_name}:** {engine.notify_summary(new)}.\n"
                        + (f"Open the board: {board_url}\n" if board_url else "")
                        + "(Change how I ping you with /notify.)")
                try:
                    user = await self.fetch_user(int(uid))
                    await user.send(text)
                except (discord.Forbidden, discord.HTTPException):
                    channel_pings.append((uid, new))  # DMs closed: fall back to a mention in the channel
                except Exception:
                    log.exception("could not DM %s", uid)
            if channel_pings:
                seen, merged = set(), []
                for _, new in channel_pings:
                    for t in new:
                        if t["id"] not in seen:
                            seen.add(t["id"])
                            merged.append(t)
                mentions = " ".join(f"<@{uid}>" for uid, _ in channel_pings)
                channel = guild.get_channel(self.store.get(gid)["channel_id"]) if guild else None
                if channel:
                    try:
                        await channel.send(f"{mentions} New on the board: {engine.notify_summary(merged)}. "
                                           "Press **My board** to take one. (Use /notify to change this.)",
                                           allowed_mentions=discord.AllowedMentions(
                                               users=True, roles=False, everyone=False))
                    except Exception:
                        log.exception("could not post channel ping")

    # ---- guards ----------------------------------------------------------

    async def need_game(self, interaction):
        """Returns the state (call inside the guild lock), or replies and returns None."""
        if interaction.guild_id is None:
            await reply(interaction, "Please use this inside a server.")
            return None
        s = self.store.get(interaction.guild_id)
        if s is None:
            await reply(interaction, "No game is running here yet. A server admin can start one with /setup.")
        return s

    # ---- board and tasks ---------------------------------------------------

    async def show_board(self, interaction):
        gid = interaction.guild_id
        uid = str(interaction.user.id)
        async with self.store.lock(gid or 0):
            s = await self.need_game(interaction)
            if s is None:
                return
            engine.join(s, uid, interaction.user.display_name)
            engine.touch(s, uid)
            engine.release_expired(s)
            mine = engine.my_claim(s, uid)
            ts = sorted(engine.visible_tasks(s, uid), key=lambda t: (t is not mine, t["id"]))
            options = []
            for t in ts[:25]:
                label = engine.clip(engine.LABELS[t["type"]], 100)
                desc = engine.preview(s, t) + (" (yours)" if mine and t["id"] == mine["id"] else "")
                options.append(discord.SelectOption(label=label, description=engine.clip(desc, 100),
                                                    value=str(t["id"])))
            total = len(ts)
            self.store.save(gid)
        if not options:
            await reply(interaction, "Nothing for you right now - check back soon, or ask in the channel. "
                                     "New tasks appear as people write.")
            return
        extra = f" (showing 25 of {total})" if total > 25 else ""
        await reply(interaction, f"**Your board** - {total} task(s) available{extra}.",
                    view=TaskSelectView(self, options))

    async def show_status(self, interaction):
        async with self.store.lock(interaction.guild_id or 0):
            s = await self.need_game(interaction)
            if s is None:
                return
            text = "\n".join(engine.status_lines(s))
        await reply(interaction, text)

    async def open_task(self, interaction, task_id):
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with self.store.lock(gid):
            s = self.store.get(gid)
            t, err = engine.claim_task(s, uid, task_id)
            if err:
                await reply(interaction, err)
                return
            embed = task_embed(s, t)
            self.store.save(gid)
        await reply(interaction, embed=embed, view=TaskActionView(self, task_id))

    async def release(self, interaction):
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with self.store.lock(gid):
            s = self.store.get(gid)
            n = engine.release_claim(s, uid)
            self.store.save(gid)
        await reply(interaction, "Put back on the board." if n else "You weren't holding anything.")

    async def open_form(self, interaction, task_id, prefill=None):
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with self.store.lock(gid):
            s = self.store.get(gid)
            t = engine.find_task(s, task_id)
            if not t or not t["open"]:
                await reply(interaction, "That task has already been completed.")
                return
            if engine.claimed_by(s, t) != uid:  # claim lapsed - try to take it again
                t, err = engine.claim_task(s, uid, task_id)
                if err:
                    await reply(interaction, err)
                    return
                self.store.save(gid)
            spec = engine.describe(s, t)
            title = engine.LABELS[t["type"]]
        modal = FormModal(title, spec["fields"], lambda i, v: self.submit(i, task_id, v), prefill=prefill)
        await interaction.response.send_modal(modal)

    async def submit(self, interaction, task_id, values, force=False):
        await interaction.response.defer(ephemeral=True)
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with self.store.lock(gid):
            s = self.store.get(gid)
            t = engine.find_task(s, task_id)
            if not t or not t["open"]:
                await interaction.followup.send("That task was already completed by someone else.", ephemeral=True)
                return
            holder = engine.claimed_by(s, t)
            if (holder and holder != uid) or uid not in engine.eligible(s, t):
                await interaction.followup.send("That task is no longer yours to complete.", ephemeral=True)
                return
            spec = engine.describe(s, t)
            problem = engine.validate(spec, values)
            if problem and not (force and problem[2]):
                key, msg, soft = problem
                await interaction.followup.send(msg, view=RetryView(self, task_id, values, soft), ephemeral=True)
                return
            events = engine.perform(s, uid, t, values)
            self.store.save(gid)
        scene_id = next((e["scene"] for e in events if e["type"] == "scene"), None)
        ended = any(e["type"] == "storyline_end" for e in events)
        await self.publish(gid, events)
        if scene_id and ended:
            await interaction.followup.send(
                "Final scene posted - that ends the storyline! Someone will write its ending recap. "
                "You can still add to the cards or create a new one.",
                view=SceneFollowupView(self, scene_id, ended=True), ephemeral=True)
        elif scene_id:
            await interaction.followup.send(
                "Scene posted! Optionally add to the cards, create a new one, or leave a hook "
                "for the next writer.", view=SceneFollowupView(self, scene_id), ephemeral=True)
        else:
            await interaction.followup.send("Saved - thank you! (Press **My board** for the next one.)",
                                            ephemeral=True)

    # ---- after-scene follow-ups ----------------------------------------------

    async def pick_card_to_append(self, interaction, scene_id):
        gid = interaction.guild_id
        async with self.store.lock(gid):
            s = self.store.get(gid)
            options = []
            for cid in engine.scene_cards(s, scene_id):
                c = engine.get_card(s, cid)
                options.append(discord.SelectOption(label=engine.clip(c["name"], 100),
                                                    description=engine.KIND_LABEL[c["kind"]], value=str(cid)))
        await reply(interaction, "Which card did this scene change?", view=CardSelectView(self, scene_id, options))

    async def open_append_form(self, interaction, scene_id, card_id):
        gid = interaction.guild_id
        async with self.store.lock(gid):
            name = engine.get_card(self.store.get(gid), card_id)["name"]
        fields = [engine.F("text", "What changed? (a fact, secret, shift)", long=True, max=500),
                  engine.F("state", "Current state (optional)", required=False, max=200)]

        async def on_values(i, v):
            await self.do_append(i, scene_id, card_id, v)
        await interaction.response.send_modal(FormModal(f"Add to {name}", fields, on_values))

    async def do_append(self, interaction, scene_id, card_id, values):
        await interaction.response.defer(ephemeral=True)
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with self.store.lock(gid):
            s = self.store.get(gid)
            try:
                engine.append_to_card(s, uid, scene_id, card_id, values["text"], values.get("state") or None)
            except ValueError as err:
                await interaction.followup.send(str(err), ephemeral=True)
                return
            self.store.save(gid)
            name = engine.get_card(s, card_id)["name"]
            line_id = engine.get_scene(s, scene_id)["line"]
        await self.note_in_story(gid, f"**{name}** - {values['text']}", line_id)
        await interaction.followup.send(f"Added to {name}.", ephemeral=True)

    async def open_promote_form(self, interaction, scene_id):
        fields = [engine.F("name", "Name (short title)", max=60),
                  engine.F("text", "Description (1-2 sentences)", long=True, max=600),
                  engine.F("kind", "Kind: c, p or t", "kind", default="c", max=1)]

        async def on_values(i, v):
            await self.do_promote(i, scene_id, v)
        await interaction.response.send_modal(FormModal("New card from this scene", fields, on_values))

    async def do_promote(self, interaction, scene_id, values):
        await interaction.response.defer(ephemeral=True)
        gid, uid = interaction.guild_id, str(interaction.user.id)
        bad = engine.check_kind(values["kind"] or "")
        if bad:
            await interaction.followup.send(bad, ephemeral=True)
            return
        async with self.store.lock(gid):
            s = self.store.get(gid)
            try:
                card, events = engine.promote_card(s, uid, scene_id, values["name"], values["text"], values["kind"])
            except ValueError as err:
                await interaction.followup.send(str(err), ephemeral=True)
                return
            self.store.save(gid)
        await self.publish(gid, events)
        await interaction.followup.send(f"New card created: {card['name']}.", ephemeral=True)

    async def open_hook_form(self, interaction, scene_id):
        fields = [engine.F("text", "A question or twist for the next writer", long=True, max=400)]

        async def on_values(i, v):
            await self.do_hook(i, scene_id, v)
        await interaction.response.send_modal(FormModal("Leave a hook", fields, on_values))

    async def do_hook(self, interaction, scene_id, values):
        await interaction.response.defer(ephemeral=True)
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with self.store.lock(gid):
            s = self.store.get(gid)
            try:
                engine.set_hook(s, uid, scene_id, values["text"])
            except ValueError as err:
                await interaction.followup.send(str(err), ephemeral=True)
                return
            self.store.save(gid)
            who = engine.name_of(s, uid)
            line_id = engine.get_scene(s, scene_id)["line"]
        await self.note_in_story(gid, f"**Hook from {who}:** {values['text']}", line_id)
        await interaction.followup.send("Hook saved for the next writer.", ephemeral=True)

    # ---- publishing to the channel ----------------------------------------------

    async def story_channel(self, gid, line_id=None):
        """A storyline's Discord thread (or the game channel if it has none / line_id is None)."""
        guild = self.get_guild(gid)
        s = self.store.get(gid)
        if not guild or not s:
            return None
        dt = None
        if line_id is not None:
            line = s.get("storylines", {}).get(str(line_id))
            dt = line.get("discord_thread") if line else None
        if dt:
            found = guild.get_thread(dt)
            if found is None:
                try:
                    found = await guild.fetch_channel(dt)
                except discord.HTTPException:
                    found = None
            if found:
                return found
        return guild.get_channel(s["channel_id"]) if s["channel_id"] else None

    async def ensure_line_thread(self, gid, line_id, opening_msg=None):
        """Give a storyline a Discord thread if it has none (new lines, or lines made by a save upgrade)."""
        s = self.store.get(gid)
        guild = self.get_guild(gid)
        if not s or not guild or not s["channel_id"]:
            return None
        line = s["storylines"].get(str(line_id))
        if not line:
            return None
        if line.get("discord_thread"):
            existing = await self.story_channel(gid, line_id)
            if existing is not None and getattr(existing, "id", None) == line["discord_thread"]:
                return existing
        channel = guild.get_channel(s["channel_id"])
        if channel is None:
            return None
        name = f"Storyline: {line['title']}"[:95]
        if opening_msg is None:
            opening_msg = await channel.send(f"A storyline continues: **{line['title']}**")
        dt = await opening_msg.create_thread(name=name, auto_archive_duration=10080)
        async with self.store.lock(gid):
            self.store.get(gid)["storylines"][str(line_id)]["discord_thread"] = dt.id
            self.store.save(gid)
        return dt

    async def note_in_story(self, gid, text, line_id=None):
        try:
            target = await self.story_channel(gid, line_id)
            if target:
                await target.send(text[:1900])
        except Exception:
            log.exception("could not post note")

    async def publish(self, gid, events):
        for ev in events:
            try:
                await self.publish_one(gid, ev)
            except Exception:
                log.exception("could not publish event %s", ev)

    async def publish_one(self, gid, ev):
        s = self.store.get(gid)
        channel = await self.story_channel(gid)
        if channel is None:
            return
        ty = ev["type"]
        if ty == "reveal":
            c = engine.get_chain(s, ev["chain"])
            e = discord.Embed(title=f"Chain #{c['id']} revealed",
                              description=lines_text(engine.chain_lines(s, c, 1)), color=COLOR)
            e.set_footer(text="Someone will weave this into a card - check the board.")
            await channel.send(embed=e)
        elif ty == "card":
            c = engine.get_card(s, ev["card"])
            e = discord.Embed(title=f"New card: {c['name']}", description=c["text"], color=COLOR)
            e.set_footer(text=engine.KIND_LABEL[c["kind"]])
            await channel.send(embed=e)
        elif ty == "crossing":
            l = engine.get_link(s, ev["link"])
            a, b = engine.get_card(s, l["a"]), engine.get_card(s, l["b"])
            await channel.send(f"Two cards are about to meet: **{a['name']}** and **{b['name']}**. "
                               "A linking question is waiting on the board.")
        elif ty in ("storyline_start", "story_start"):
            o = engine.get_opening(s, ev["opening"])
            line = engine.get_line(s, ev["line"])
            e = discord.Embed(title=f"A new storyline begins: {line['title']}",
                              description=lines_text(engine.opening_lines(s, o)), color=COLOR)
            msg = await channel.send(embed=e)
            dt = await self.ensure_line_thread(gid, ev["line"], msg)
            if dt:
                await dt.send("The first scene is waiting on the board - press **My board** on the pinned "
                              "message, or use /board.")
        elif ty == "opening_queued":
            o = engine.get_opening(s, ev["opening"])
            line = engine.get_line(s, ev["line"])
            e = discord.Embed(title=f"A new development is coming to: {line['title']}",
                              description=lines_text(engine.opening_lines(s, o)), color=COLOR)
            e.set_footer(text="It will be woven into that storyline's next scene.")
            await channel.send(embed=e)
        elif ty == "beat":
            o = engine.get_opening(s, ev["opening"])
            e = discord.Embed(title="A new development",
                              description=lines_text(engine.opening_lines(s, o)), color=COLOR)
            e.set_footer(text="It will be woven into the next scene.")
            await (await self.story_channel(gid, ev["line"])).send(embed=e)
        elif ty == "scene":
            sc = engine.get_scene(s, ev["scene"])
            e = discord.Embed(description=sc["text"], color=COLOR)
            line = engine.get_line(s, ev["line"])
            where = (f"Scene {sc['number']} of {line['length']}" if line.get("length")
                     else f"Chapter {sc['chapter']} - scene {sc['number']}")
            if line.get("length") and sc["number"] == line["length"]:
                where += " (final)"
            e.set_author(name=f"{where} - {engine.name_of(s, sc['author'])}")
            inj = sc.get("injection")
            if inj and inj["kind"] == "beat":
                l = engine.get_link(s, engine.get_opening(s, inj["opening"])["link"])
                names = f"{engine.get_card(s, l['a'])['name']} & {engine.get_card(s, l['b'])['name']}"
                e.set_footer(text=f"New development woven in: {names}")
            elif inj and inj["kind"] == "arrival":
                e.set_footer(text=f"Wildcard: {engine.get_card(s, inj['card'])['name']} entered the story")
            elif inj and inj["kind"] == "crossover":
                other = s["storylines"].get(str(inj.get("from")))
                where = f" from \"{other['title']}\"" if other else ""
                e.set_footer(text=f"Crossover: {engine.get_card(s, inj['card'])['name']} arrives{where}")
            elif inj and inj["kind"] == "word":
                e.set_footer(text=f"Wildcard word: {inj['text']}")
            await (await self.story_channel(gid, ev["line"])).send(embed=e)
        elif ty == "chapter_end":
            await (await self.story_channel(gid, ev["line"])).send(
                f"**Chapter {ev['chapter']} is complete.** A recap is waiting on the board.")
        elif ty == "storyline_end":
            line = engine.get_line(s, ev["line"])
            await (await self.story_channel(gid, ev["line"])).send(
                f"**This storyline has ended.** An ending recap and aftermath are waiting on the board.")
            await channel.send(f"**{line['title']}** has reached its end. A new storyline can take its place "
                               "as soon as a hook is ready.")
        elif ty == "recap":
            line = engine.get_line(s, ev["line"])
            r = next(x for x in line["recaps"] if x["chapter"] == ev["chapter"])
            if r.get("ending"):
                e = discord.Embed(title=f"The end: {r['title']}", description=r["text"], color=COLOR)
                e.add_field(name="Aftermath - what has changed in the world", value=r["aftermath"][:1000], inline=False)
            else:
                e = discord.Embed(title=f"Chapter {r['chapter']}: {r['title']}", description=r["text"], color=COLOR)
            e.set_footer(text=f"Recap by {engine.name_of(s, r['author'])}")
            await (await self.story_channel(gid, ev["line"])).send(embed=e)
        elif ty == "seeding_done":
            await channel.send("The opening goal has been reached. The world is underway, and new seed "
                               "questions keep feeding in fresh cards and developments.")


# --------------------------------------------------------------- commands --

def register_commands(bot):
    tree = bot.tree

    @tree.command(name="setup", description="Start a story game in this server (admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Where the game lives (default: this channel)",
                           hops="Question/answer pairs per seed chain",
                           roots="Seed questions to start with",
                           handoffs="Chains seeded from another chain's last answer",
                           goal="Hooks written before start-up seeding counts as done",
                           storylines="How many storylines run at once in the shared world",
                           storyline_length="Scenes before a storyline ends (0 = never ends)",
                           chapter_length="Scenes per chapter",
                           claim_hours="How long a picked task is held for you",
                           ping_cooldown="Minutes between pings for each player (new tasks are batched)",
                           reset="Discard an existing game here")
    async def setup_cmd(interaction: discord.Interaction,
                        channel: Optional[discord.TextChannel] = None,
                        hops: app_commands.Range[int, 1, 4] = 2,
                        roots: app_commands.Range[int, 2, 7] = 3,
                        handoffs: app_commands.Range[int, 0, 3] = 1,
                        goal: app_commands.Range[int, 1, 5] = 2,
                        storylines: app_commands.Range[int, 1, 6] = 3,
                        storyline_length: app_commands.Range[int, 0, 40] = 8,
                        chapter_length: app_commands.Range[int, 3, 20] = 8,
                        claim_hours: app_commands.Range[int, 1, 168] = 24,
                        ping_cooldown: app_commands.Range[int, 1, 1440] = 30,
                        reset: bool = False):
        if not interaction.user.guild_permissions.manage_guild:
            await reply(interaction, "You need the Manage Server permission to do that.")
            return
        await interaction.response.defer(ephemeral=True)
        channel = channel or interaction.channel
        gid = interaction.guild_id
        async with bot.store.lock(gid):
            if bot.store.get(gid) and not reset:
                await interaction.followup.send("A game already exists here. Run /setup again with "
                                                "reset: True to start over.", ephemeral=True)
                return
            s = engine.new_game(channel.id, hops=hops, roots=roots, handoffs=handoffs, goal=goal,
                                chapter_length=chapter_length, max_storylines=storylines, storyline_length=storyline_length, claim_hours=claim_hours,
                                notify_cooldown_min=ping_cooldown)
            intro = discord.Embed(
                title="The Story Board",
                description="We're writing a world together - everything in it comes from us.\n\n"
                            "Press **My board** to see what you can write right now. Everything you "
                            "pick is private until you post it.", color=COLOR)
            try:
                msg = await channel.send(embed=intro, view=BoardView(bot))
            except discord.Forbidden:
                await interaction.followup.send("I can't post in that channel. Give me Send Messages, "
                                                "Embed Links, Create Public Threads and Send Messages in "
                                                "Threads there.", ephemeral=True)
                return
            s["board_message_id"] = msg.id
            bot.store.put(gid, s)
        await interaction.followup.send(f"Game started in {channel.mention}. Players join automatically "
                                        "the first time they open their board.", ephemeral=True)

    @tree.command(name="board", description="See the tasks you can take right now")
    @app_commands.guild_only()
    async def board_cmd(interaction: discord.Interaction):
        await bot.show_board(interaction)

    @tree.command(name="status", description="How the game is going")
    @app_commands.guild_only()
    async def status_cmd(interaction: discord.Interaction):
        await bot.show_status(interaction)

    @tree.command(name="notify", description="Choose how I ping you about new tasks")
    @app_commands.guild_only()
    @app_commands.describe(mode="How to ping you (leave empty to see your current setting)")
    @app_commands.choices(mode=[app_commands.Choice(name="DM me", value="dm"),
                                app_commands.Choice(name="@mention me in the game channel", value="channel"),
                                app_commands.Choice(name="Don't ping me", value="off")])
    async def notify_cmd(interaction: discord.Interaction, mode: Optional[str] = None):
        gid, uid = interaction.guild_id, str(interaction.user.id)
        async with bot.store.lock(gid or 0):
            s = await bot.need_game(interaction)
            if s is None:
                return
            engine.join(s, uid, interaction.user.display_name)
            engine.touch(s, uid)
            if mode:
                engine.set_notify(s, uid, mode)
                bot.store.save(gid)
            current = engine.prefs(s, uid)["notify"]
            cooldown = s["settings"]["notify_cooldown_min"]
        words = {"dm": "I'll DM you", "channel": "I'll @mention you in the game channel", "off": "I won't ping you"}
        await reply(interaction, f"{words[current]} when new tasks you can take appear "
                                 f"(at most once every {cooldown} minutes, batched). "
                                 "Open DMs from server members if you want DMs; otherwise I'll mention you in the channel.")

    @tree.command(name="howto", description="How the game works")
    async def howto_cmd(interaction: discord.Interaction):
        await reply(interaction, HOWTO)

    @tree.command(name="cards", description="List every card in the world")
    @app_commands.guild_only()
    async def cards_cmd(interaction: discord.Interaction):
        async with bot.store.lock(interaction.guild_id):
            s = await bot.need_game(interaction)
            if s is None:
                return
            rows = []
            for c in s["cards"].values():
                state = f" - {c['state']}" if c["state"] else ""
                mark = "" if c["in_play"] else " (unplaced)"
                rows.append(f"**{c['name']}** ({engine.KIND_LABEL[c['kind']]}){mark}{state}")
        text = "\n".join(rows) or "No cards yet."
        await reply(interaction, text[:1990])

    @tree.command(name="card", description="Show one card in full")
    @app_commands.guild_only()
    @app_commands.describe(name="The card's name")
    async def card_cmd(interaction: discord.Interaction, name: str):
        async with bot.store.lock(interaction.guild_id):
            s = await bot.need_game(interaction)
            if s is None:
                return
            c = engine.find_card(s, name)
            if c is None:
                await reply(interaction, "I couldn't find exactly one card with that name. Try /cards.")
                return
            lines = engine.card_context(s, c, "Card", log_n=10)
            e = discord.Embed(title=c["name"], description=lines_text(lines), color=COLOR)
        await reply(interaction, embed=e)

    @card_cmd.autocomplete("name")
    async def card_autocomplete(interaction: discord.Interaction, current: str):
        s = bot.store.get(interaction.guild_id)
        if not s:
            return []
        current = current.lower()
        names = [c["name"] for c in s["cards"].values() if current in c["name"].lower()]
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names[:25]]

    @tree.command(name="story", description="Where the storylines stand")
    @app_commands.guild_only()
    async def story_cmd(interaction: discord.Interaction):
        async with bot.store.lock(interaction.guild_id):
            s = await bot.need_game(interaction)
            if s is None:
                return
            running, ended = engine.open_lines(s), engine.closed_lines(s)
            if not running and not ended:
                await reply(interaction, "No storyline has started yet. The first begins with the first finished hook.")
                return
            out = [f"**{len(running)} running, {len(ended)} ended** - a world of {len(s['cards'])} card(s); "
                   f"{len(engine.pending_openings(s))} hook(s) not yet placed."]
            for l in running:
                n = len(l["scenes"])
                of = f" of {l['length']}" if l.get("length") else ""
                row = (f"\n**{l['title']}** - scene {n}{of}, {len(l['cards'])} card(s), "
                       f"{len(engine.pending_openings(s, l))} development(s) waiting")
                if l.get("discord_thread"):
                    row += f"\nhttps://discord.com/channels/{interaction.guild_id}/{l['discord_thread']}"
                out.append(row)
            if ended:
                out.append("\n**Ended storylines**")
                for l in ended[-6:]:
                    e = engine.ending_of(l)
                    if e:
                        out.append(f"- *{e['title']}* ({l['title']}): {engine.clip(e['text'], 120)} "
                                   f"Aftermath: {engine.clip(e['aftermath'], 120)}")
                    else:
                        out.append(f"- {l['title']} (ending recap still to be written)")
        await reply(interaction, "\n".join(out)[:1990])


def main():
    load_dotenv()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("Set DISCORD_TOKEN (in the environment or a .env file). See README.md.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    StoryBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
