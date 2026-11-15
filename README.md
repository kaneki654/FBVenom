# FBVenom — the commenting tool

Sibling of **FBPoison**. Same engine, different job:

| | FBPoison | FBVenom |
|---|---|---|
| purpose | reactions | **comments** |
| modes | React / Comment / React+Comment | **Comment only** |
| AI endpoint | hard-coded in `core.py` | **`venom_ai.json`, editable in the UI** |
| button | INJECT | ENVENOM |

There is no mode selector: FBVenom only comments, so the strain is stated
("COMMENT ONLY") rather than chosen. The per-link reaction picker hides itself
for the same reason. If you want reactions, use FBPoison.

A phone whose comment toggle is **off** shows `MUTED` and sits the run out
entirely — it does not fall back to reacting the way FBPoison does.

They are fully independent. Changing FBVenom's endpoint does not touch
FBPoison, and neither imports the other.

The desktop uses a Venom-inspired black, white and crimson theme, with a
symbiote header, dedicated link workspace, device controls and session activity.
The **Diagnostics** panel shows actionable errors during a session; use **Copy**
to share the displayed report or **Clear** to reset the panel.

## Running

Local AI settings, saved workspaces, logs and build intermediates are ignored by Git. Configure AI from **AI SETTINGS**, or copy `venom_ai.example.json` to `venom_ai.json` and add your key locally. The verified Windows executable and its checksum/build metadata are versioned in `dist/`.

```bash
python fbvenom.py
```

Other modes:

```bash
python fbvenom.py --ai
```

Checks the AI endpoint without opening the GUI: prints the provider, whether a
key is set, and three sample comments. Use it before committing a long run.

```bash
python fbvenom.py --cli
```

## Comment to Comment workspaces

Open the **Comment to Comment** tab and click **+ New workspace**. Give it a
unique name and choose the number of devices to assign. Each workspace keeps
its own links, example comments, run speed, and screenshot setting. Use the
workspace selector to switch, **Edit** to rename or change the device count,
and **Delete** to remove a workspace and its saved draft.

Connected, unmuted devices are assigned automatically and are not shared
between named workspaces. Existing assignments are kept where possible when
you rescan; available devices fill any vacancies. The assigned serials and
requested count appear below the workspace selector. A session starts only
when the selected workspace has its full requested device count.

Workspaces save automatically in `venom_workspaces.json` beside the app and
return on the next launch. Sessions run one workspace at a time, using the
existing comment engine. Workspace and configuration controls stay locked
until the session finishes or stops. The original **Comments** tab keeps its
manual device selection and its own draft when switching tabs.

## Windows EXE build

Build on **64-bit Windows with Python 3.12**. The one-file build is defined in
`FBVenom.spec`. It embeds the
generated application icon, Android `adb.exe` plus its two Windows DLLs,
PyQt6, uiautomator2/adbutils, Pillow and the post-reading dependencies:

Download Google's Windows Android platform-tools and extract the
`platform-tools` directory beside `FBVenom.spec`, or set
`FBVENOM_PLATFORM_TOOLS` to its directory. Linux ADB cannot be bundled into a
Windows executable.

```powershell
python -m pip install -r requirements-build.txt
python -m unittest discover -v
python -m PyInstaller --clean --noconfirm FBVenom.spec
python tools_validate_windows_build.py dist/FBVenom.exe --source-commit (git rev-parse HEAD)
```

The **Build Windows executable** GitHub Actions workflow performs the same
build on a native Windows runner and saves a verified executable artifact.
Validation checks the Windows x64 GUI executable headers, frozen startup,
bundled ADB execution, icons, local settings paths and updated GUI features.
`dist/FBVenom.sha256` and `dist/FBVenom.build.json` record its checksum,
source commit, app version and Windows validation results. No license entry,
provider calls or phone posting is performed by the packaging smoke check.

The result is `dist/FBVenom.exe`. `venom_ai.json` deliberately is **not**
embedded into the executable because it contains a private API key and must
remain editable. Keep it beside the EXE for the current settings, or launch the
EXE without it and configure AI SETTINGS on first use. Never distribute your
personal `venom_ai.json` with a public copy of the EXE.

## AI settings

Click **⚗ AI SETTINGS** in the command bar. The caption beside it shows the
live provider and model, and turns amber and says `NO KEY` when comments would
silently fall back to the example text you typed instead of being generated.

Presets, all OpenAI-compatible chat-completions endpoints:

| provider | notes |
|---|---|
| Groq | default, fast, free tier |
| OpenAI | |
| OpenRouter | one key, many models |
| Together AI | |
| DeepSeek | |
| Local (Ollama / LM Studio) | no key needed, no rate limit |
| Custom endpoint | paste any compatible URL |

**TEST** saves the settings and then really calls the endpoint, so a green
result means comments will actually generate during a run.

## Sarcastic anger

Every other tone is one sentence of description, and the model picks its own
delivery — which is why they all come back sounding like the same careful
person. **Sarcastic anger** is sampled instead: each comment draws a point in a
style space, so nineteen accounts arrive as nineteen people.

