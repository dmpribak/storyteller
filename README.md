# Storybot - a shared world of parallel, ending storylines, for Discord

Players write everything: the seed answers, the follow-up questions, the cards, the hooks, the scenes,
the chapter recaps. The bot only deals out structure (who sees what, who goes next, when to inject
something unexpected, who to ping).

## Files

| File | What it is |
|---|---|
| `bot.py` | The Discord layer: slash commands, board buttons, private task views, pop-up forms, public posts, pings |
| `engine.py` | All game rules. Plain Python, no Discord imports, fully testable on its own |
| `test_engine.py` | Headless simulation of the whole game (`python test_engine.py`) |
| `test_migration.py` | Upgrades each save in `fixtures/` from the old format and plays on (`python test_migration.py`) |
| `test_notify.py` | The ping rules (`python test_notify.py`) |
| `fixtures/` | Real saves from the previous version (v1 separate threads, v2 one big story, v3 endless storylines), used by the migration tests |
| `requirements.txt`, `.env.example` | Dependencies and config template |

## Set up the bot

1. Go to https://discord.com/developers/applications, create an application, open **Bot** and reset/copy the token.
   No privileged intents are needed.
2. Open **OAuth2 > URL Generator**. Scopes: `bot` and `applications.commands`. Bot permissions:
   View Channels, Send Messages, Embed Links, Create Public Threads, Send Messages in Threads, Read Message History.
   Open the generated URL to invite the bot.
3. Install and configure (Python 3.10+):
   ```
   pip install -r requirements.txt
   cp .env.example .env      # then paste your token into .env
   python bot.py
   ```
   While developing, set `DEV_GUILD_ID` in `.env` to your test server's id so slash commands update instantly.
   Game data is saved as one JSON file per server in `data/` (change with `STORYBOT_DATA`).

## Start a game

An admin runs `/setup` (optionally picking the channel and tuning `hops`, `roots`, `handoffs`, `goal`,
`storylines`, `storyline_length`, `chapter_length`, `claim_hours`, `ping_cooldown`). The bot posts a pinned board message with
**My board / Status / How to play** buttons. Players join the first time they open their board.

## How play works

**Seeding** (runs in the background for the whole game). Players answer simple present-moment questions.
Each answer starts a chain that grows blind: one person writes a follow-up question seeing only the last
answer, the next person answers seeing only that question. A finished chain is revealed in the channel and
someone weaves it into a named card (character / place / thing-concept-situation). Two cards are then tied
together by a question and an answer, and someone writes a hook for the pairing.

**The world and its storylines.** All cards live in one shared world. Up to `storylines` (default 3) storylines
are open at once, each in its own Discord thread and each with exactly one "write the next scene" task open, so
several people can write at the same time. A storyline lasts `storyline_length` scenes (default 8; 0 means it
never ends) and then **ends**. The first finished hooks each start a storyline; when one ends, its slot is freed
and the next hook starts a fresh storyline. If every slot is taken, a new hook is queued as a **development** of
the running storyline with the least going on, bringing its two cards and hook into it.

**How storylines intersect** (while they run)
- Every storyline gets a **guaranteed crossover** around its midpoint: a card from another storyline walks in
  (cards from storylines still running are preferred, so it is a real intersection; ended ones are the fallback).
- Cards are shared: anything appended to a card shows up in every storyline that card is in.
- Each scene prompt carries a **"Meanwhile, elsewhere in the world"** digest of the other running storylines.
- Random wildcards add more: extra crossovers (also from ended storylines), an unplaced card from background
  seeding, or a word from the answer pool.

**How an ended storyline shapes the future**
- The last scene is told it is the finale. Then someone writes the **ending**: a title, a recap, and an
  **aftermath** ("what has changed in the world?").
- The aftermath is written into the log of the storyline's two founding cards. Any later storyline that uses
  those cards sees it, and so do the people editing them.
- Later scene prompts carry **"Echoes of storylines that already ended"**: endings that share cards with the
  storyline come first, then the most recent. A new storyline therefore starts inside a world that remembers.
- Characters and places from ended storylines can cross over into new ones.

