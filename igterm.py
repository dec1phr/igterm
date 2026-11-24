#!/usr/bin/env python3
"""igterm - a WeeChat-style, text-only Instagram Direct Messages client.

Only the Messages (DM) section is supported.  Reels, posts and stories that
people send you are shown as plain links.  No images are ever rendered.

Instagram has no official DM API; this program uses the unofficial private API
through the `instagrapi` package.  Use at your own risk (see README.md).
"""
from __future__ import annotations

import argparse
import curses
import getpass
import json
import locale
import logging
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

__version__ = "1.0.0"
APP = "igterm"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP
SESSION_FILE = CONFIG_DIR / "session.json"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "igterm.log"

CORE_ID = "__core__"
URL_RE = re.compile(r"https?://[^\s<>\"']+")


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def clean(s) -> str:
    """Make remote text safe for a terminal (no escape sequences, no bidi tricks)."""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    out = []
    for ch in s:
        if ch == "\n":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc":
            out.append("?")
        elif cat in ("Cf", "Cs", "Zl", "Zp"):
            continue  # zero-width / bidi / surrogate / line separators: drop
        else:
            out.append(ch)
    return "".join(out)


def cw(ch: str) -> int:
    """Display width of one character."""
    if unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def swidth(s: str) -> int:
    return sum(cw(c) for c in s)


def clip(s: str, width: int) -> str:
    """Cut s so its display width is <= width."""
    if width <= 0:
        return ""
    out, w = [], 0
    for ch in s:
        c = cw(ch)
        if w + c > width:
            break
        out.append(ch)
        w += c
    return "".join(out)


def wrap(text: str, width: int) -> list[str]:
    """Word-wrap text to a display width. Always returns at least one line."""
    width = max(1, width)
    out: list[str] = []
    for para in text.split("\n"):
        line, lw = "", 0
        for tok in re.findall(r"\s+|\S+", para):
            if tok.isspace():
                if line and lw + len(tok) <= width:
                    line += tok
                    lw += len(tok)
                continue
            tw = swidth(tok)
            if lw + tw <= width:
                line += tok
                lw += tw
                continue
            if line:
                out.append(line.rstrip())
                line, lw = "", 0
            if tw <= width:
                line, lw = tok, tw
            else:  # hard-split an over-long word
                for ch in tok:
                    c = cw(ch)
                    if lw + c > width:
                        out.append(line)
                        line, lw = "", 0
                    line += ch
                    lw += c
        out.append(line.rstrip())
    return out or [""]