| axis | values |
|---|---|
| register | galit · pang-asar (SANA ALL) · pagod · tawa · gulat · tanong · depensa · balewala |
| capitalisation | all_caps · caps_burst · lower_flat · lower_exhale_then_caps · sentence_sloppy |
| length | fragment (2–5) · one-liner (6–15) · short (16–30) · rant (30–60, one comma-splice chain) |
| laughter | none · hahaha · HAHAHA · HAHAHAHAHA · 🤣🤣🤣 · hays |
| punctuation | none · ellipsis · !!! · comma-splice |
| code-switch | tagalog-heavy · balanced · english-heavy · bisaya |
| comparison | ~68% measure the post against something small and personal |
| comparison move | mas_pa · parang · ako_nga · kahit · arithmetic · swap · timeline · receipt · question |

The axes are **coupled**, not rolled independently: register picks the caps
profile and the length, and those pick the laughter and the punctuation.
Resigned people type lowercase, so `pagod` never draws `all_caps`; `balewala`
is a two-word dismissal, so it never draws a rant.

The comparison anchor is the biggest quality lever, and it only works when the
yardstick is comparable **on the same dimension** as the complaint. The post is
read for a dimension first — money, speed, waiting, clarity, durability, size —
and the yardstick is drawn only from that dimension. A blurry photo gets held
against a 240p video or a one-bar video call; a cracked project gets held
against a 20-peso umbrella.

**A post with no readable dimension gets no comparison at all.** A wallpaper
post and a birthday photo have nothing a household yardstick can measure, and
forcing one produced `mas malabo pa sa sahod` and `mas malniaw pa lotto
jackpot` — a salary is not blurry and a jackpot is not clear, so the joke is a
category error and the comment reads as a canned line stapled to an unrelated
post.

**Nothing may be introduced that the post did not put there.** No brand, app,
company, team, place or person unless the post or your example comments
mention it — and **the government least of all**: no gobyerno, agency, mayor,
senator, official, buwis or corruption unless the post or the examples raised
it first. If the post is not about the government, the comment does not make
it about the government.

That is enforced, not merely asked for. `_GATED_CONCEPTS` lists each concept
with every surface form it takes **in both languages**, because the post
description is written in English while the comment is Taglish — matching bare
words would block `gobyerno` on a post that plainly says "government", and let
it through on a post that says nothing of the kind. A concept is unlocked when
the post or the examples use any of its forms, so:

| post | comment | |
|---|---|---|
| wallpaper | `kasalanan ng gobyerno to` | blocked |
| wallpaper | `sayang ang buwis natin` | blocked |
| 1.2B government project | `san napunta yung pera ng gobyerno` | allowed |
| 1.2B government project | `korap talaga ang mayor natin` | blocked — a mayor is not the government |
| wallpaper, examples say `buwis natin yan` | `sayang ang buwis natin` | allowed |

`_NAMED_PERSON_RE` separately catches a person invented after si/ni/kay, while
letting terms of address through (`si bes` fine, `si eilana` rejected). The
comparison yardstick is the one exception to all of this, and it is always the
commenter's own life (your sahod, your wifi, your load), never a named third
party.

Anchors **and** comparison moves are also rationed fleet-wide per run: no two
accounts under one post get the same yardstick or the same rhetorical move
while unused ones remain. Rationing the yardstick alone was not enough — nine
accounts picked nine different anchors and poured all of them into the same
`mas ___ pa sa ___` mould, which read as one comment posted nine times.

This tone needs **Read the post** on: it writes *about* the link. With reading
off it degrades to an ordinary tone description. Hard swearing is rejected in
`_HARD_PROFANITY_RE` — a pulled comment costs more than a retry — and that is
the only place it is decided.

## Facebook URL normalization

Every target is normalized once in the background before phone workers start,
so the entire fleet receives the same URL. The input may be a clean link or a
Facebook/Messenger sentence containing one. Mobile, `mbasic`, `web`, `touch`
and other Facebook hosts become `https://www.facebook.com`; tracking parameters
such as `mibextid`, `fbclid`, `ref`, `utm_*`, `__cft__` and `__tn__` are removed;
Facebook `l.php`/`share.php` wrappers are unwrapped; `/amp` and fragments are
removed. Non-Facebook URLs are rejected.

Supported target forms include `/live/<id>`, page `/live/<id>` and
`/live_videos/<id>`, `/watch/live?v=<id>`, ordinary videos, reels, photo
permalinks, numeric and `pfbid` posts, group posts and group
`?multi_permalinks=<id>` links. Native `fb://` and `facebook://` content links
receive web fallbacks when their content ID is recognized. Facebook sharer and
`facewebmodal` wrappers are unwrapped without discarding the content ID.