Each scene prompt also puts a few cards **in the spotlight**, rotating through that storyline's whole cast. After a
scene you can add to cards, create a new card the scene invented, and (except on the finale) leave a hook for the
next writer. If `chapter_length` is shorter than `storyline_length`, chapter recaps are written along the way too;
the last one is the ending. `/story` lists the running storylines (with thread links) and the latest endings.

**Privacy.** Everything you pick and write stays private (ephemeral) until posted, so the blind steps stay
blind. Tasks you pick are held for you for `claim_hours`, then return to the board.

## Pings

The bot tells players when there is something new they can take.

- **Default is a DM.** If someone has DMs closed, the bot @mentions them in the game channel instead.
- `/notify` lets each player choose **DM**, **channel mention**, or **off** (and shows the current setting).
- **Batched:** each player is pinged at most once every `ping_cooldown` minutes (default 30). Tasks that
  appear in between are saved up and arrive in one message.
- **Relevant only:** you are only told about tasks you can actually take, and never about tasks your own
  action just created. A new player isn't pinged about the backlog that existed before they joined.
- **No spoilers:** a ping lists task *types* only ("Ask a follow-up question"), never content, so blind
  steps stay blind.
- Players who haven't opened their board for 30 days (`inactive_days`) are skipped until they come back.
- Only people who have opened the board at least once (and so joined) are ever pinged.

## Commands

`/setup` (admin) - `/board` - `/status` - `/story` - `/cards` - `/card <name>` - `/notify` - `/howto`

## Upgrading from earlier versions

Saves upgrade automatically the first time the bot starts, in a chain (v1 -> v2 -> v3 -> v4). Nothing is lost,
and the original file is first copied to `data/<server id>.v<old version>.backup.json`.

- **From v3 (endless parallel storylines):** each storyline finishes its current stretch: it ends at its next
  multiple of `storyline_length` scenes (a storyline at scene 27 with length 8 ends at scene 32). Then its ending
  recap is owed and the slot is reused.
- **From v2 (one big story):** the story becomes a storyline (same rule as above), keeping its thread. Hooks that
  were waiting start new storylines, up to `storylines`.
- **From v1 (separate threads):** each old thread becomes a storyline with its Discord thread. Threads that were
  resolved are ended storylines (their ending recap is owed, so each shows up on the board once). Running threads
  end at their original `thread_length`, which becomes `storyline_length`. Old thread records are kept in the
  save under `legacy_threads`.
- Cards, chains, links, hooks and card logs are untouched. Recaps owed for chapters already written appear on the board.
- Storylines that have no Discord thread yet get one when the bot starts.
- Old saves have no ping preferences yet; they are created on first use, so nobody gets a flood of pings.
- Running the upgrade again changes nothing.

## What to know

- The game logic is tested headlessly and the bot's handlers were exercised against fake Discord objects
  (including loading an old save, pings, the closed-DM fallback and `/notify`), but **it has not been run
  against live Discord yet**. Expect small discord.py details to need a fix on first contact (permissions,
  modal limits, thread creation). Pings are the most likely place for surprises: they rely on being able to
  DM server members and on the bot staying online, because they are sent by a loop that checks once a minute.
- "Fresh eyes" (weaving a chain, linking cards, writing a hook, recapping a chapter) is a preference, not a
  rule. Fresh players get first claim; if nobody is fresh (everyone has contributed) it opens to everyone at
  once, and otherwise after `fresh_grace_hours` (default 24) anyone can take it, so absent players can't stall
  the story. You still never answer your own linking question or follow your own piece in a chain. A task that
  opens up after the grace period appears on the board but doesn't trigger a new ping. With 1 player the blind
  rules can't hold, but the flow still runs for testing.
- No content filtering by design. Servers can use Discord's built-in AutoMod; there is no remove-entry admin
  command yet.
- Not built yet: flagging a card detail as disputed, an admin tool to edit or remove entries, scheduled daily
  check-ins, and reminders for tasks you've picked but not finished.
- Game numbers (injection odds, spotlight size, append slots, hand-off chance, development spacing) live in
  `engine.DEFAULTS` and `maybe_inject`.
