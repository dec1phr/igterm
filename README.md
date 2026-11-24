# igterm

A WeeChat-style, text-only client for **Instagram Direct Messages**. Only the
Messages section exists. Reels, posts and stories people send you show up as
plain `https://www.instagram.com/...` links; nothing graphical is ever fetched
or drawn.

```
 alice
 1.igterm                │ ── Tue 24 Nov 2026 ──
 2.alice (2)             │ 11:16 alice │ hey! did you see this?
 3.bob                   │ 11:16 alice │ [reel] https://www.instagram.com/reel/C0ffee12345/
 4.Weekend plan          │ 11:19   you │ lol yes, that one is great
 [13:16] [you] 2:alice [Act: 4(1)]
 _
```

## Install (Fedora)

```sh
./install.sh          # creates a venv in ~/.local/share/igterm, installs instagrapi,
                      # puts `igterm` in ~/.local/bin
igterm --demo         # offline demo with fake chats, no account needed
igterm                # real login
```

Optional: `sudo dnf install xdg-utils wl-clipboard` for `/open` and `/copy`.

If `pip install instagrapi` fails on a very new default Python, run
`sudo dnf install python3.13` and re-run `./install.sh` (it prefers 3.13/3.12).

## Using it

First run asks for username, password and any 2FA / security code. Only the
session token is stored (`~/.config/igterm/session.json`, mode 600), never
your password. Later runs resume silently. `igterm --relogin` starts over.

| Keys | |
|---|---|
| Enter | send message |
| PgUp / PgDn | scroll (scrolling to the top loads older messages) |
| Ctrl-P / F5, Ctrl-N / F6 | previous / next buffer |
| Alt-1 … Alt-9, Alt-a | jump to buffer N / next buffer with unread |
| Ctrl-A/E, Ctrl-U/K/W, Up/Down | line editing and input history |

| Command | |
|---|---|
| `/go N`, `/next`, `/prev` | switch buffer (1 = system buffer) |
| `/dm USER [text]` | open a chat; with text, start a new conversation |
| `/more` | load older messages |
| `/reload` | check for messages now |
| `/links`, `/open [N]`, `/copy [N]` | list / open in browser / copy links in this buffer |
| `/markread`, `/clear`, `/help`, `/quit` | |

To send a message that starts with `/`, type `//`.

Options: `--interval SECONDS` (inbox check period, default 20, min 10),
`--bell`, `-u USERNAME`, `--relogin`.

## Behaviour worth knowing

* **No read receipts are sent.** Unread counts are tracked locally; the other
  person never sees "seen" because of igterm.
* Remote text is sanitised (control characters, bidi overrides removed) so a
  malicious message cannot inject terminal escape sequences.
* All network calls run on one background thread; the UI never freezes.
  Failures show in buffer 1 and polling backs off automatically.
* Only the regular inbox is shown (not message requests), and the newest
  ~15 messages per chat are fetched; older ones load on demand.

## Important caveats

* Instagram has **no official DM API**. This uses the unofficial private API via
  [instagrapi](https://github.com/subzeroid/instagrapi). That is against
  Instagram's Terms of Use, can trigger security checks or temporary blocks, and
  can break whenever Instagram changes things. Use an account you can afford to
  have challenged, keep the polling interval gentle, and do not run it from
  several places at once.
* The unit tests and UI test run against the built-in demo backend and
  fixtures; the live Instagram path could not be exercised without an account,
  so expect to report/fix small API-shape differences.

## Tests

```sh
python3 -I -m unittest discover -s tests -v
python3 -I tests/pty_smoke.py
```