def norm_ig_url(u: str) -> str | None:
    """Return a tidy instagram.com URL (query/fragment dropped) or None."""
    try:
        p = urlsplit(u.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or host not in (
        "instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am"
    ):
        return None
    return urlunsplit(("https", host, p.path, "", ""))


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Msg:
    id: str
    ts: float
    user_id: str
    text: str


@dataclass
class Thread:
    id: str
    title: str
    users: dict = field(default_factory=dict)  # user_id -> username
    is_group: bool = False
    last_activity: float = 0.0
    msgs: dict = field(default_factory=dict)  # msg id -> Msg
    oldest_cursor: str | None = None
    has_older: bool = False
    seen_ts: float = 0.0
    scroll: int = 0
    cleared_before: float = 0.0
    loading: bool = False


# --------------------------------------------------------------------------
# Parsing of Instagram's raw DM JSON  (pure functions -> unit-testable)
# --------------------------------------------------------------------------

ITEM_LABELS = {
    "clip": "reel", "xma_clip": "reel", "xma_reel_share": "reel",
    "felix_share": "video", "media_share": "post", "xma_media_share": "post",
    "story_share": "story", "xma_story_share": "story", "reel_share": "story reply",
    "voice_media": "voice message", "raven_media": "disappearing photo/video",
    "visual_media": "disappearing photo/video", "media": "photo/video",
    "animated_media": "GIF", "profile": "profile", "location": "location",
    "hashtag": "hashtag", "generic_xma": "shared content", "xma_link": "link",
    "video_call_event": "call", "placeholder": "unavailable message",
    "action_log": "event", "live_viewer_invite": "live invite",
    "avatar_effect_share": "effect", "ephemeral_media": "disappearing media",
}
_CODE_RE = re.compile(r"[A-Za-z0-9_-]{5,20}")


def find_link(o, depth: int = 0) -> str | None:
    """Dig through a shared-item dict for an instagram.com link."""
    if depth > 7:
        return None
    if isinstance(o, dict):
        for k in ("target_url", "link_url", "url", "permalink", "share_url"):
            v = o.get(k)
            if isinstance(v, str):
                u = norm_ig_url(v)
                if u:
                    return u
        code, pt = o.get("code"), o.get("product_type")
        if isinstance(code, str) and _CODE_RE.fullmatch(code) and pt:
            kind = "reel" if pt in ("clip", "reels") else "p"
            return f"https://www.instagram.com/{kind}/{code}/"
        for v in o.values():
            if isinstance(v, (dict, list)):
                r = find_link(v, depth + 1)
                if r:
                    return r
    elif isinstance(o, list):
        for v in o:
            r = find_link(v, depth + 1)
            if r:
                return r
    return None


def describe_item(it: dict) -> str:
    t = it.get("item_type") or ""
    txt = it.get("text") if isinstance(it.get("text"), str) else ""
    if t == "text":
        body = txt
    elif t == "like":
        body = "<3"
    elif t == "link":
        link = it.get("link") if isinstance(it.get("link"), dict) else {}
        body = link.get("text") or txt or ""
        url = (link.get("link_context") or {}).get("link_url") if isinstance(
            link.get("link_context"), dict) else None
        if url and url not in body:
            body = f"{body} {url}".strip()
    else:
        label = ITEM_LABELS.get(t, t.replace("_", " ") or "message")
        extra = ""
        if t == "reel_share" and isinstance(it.get("reel_share"), dict):
            extra = it["reel_share"].get("text") or ""
        elif t == "action_log" and isinstance(it.get("action_log"), dict):
            extra = it["action_log"].get("description") or ""
        elif t == "placeholder" and isinstance(it.get("placeholder"), dict):
            extra = it["placeholder"].get("message") or ""
        elif t == "profile" and isinstance(it.get("profile"), dict):
            un = it["profile"].get("username")
            if isinstance(un, str) and re.fullmatch(r"[A-Za-z0-9._]{1,30}", un):
                extra = f"https://www.instagram.com/{un}/"
        elif t in ("voice_media", "raven_media", "visual_media", "media", "animated_media"):
            extra = ""
        url = None if t in ("voice_media", "raven_media", "visual_media", "media") else find_link(it)
        parts = [f"[{label}]"]
        if url and url not in extra:
            parts.append(url)
        if extra:
            parts.append(extra)
        if txt and txt not in extra:
            parts.append(txt)
        body = " ".join(parts)
    rep = it.get("replied_to_message")
    if isinstance(rep, dict) and isinstance(rep.get("text"), str) and rep["text"]:
        snippet = rep["text"].replace("\n", " ")
        if len(snippet) > 40:
            snippet = snippet[:37] + "..."
        body = f'(re: "{snippet}") {body}'
    return body


def parse_item(it: dict) -> Msg | None:
    iid = it.get("item_id")
    if iid is None:
        return None
    try:
        ts = int(it.get("timestamp") or 0) / 1_000_000
    except (TypeError, ValueError):
        ts = 0.0
    return Msg(str(iid), ts, str(it.get("user_id", "")), clean(describe_item(it)))


def parse_thread(t: dict) -> Thread | None:
    tid = t.get("thread_id")
    if tid is None:
        return None
    users = {}
    for u in t.get("users") or []:
        pk = u.get("pk") if u.get("pk") is not None else u.get("pk_id")
        if pk is not None and u.get("username"):
            users[str(pk)] = clean(u["username"])
    is_group = bool(t.get("is_group"))
    names = list(users.values())
    if is_group:
        title = clean(t.get("thread_title") or ", ".join(names) or "group")
    else:
        title = names[0] if names else clean(t.get("thread_title") or "unknown")
    try:
        last = int(t.get("last_activity_at") or 0) / 1_000_000
    except (TypeError, ValueError):
        last = 0.0
    th = Thread(str(tid), title, users, is_group, last)
    for it in t.get("items") or []:
        m = parse_item(it)
        if m:
            th.msgs[m.id] = m
    th.oldest_cursor = t.get("oldest_cursor")
    th.has_older = bool(t.get("has_older"))
    return th


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class InstaBackend:
    """Talks to Instagram through instagrapi (imported lazily)."""

    def __init__(self):
        self.cl = None
        self.me_id = ""
        self.me_name = ""

    # -- login happens on the plain terminal, before curses starts ----------
    def login(self, username: str | None, relogin: bool = False) -> None:
        try:
            from instagrapi import Client  # noqa
        except ImportError:
            sys.exit(
                "The 'instagrapi' package is not installed.\n"
                "Run ./install.sh, or:  pip install instagrapi   (inside a venv)\n"
                "To try the interface without an account:  igterm --demo"
            )
        logging.disable(logging.CRITICAL)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        cl = Client()
        cl.delay_range = [1, 3]
        cl.challenge_code_handler = lambda user, choice: input(
            f"Instagram sent a security code via {choice}. Enter it: ").strip()
        self.cl = cl

        if relogin and SESSION_FILE.exists():
            SESSION_FILE.unlink()

        if SESSION_FILE.exists():
            try:
                cl.load_settings(str(SESSION_FILE))
                acct = cl.account_info()
                self.me_id = str(cl.user_id)
                self.me_name = clean(acct.username)
                print(f"Resumed saved session as @{self.me_name}")
                return
            except Exception as e:  # noqa: BLE001
                print(f"Saved session no longer valid ({type(e).__name__}); logging in again.")
                cl = Client()
                cl.delay_range = [1, 3]
                cl.challenge_code_handler = self.cl.challenge_code_handler
                self.cl = cl

        username = username or input("Instagram username: ").strip()
        password = os.environ.get("IGTERM_PASSWORD") or getpass.getpass("Password: ")
        try:
            cl.login(username, password)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "TwoFactorRequired":
                code = input("Two-factor code: ").strip().replace(" ", "")
                cl.login(username, password, verification_code=code)
            else:
                raise
        old = os.umask(0o077)
        try:
            cl.dump_settings(str(SESSION_FILE))
        finally:
            os.umask(old)
        os.chmod(SESSION_FILE, 0o600)
        self.me_id = str(cl.user_id)
        self.me_name = clean(getattr(cl, "username", None) or username)
        print(f"Logged in as @{self.me_name}")

    # -- operations used by the UI worker thread -----------------------------
    def inbox(self) -> list[Thread]:
        data = self.cl.private_request("direct_v2/inbox/", params={
            "visual_message_return_type": "unseen",
            "thread_message_limit": "15",
            "persistentBadging": "true",
            "limit": "25",
            "is_prefetching": "false",
            "selected_filter": "",
            "sort_order": "relevance",
        })
        threads = ((data or {}).get("inbox") or {}).get("threads") or []
        out = []
        for t in threads:
            th = parse_thread(t)
            if th:
                out.append(th)
        return out

    def older(self, tid: str, cursor: str | None):
        if not cursor:
            return [], None, False
        data = self.cl.private_request(f"direct_v2/threads/{tid}/", params={
            "visual_message_return_type": "unseen",
            "direction": "older",
            "seq_id": "40065",
            "limit": "30",
            "cursor": cursor,
        })
        t = (data or {}).get("thread") or {}
        msgs = [m for m in (parse_item(i) for i in t.get("items") or []) if m]
        return msgs, t.get("oldest_cursor"), bool(t.get("has_older"))

    def send(self, tid: str, text: str):
        m = self.cl.direct_send(text, thread_ids=[int(tid)])
        try:
            return Msg(str(m.id), m.timestamp.timestamp(), self.me_id, clean(text))
        except Exception:  # noqa: BLE001
            return None  # UI will pick the message up on the next poll

    def start_dm(self, username: str, text: str) -> None:
        uid = self.cl.user_id_from_username(username)
        self.cl.direct_send(text, user_ids=[int(uid)])


class DemoBackend:
    """Offline fake used by --demo (and by the automated tests)."""

    me_id = "1"
    me_name = "you"

    def __init__(self):
        now = time.time()
        self._n = 100
        self.threads = {}
        self._add("t1", "alice", {"2": "alice"}, False, [
            ("2", now - 7200, "hey! did you see this?"),
            ("2", now - 7190, "[reel] https://www.instagram.com/reel/C0ffee12345/"),
            ("1", now - 7000, "lol yes, that one is great"),
        ])
        self._add("t2", "bob", {"3": "bob"}, False, [
            ("3", now - 90000, "are we still on for friday?"),
            ("1", now - 89000, "yes, 7pm"),
        ])
        self._add("t3", "Weekend plan", {"2": "alice", "3": "bob", "4": "carol"}, True, [
            ("4", now - 3000, "who is bringing snacks"),
            ("3", now - 2900, "[post] https://www.instagram.com/p/Bx12345abcd/ look at this place"),
            ("2", now - 2800, "a very long message " * 8),
        ])
        self.pending = []

    def _add(self, tid, title, users, group, items):
        th = Thread(tid, title, users, group, items[-1][1])
        for uid, ts, text in items:
            self._n += 1
            th.msgs[str(self._n)] = Msg(str(self._n), ts, uid, text)
        self.threads[tid] = th

    def inbox(self):
        for tid, text in self.pending:
            th = self.threads[tid]
            uid = next(iter(th.users))
            self._n += 1
            th.msgs[str(self._n)] = Msg(str(self._n), time.time(), uid, f"echo: {text}")
            th.last_activity = time.time()
        self.pending = []
        return [Thread(t.id, t.title, dict(t.users), t.is_group, t.last_activity,
                       dict(t.msgs), None, False) for t in self.threads.values()]

    def older(self, tid, cursor):
        return [], None, False

    def send(self, tid, text):
        self._n += 1
        m = Msg(str(self._n), time.time(), self.me_id, text)
        self.threads[tid].msgs[m.id] = m
        self.pending.append((tid, text))
        return m

    def start_dm(self, username, text):
        tid = f"t-{username}"
        if tid not in self.threads:
            self._add(tid, username, {"9" + username: username}, False,
                      [(self.me_id, time.time(), text)])
        else:
            self.send(tid, text)


# --------------------------------------------------------------------------
# Background worker: all network calls are serialised on one thread
# --------------------------------------------------------------------------

class Worker(threading.Thread):
    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.jobs: queue.Queue = queue.Queue()
        self.events = events

    def submit(self, name, ctx, fn, *args):
        self.jobs.put((name, ctx, fn, args))

    def run(self):
        while True:
            name, ctx, fn, args = self.jobs.get()
            try:
                res = fn(*args)
                self.events.put(("ok", name, ctx, res))
            except BaseException as e:  # noqa: BLE001
                self.events.put(("err", name, ctx, e))


# --------------------------------------------------------------------------
# The curses application
# --------------------------------------------------------------------------

HELP = """\
Keys:  Enter send | PgUp/PgDn scroll | Ctrl-P / F5 previous buffer | Ctrl-N / F6 next buffer
       Alt-1..9 jump to buffer | Alt-a next buffer with unread | Ctrl-L redraw
       Ctrl-A/E home/end | Ctrl-U/K/W kill line | Up/Down input history
Commands:
  /go N            switch to buffer N (buffer 1 is this system buffer)
  /next, /prev     switch buffers
  /dm USER [text]  open a chat with USER; with text, starts a new conversation
  /more            load older messages of this chat
  /reload          check for new messages now
  /links           list all links in this buffer
  /open [N]        open link N (default: the last one) with xdg-open
  /copy [N]        copy link N (default: the last one) to the clipboard
  /markread        mark this buffer as read
  /clear           clear this buffer's display
  /quit            exit            (send a message starting with / by typing //)"""


class App:
    def __init__(self, stdscr, backend, args):
        self.scr = stdscr
        self.be = backend
        self.args = args
        self.events: queue.Queue = queue.Queue()
        self.worker = Worker(self.events)
        self.worker.start()
        self.threads: dict[str, Thread] = {}
        self.order: list[str] = [CORE_ID]
        self.threads[CORE_ID] = Thread(CORE_ID, "igterm")
        self.cur_id = CORE_ID
        self.inp: list[str] = []
        self.pos = 0
        self.hist: list[str] = []
        self.hist_i = 0
        self.status = ""
        self.quit = False
        self.dirty = True
        self.sys_n = 0
        self.polled_once = False
        self.poll_inflight = False
        self.next_poll = 0.0
        self.backoff = 0.0
        self.poll_enabled = True
        self.pending_goto: str | None = None
        self.last_minute = ""
        self.last_total = 0
        self.last_rows = 1
        self.fresh_state = not STATE_FILE.exists()
        self.seen_state = self._load_state()
        self._init_curses()
        self.sysmsg(self.core, f"igterm {__version__} - type /help for help. "
                    "Link-only mode: reels/posts show as URLs, nothing is downloaded.")

    # ---- setup / state ----------------------------------------------------
    @property
    def core(self) -> Thread:
        return self.threads[CORE_ID]

    def _init_curses(self):
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)
        self.attrs = {"text": 0, "dim": curses.A_DIM, "time": curses.A_DIM,
                      "sep": curses.A_DIM, "bar": curses.A_REVERSE,
                      "sys": curses.A_DIM, "me": curses.A_BOLD,
                      "unread": curses.A_BOLD, "cur": curses.A_REVERSE,
                      "link": curses.A_UNDERLINE}
        for i in range(6):
            self.attrs[f"nick:{i}"] = curses.A_BOLD
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            cols = [curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                    curses.COLOR_MAGENTA, curses.COLOR_BLUE, curses.COLOR_RED]
            for i, c in enumerate(cols):
                curses.init_pair(i + 1, c, bg)
                self.attrs[f"nick:{i}"] = curses.color_pair(i + 1) | curses.A_BOLD
            self.attrs["me"] = curses.color_pair(2) | curses.A_BOLD
            self.attrs["unread"] = curses.color_pair(3) | curses.A_BOLD

    def _load_state(self) -> dict:
        try:
            return {str(k): float(v) for k, v in
                    json.loads(STATE_FILE.read_text()).get("seen", {}).items()}
        except Exception:  # noqa: BLE001
            return {}

    def save_state(self):
        if getattr(self.args, "demo", False):
            return
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            seen = {t.id: t.seen_ts for t in self.threads.values() if t.id != CORE_ID}
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seen": seen}))
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    # ---- helpers ----------------------------------------------------------
    def cur(self) -> Thread:
        return self.threads[self.cur_id]

    def idx(self, tid: str) -> int:
        return self.order.index(tid) + 1

    def sysmsg(self, th: Thread, text: str):
        for line in clean(text).split("\n"):
            self.sys_n += 1
            th.msgs[f"sys-{self.sys_n:09d}"] = Msg(f"sys-{self.sys_n:09d}", time.time(), "*", line)
        self.dirty = True

    def nick(self, th: Thread, m: Msg) -> str:
        if m.user_id == "*":
            return "--"
        if m.user_id == self.be.me_id:
            return self.be.me_name or "me"
        return th.users.get(m.user_id) or self._global_name(m.user_id)

    def _global_name(self, uid: str) -> str:
        for t in self.threads.values():
            if uid in t.users:
                return t.users[uid]
        return uid[-6:] if uid else "?"

    def unread(self, th: Thread) -> int:
        core = th.id == CORE_ID
        return sum(1 for m in th.msgs.values()
                   if m.user_id != self.be.me_id and m.ts > th.seen_ts
                   and (core or m.user_id != "*"))

    def mark_read(self, th: Thread):
        core = th.id == CORE_ID
        latest = max((m.ts for m in th.msgs.values() if core or m.user_id != "*"),
                     default=0.0)
        if latest > th.seen_ts:
            th.seen_ts = latest
            self.dirty = True

    def switch(self, tid: str):
        if tid in self.threads and tid != self.cur_id:
            self.mark_read(self.cur())
            self.cur_id = tid
            self.cur().scroll = 0
            self.mark_read(self.cur())
            self.save_state()
            self.dirty = True

    def switch_rel(self, d: int):
        i = self.order.index(self.cur_id)
        self.switch(self.order[(i + d) % len(self.order)])

    def switch_unread(self):
        n = len(self.order)
        i = self.order.index(self.cur_id)
        for k in range(1, n + 1):
            tid = self.order[(i + k) % n]
            if self.unread(self.threads[tid]):
                self.switch(tid)
                return

    def links(self, th: Thread) -> list[str]:
        out = []
        for m in sorted(th.msgs.values(), key=lambda m: (m.ts, m.id)):
            if m.ts <= th.cleared_before:
                continue
            for u in URL_RE.findall(m.text):
                out.append(u.rstrip(".,;:!?)]"))
        return out

    # ---- network scheduling ---------------------------------------------------
    def maybe_poll(self):
        if not self.poll_enabled or self.poll_inflight or time.time() < self.next_poll:
            return
        self.poll_inflight = True
        self.worker.submit("poll", None, self.be.inbox)

    def schedule_next_poll(self, error: bool):
        base = float(self.args.interval)
        if error:
            self.backoff = min(300.0, max(base * 2, self.backoff * 2))
            delay = self.backoff
        else:
            self.backoff = 0.0
            delay = base * random.uniform(0.85, 1.25)
        self.next_poll = time.time() + delay

    def drain_events(self):
        while True:
            try:
                kind, name, ctx, payload = self.events.get_nowait()
            except queue.Empty:
                return
            self.dirty = True
            if name == "poll":
                self.poll_inflight = False
                if kind == "ok":
                    self.on_poll(payload)
                    self.schedule_next_poll(False)
                    if self.status.startswith(("poll failed", "refreshing", "message sent")):
                        self.status = ""
                else:
                    self.on_error("poll failed", payload)
            elif name == "send":
                if kind == "ok":
                    if self.status == "sending...":
                        self.status = ""
                    th = self.threads.get(ctx)
                    if th is not None and payload is not None:
                        th.msgs[payload.id] = payload
                        th.last_activity = max(th.last_activity, payload.ts)
                    self.next_poll = 0.0 if payload is None else self.next_poll
                else:
                    self.on_error("send failed", payload, ctx)
            elif name == "more":
                th = self.threads.get(ctx)
                if th is not None:
                    th.loading = False
                    if kind == "ok":
                        msgs, cursor, has_older = payload
                        for m in msgs:
                            th.msgs.setdefault(m.id, m)
                        th.oldest_cursor, th.has_older = cursor, has_older
                        self.status = f"loaded {len(msgs)} older messages"
                        if not msgs:
                            th.has_older = False
                    else:
                        self.on_error("loading older messages failed", payload, ctx)
            elif name == "dm":
                if kind == "ok":
                    self.status = "message sent; refreshing"
                    self.pending_goto = ctx
                    self.next_poll = 0.0
                else:
                    self.on_error("/dm failed", payload)

    def on_error(self, what: str, exc, ctx=None):
        ename = type(exc).__name__
        msg = clean(str(exc)).replace("\n", " ")[:300]
        self.sysmsg(self.core, f"{what}: {ename}: {msg}")
        self.status = f"{what} (see buffer 1)"
        if what == "poll failed":
            self.poll_inflight = False
            if ename in ("LoginRequired", "ClientLoginRequired", "ChallengeRequired",
                         "ReloginAttemptExceeded"):
                self.poll_enabled = False
                self.sysmsg(self.core, "Your Instagram session is no longer valid. Quit and "
                            "start igterm with --relogin to sign in again.")
            else:
                self.schedule_next_poll(True)

    def on_poll(self, threads: list[Thread]):
        first = not self.polled_once
        self.polled_once = True
        for td in threads:
            th = self.threads.get(td.id)
            if th is None:
                th = td
                if td.id in self.seen_state:
                    th.seen_ts = self.seen_state[td.id]
                elif self.fresh_state and first:
                    th.seen_ts = max((m.ts for m in th.msgs.values()), default=0.0)
                self.threads[th.id] = th
                self.order.append(th.id)
                if not first and self.args.bell and th.id != self.cur_id:
                    self.beep()
            else:
                new_from_others = [m for i, m in td.msgs.items() if i not in th.msgs
                                   and m.user_id != self.be.me_id]
                for i, m in td.msgs.items():
                    th.msgs.setdefault(i, m)
                th.title = td.title or th.title
                th.users.update(td.users)
                th.is_group = td.is_group
                th.last_activity = max(th.last_activity, td.last_activity)
                if th.oldest_cursor is None and not th.loading:
                    th.oldest_cursor, th.has_older = td.oldest_cursor, td.has_older
                if new_from_others and not first and self.args.bell and th.id != self.cur_id:
                    self.beep()
        if self.pending_goto:
            want = self.pending_goto.lower()
            self.pending_goto = None
            for t in self.threads.values():
                if not t.is_group and any(u.lower() == want for u in t.users.values()):
                    self.switch(t.id)
                    break

    def beep(self):
        try:
            curses.beep()
        except curses.error:
            pass

    # ---- drawing ----------------------------------------------------------
    def put(self, y: int, x: int, s: str, attr: int = 0, maxw: int | None = None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        limit = (w - x) if maxw is None else min(maxw, w - x)
        s = clip(s, limit)
        if not s:
            return
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def put_links(self, y, x, s, attr, maxw):
        pos = 0
        for m in URL_RE.finditer(s):
            self.put(y, x, s[pos:m.start()], attr, maxw)
            x += swidth(s[pos:m.start()]); maxw -= swidth(s[pos:m.start()])
            self.put(y, x, m.group(), attr | self.attrs["link"], maxw)
            x += swidth(m.group()); maxw -= swidth(m.group())
            pos = m.end()
        self.put(y, x, s[pos:], attr, maxw)

    def nick_attr(self, th: Thread, m: Msg) -> int:
        if m.user_id == "*":
            return self.attrs["sys"]
        if m.user_id == self.be.me_id:
            return self.attrs["me"]
        h = sum(ord(c) for c in self.nick(th, m)) % 6
        return self.attrs[f"nick:{h}"]

    def render(self, th: Thread, width: int):
        """Return list of (kind, [(text, attr)...]) display lines for a buffer."""
        msgs = sorted((m for m in th.msgs.values() if m.ts > th.cleared_before),
                      key=lambda m: (m.ts, m.id))
        nickw = max(4, min(16, max((swidth(self.nick(th, m)) for m in msgs), default=4)))
        prefix = 6 + nickw + 3
        tw = max(8, width - prefix)
        lines, lastday = [], None
        for m in msgs:
            d = datetime.fromtimestamp(m.ts)
            if d.date() != lastday:
                lastday = d.date()
                lines.append([(f"── {d:%a %d %b %Y} ──", self.attrs["sep"])])
            n = clip(self.nick(th, m), nickw)
            n = " " * (nickw - swidth(n)) + n
            tattr = self.attrs["sys"] if m.user_id == "*" else self.attrs["text"]
            for i, part in enumerate(wrap(m.text, tw)):
                if i == 0:
                    lines.append([(f"{d:%H:%M} ", self.attrs["time"]),
                                  (n, self.nick_attr(th, m)),
                                  (" │ ", self.attrs["dim"]), (part, tattr)])
                else:
                    lines.append([(" " * (6 + nickw) + " │ ", self.attrs["dim"]),
                                  (part, tattr)])
        return lines

    def draw(self):
        scr = self.scr
        h, w = scr.getmaxyx()
        scr.erase()
        if h < 6 or w < 30:
            self.put(0, 0, "terminal too small")
            scr.refresh()
            return
        th = self.cur()
        sbw = 0 if w < 70 else min(28, w // 4)
        mx = sbw + 2 if sbw else 0
        mw = w - mx

        # title bar
        if th.id == CORE_ID:
            title = " igterm - system messages and help"
        else:
            who = ", ".join(th.users.values())
            title = f" {th.title}" + (f" - {len(th.users) + 1} members: {who}" if th.is_group else "")
        self.put(0, 0, " " * w, self.attrs["bar"])
        self.put(0, 0, title, self.attrs["bar"], w)

        # sidebar
        if sbw:
            rows = h - 3
            n = len(self.order)
            first = 0
            ci = self.order.index(self.cur_id)
            if n > rows:
                first = min(max(0, ci - rows // 2), n - rows)
            for r in range(min(rows, n)):
                tid = self.order[first + r]
                t = self.threads[tid]
                u = self.unread(t)
                label = f"{first + r + 1:>2}.{t.title}"
                suffix = f" ({u})" if u else ""
                label = clip(label, sbw - swidth(suffix)) + suffix
                if tid == self.cur_id:
                    a = self.attrs["cur"]
                elif u:
                    a = self.attrs["unread"]
                else:
                    a = 0
                self.put(1 + r, 0, label.ljust(sbw) if tid == self.cur_id else label, a, sbw)
            for r in range(1, h - 2):
                try:
                    scr.addch(r, sbw, curses.ACS_VLINE, self.attrs["dim"])
                except curses.error:
                    pass

        # messages (bottom aligned)
        rows = h - 3
        lines = self.render(th, mw)
        total = len(lines)
        self.last_total, self.last_rows = total, rows
        maxscroll = max(0, total - rows)
        th.scroll = min(max(0, th.scroll), maxscroll)
        end = total - th.scroll
        vis = lines[max(0, end - rows):end]
        y = 1 + rows - len(vis)
        for segs in vis:
            x = mx
            for text, attr in segs:
                self.put_links(y, x, text, attr, mw - (x - mx))
                x += swidth(text)
            y += 1

        # status bar
        now = datetime.now().strftime("%H:%M")
        self.last_minute = now
        act = []
        for tid in self.order:
            if tid == self.cur_id:
                continue
            u = self.unread(self.threads[tid])
            if u:
                act.append(f"{self.idx(tid)}({u})")
        left = f" [{now}] [{self.be.me_name}] {self.idx(th.id)}:{th.title}"
        if th.scroll:
            left += f" -MORE({th.scroll})-"
        if th.loading:
            left += " [loading...]"
        if act:
            left += f" [Act: {' '.join(act)}]"
        if self.status:
            left += f" | {self.status}"
        self.put(h - 2, 0, " " * w, self.attrs["bar"])
        self.put(h - 2, 0, left, self.attrs["bar"], w)

        # input line
        text = "".join(self.inp)
        start = 0
        while start < self.pos and swidth(text[start:self.pos]) >= w - 1:
            start += 1
        self.put(h - 1, 0, text[start:], 0, w - 1)
        cx = min(w - 1, swidth(text[start:self.pos]))
        try:
            scr.move(h - 1, cx)
        except curses.error:
            pass
        scr.refresh()
        self.dirty = False

    # ---- input ------------------------------------------------------------
    def read_key(self):
        try:
            return self.scr.get_wch()
        except curses.error:
            return None

    def handle_key(self, k):
        th = self.cur()
        if isinstance(k, int):
            if k == curses.KEY_RESIZE:
                self.scr.clear()
            elif k in (curses.KEY_BACKSPACE,):
                self.backspace()
            elif k == curses.KEY_DC:
                if self.pos < len(self.inp):
                    del self.inp[self.pos]
            elif k == curses.KEY_LEFT:
                self.pos = max(0, self.pos - 1)
            elif k == curses.KEY_RIGHT:
                self.pos = min(len(self.inp), self.pos + 1)
            elif k == curses.KEY_HOME:
                self.pos = 0
            elif k == curses.KEY_END:
                self.pos = len(self.inp)
            elif k == curses.KEY_UP:
                self.history(-1)
            elif k == curses.KEY_DOWN:
                self.history(1)
            elif k == curses.KEY_PPAGE:
                th.scroll += max(1, self.last_rows - 2)
                self.maybe_load_older(th)
            elif k == curses.KEY_NPAGE:
                th.scroll = max(0, th.scroll - max(1, self.last_rows - 2))
            elif k == curses.KEY_F5:
                self.switch_rel(-1)
            elif k == curses.KEY_F6:
                self.switch_rel(1)
            elif k in (curses.KEY_ENTER,):
                self.submit()
            return
        # str
        if k in ("\n", "\r"):
            self.submit()
        elif k in ("\x7f", "\b"):
            self.backspace()
        elif k == "\x01":
            self.pos = 0
        elif k == "\x05":
            self.pos = len(self.inp)
        elif k == "\x0b":
            del self.inp[self.pos:]
        elif k == "\x15":
            del self.inp[:self.pos]
            self.pos = 0
        elif k == "\x17":
            i = self.pos
            while i > 0 and self.inp[i - 1] == " ":
                i -= 1
            while i > 0 and self.inp[i - 1] != " ":
                i -= 1
            del self.inp[i:self.pos]
            self.pos = i
        elif k == "\x04":
            if self.pos < len(self.inp):
                del self.inp[self.pos]
        elif k == "\x0c":
            self.scr.clear()
        elif k == "\x10":
            self.switch_rel(-1)
        elif k == "\x0e":
            self.switch_rel(1)
        elif k == "\x1b":
            self.scr.timeout(40)
            try:
                k2 = self.read_key()
            finally:
                self.scr.timeout(100)
            if isinstance(k2, str):
                if k2.isdigit() and k2 != "0":
                    n = int(k2)
                    if n <= len(self.order):
                        self.switch(self.order[n - 1])
                elif k2 == "a":
                    self.switch_unread()
        elif k.isprintable():
            self.inp.insert(self.pos, k)
            self.pos += 1

    def backspace(self):
        if self.pos > 0:
            del self.inp[self.pos - 1]
            self.pos -= 1

    def history(self, d: int):
        if not self.hist:
            return
        self.hist_i = min(max(0, self.hist_i + d), len(self.hist))
        self.inp = list(self.hist[self.hist_i]) if self.hist_i < len(self.hist) else []
        self.pos = len(self.inp)

    def maybe_load_older(self, th: Thread):
        if (th.id != CORE_ID and th.has_older and not th.loading
                and th.scroll >= max(0, self.last_total - self.last_rows)):
            th.loading = True
            self.worker.submit("more", th.id, self.be.older, th.id, th.oldest_cursor)

    def submit(self):
        text = "".join(self.inp).strip()
        self.inp, self.pos = [], 0
        if not text:
            return
        if not self.hist or self.hist[-1] != text:
            self.hist.append(text)
        self.hist_i = len(self.hist)
        if text.startswith("/") and not text.startswith("//"):
            self.command(text[1:])
            return
        if text.startswith("//"):
            text = text[1:]
        th = self.cur()
        if th.id == CORE_ID:
            self.sysmsg(th, "This is the system buffer - switch to a chat (Ctrl-N) or use /dm USER message.")
            return
        th.scroll = 0
        self.worker.submit("send", th.id, self.be.send, th.id, text)
        self.status = "sending..."

    def command(self, line: str):
        parts = line.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        th = self.cur()
        if cmd in ("quit", "q", "exit"):
            self.quit = True
        elif cmd == "help":
            self.sysmsg(th, HELP)
        elif cmd in ("go", "buffer", "b"):
            if arg.isdigit() and 1 <= int(arg) <= len(self.order):
                self.switch(self.order[int(arg) - 1])
            else:
                self.sysmsg(th, f"usage: /go N  (1..{len(self.order)})")
        elif cmd == "next":
            self.switch_rel(1)
        elif cmd == "prev":
            self.switch_rel(-1)
        elif cmd == "reload":
            self.next_poll = 0.0
            self.status = "refreshing..."
        elif cmd == "more":
            if th.id == CORE_ID or not th.has_older:
                self.sysmsg(th, "no older messages available here")
            elif not th.loading:
                th.loading = True
                self.worker.submit("more", th.id, self.be.older, th.id, th.oldest_cursor)
        elif cmd == "markread":
            self.mark_read(th)
        elif cmd == "clear":
            th.cleared_before = max((m.ts for m in th.msgs.values()), default=time.time())
        elif cmd == "dm":
            self.cmd_dm(arg)
        elif cmd == "links":
            ls = self.links(th)
            if not ls:
                self.sysmsg(th, "no links in this buffer")
            for i, u in enumerate(ls, 1):
                self.sysmsg(th, f"{i}: {u}")
        elif cmd in ("open", "copy"):
            self.cmd_link(cmd, arg)
        else:
            self.sysmsg(th, f"unknown command /{cmd} - try /help")

    def cmd_dm(self, arg: str):
        th = self.cur()
        p = arg.split(None, 1)
        if not p:
            self.sysmsg(th, "usage: /dm USER [message]")
            return
        user = p[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", user):
            self.sysmsg(th, "that does not look like an Instagram username")
            return
        for t in self.threads.values():
            if not t.is_group and any(u.lower() == user.lower() for u in t.users.values()):
                self.switch(t.id)
                if len(p) > 1:
                    t.scroll = 0
                    self.worker.submit("send", t.id, self.be.send, t.id, p[1])
                return
        if len(p) < 2:
            self.sysmsg(th, f"no existing chat with {user}; use /dm {user} your message  to start one")
            return
        self.status = f"messaging {user}..."
        self.worker.submit("dm", user, self.be.start_dm, user, p[1])

    def cmd_link(self, cmd: str, arg: str):
        th = self.cur()
        ls = self.links(th)
        if not ls:
            self.sysmsg(th, "no links in this buffer")
            return
        try:
            n = int(arg) if arg else len(ls)
            url = ls[n - 1] if n >= 1 else None
        except (ValueError, IndexError):
            url = None
        if not url:
            self.sysmsg(th, f"no such link; there are {len(ls)} (see /links)")
            return
        if cmd == "open":
            exe = shutil.which("xdg-open")
            if not exe:
                self.sysmsg(th, "xdg-open not found (sudo dnf install xdg-utils); use /copy instead")
                return
            subprocess.Popen([exe, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.status = f"opened {url}"
        else:
            self.status = "copied" if self.copy_clip(url) else "no clipboard tool; sent OSC52"
            self.status += f": {url}"

    def copy_clip(self, text: str) -> bool:
        tools = []
        if os.environ.get("WAYLAND_DISPLAY"):
            tools.append(["wl-copy"])
        tools += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
        for t in tools:
            if shutil.which(t[0]):
                try:
                    subprocess.run(t, input=text.encode(), check=True, timeout=3,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        import base64
        try:
            os.write(1, b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07")
        except OSError:
            pass
        return False

    # ---- main loop -----------------------------------------------------------
    def loop(self):
        try:
            while not self.quit:
                self.drain_events()
                self.maybe_poll()
                self.mark_read(self.cur())
                if self.dirty or datetime.now().strftime("%H:%M") != self.last_minute:
                    self.draw()
                k = self.read_key()
                if k is not None:
                    self.handle_key(k)
                    self.dirty = True
        except KeyboardInterrupt:
            pass
        finally:
            self.save_state()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=APP, description="WeeChat-style Instagram DMs in your terminal")
    ap.add_argument("-u", "--username", help="Instagram username (asked if omitted)")
    ap.add_argument("--relogin", action="store_true", help="forget the saved session and log in again")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="seconds between inbox checks (default 20, minimum 10)")
    ap.add_argument("--bell", action="store_true", help="beep on new messages in other buffers")
    ap.add_argument("--demo", action="store_true", help="offline demo with fake chats (no account)")
    ap.add_argument("--version", action="version", version=f"{APP} {__version__}")
    args = ap.parse_args(argv)

    locale.setlocale(locale.LC_ALL, "")
    if args.demo:
        backend = DemoBackend()
        args.interval = max(2.0, min(args.interval, 5.0))
    else:
        args.interval = max(10.0, args.interval)
        backend = InstaBackend()
        try:
            backend.login(args.username, args.relogin)
        except (KeyboardInterrupt, EOFError):
            print("\naborted")
            return 130
        except Exception as e:  # noqa: BLE001
            print(f"Login failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:  # keep library noise off the curses screen
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(fd, 2)
        except OSError:
            pass

    def run(stdscr):
        App(stdscr, backend, args).loop()

    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