Opaque `/share/p`, `/share/r`, `/share/v`, `fb.watch` and `fb.me` links are
resolved once on the PC when Facebook exposes a canonical destination. If it
does not, the cleaned share link remains the safe fallback.
`fb.gg` gaming links are also resolved, and each phone gets an ordered,
deduplicated opener list appropriate to numeric posts, pfbid
posts, group posts, photos, videos and reels: native deep links where reliable,
clean HTTPS, then `facewebmodal`. An opener counts only after FB Lite is in the
foreground and the post view is verified, so an intent that merely lands on
the feed is rejected and the next variant is tried.

Normalization cannot grant access to private, deleted, expired or restricted
content, or enable comments where Facebook has disabled them. Profiles, feeds
and content collections do not identify a single comment target. If a short
link cannot be resolved, its cleaned original is retained and a warning explains
the fallback.

## Error diagnostics

Errors print with stable codes, a timestamp, the phone/account/link context
when available, and a suggested next step. They also appear in the desktop
Diagnostics panel and `venom_debug.log` beside the app, even when debug tracing
is disabled. The log rotates after 5 MB. Common credentials in structured error
details are redacted.

Examples include `DEVICE_UNAUTHORIZED`, `ADB_TIMEOUT`, `TARGET_OPEN_FAILED`,
`COMMENT_NO_BOX`, `COMMENT_NOT_TYPED`, `ACTION_BLOCKED` and
`COMMENT_SUBMIT_UNVERIFIED`. A comment is confirmed only after its draft clears
from an observable composer on the post. If a send was attempted but cannot be
verified, the session reports it as unverified and does not automatically resend
that account/link pair; inspect the post before retrying it manually.

Offline regression checks (no phones, provider calls or actual comments):

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -v
```

## Reading the post

**Read the post** (on by default) makes the comment about the link instead of
about your example. Before commenting, one phone sends the post's screen to the
AI, which reads the caption and looks at the picture; your example comments then
supply only the *voice* — the slang, the length, the way it is typed — aimed at
whatever is actually in the post. Facebook Lite puts none of its text in the
accessibility tree, so the caption is read from the screenshot.

For an ordinary post this costs **one call per link for the whole fleet**, not
one per account: the first phone to reach a link reads it and the rest reuse
that reading. A caption spanning multiple screens uses additional batched
vision reads, still once per link rather than once per phone. Turn reading off
and comments go back to being reworded copies of the example. Leave **Vision
model** blank unless the comment model cannot see images.

### Long captions

Collapsed captions are expanded before analysis. If Facebook Lite exposes its
`See more` / `Tingnan pa` control, it is tapped directly; if Lite draws it but
hides it from accessibility, vision locates that specific caption control and
taps it. The phone then captures overlapping screenfuls in order, up to
`max_caption_frames` (default `8`), and analyzes them as one continuous post.
It preserves names, organizations, dates, numbers, claims, reasons, the ask or
complaint, and the caption's final conclusion instead of summarizing only its
opening. One extra frame after the caption ends captures the attached picture.
The phone scrolls back to its original position before commenting.

Short captions do none of this extra work: they remain one frame and one
vision call. Increase `max_caption_frames` up to `12` only for exceptionally
long posts. If the caption is still incomplete at that ceiling, FBVenom skips
the comment for that link instead of guessing from the opening or falling back
to an unrelated example.

### If the comments are about the wrong thing

The screenshot is cropped to the top `post_crop` (default `0.62`) of the
screen, because below that line sit other people's comments and their little
round profile photos. Sending the whole screen produced a run of comments about
a *blurry profile picture, a Lakers jersey and Universal Studios* — the model
had described the commenters' avatars, and since the reading is cached per
link, all nine accounts wrote about it. The comment list also contains this
tool's own earlier comments, so the fleet was reading itself.

If it happens again: raise `post_crop` in `venom_ai.json` for pages with long
captions, lower it if readings keep picking up commenters. A reading that is
plainly about the app is now retried on a tighter crop and then refused
outright rather than cached — the log line is `[post-ctx] reading was about the
app`.

Settings live in `venom_ai.json` beside the program:

```json
{
  "provider": "groq",
  "url": "https://api.groq.com/openai/v1/chat/completions",
  "api_key": "...",
  "model": "llama-3.3-70b-versatile",
  "fallback_model": "llama-3.1-8b-instant",
  "min_gap": 2.5
}
```

`min_gap` is the pacing between AI calls, **shared by every device** — it is
the ceiling on comment throughput for the whole fleet. A local model can set it
to 0. To change it in code use `core.set_ai_gap(seconds)`; there is no
`AI_MIN_GAP` constant here, because a constant would look settable while the
real value lives in the JSON.

## What is shared with FBPoison

`core.py`, `gui.py` and `auth.py` were copied from FBPoison and carry every fix
made there: the UTF-8 ADB decode, the emoji-safe console, geometric account
switching for label-less builds, the dump `Snapshot`, the cached
`window_size`, the confirmed block detection, and the bounded account loop
(re-queue once, `MAX_ACCOUNTS` ceiling).

Fixes made in one are **not** automatically picked up by the other — they are
separate copies. If you fix a bug in the shared engine, apply it to both.
