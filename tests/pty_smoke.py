"""Drive `igterm --demo` inside a pseudo-terminal and check the rendered screen."""
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "igterm.py")
ROWS, COLS = 30, 100

try:
    import pyte
except ImportError:
    pyte = None


def run(keys, rows=ROWS, cols=COLS, wait=0.35):
    pid, fd = pty.fork()
    if pid == 0:
        env = dict(os.environ, TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8")
        os.execvpe(sys.executable, [sys.executable, "-I", SCRIPT, "--demo"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    out = b""
    screen = stream = None
    if pyte:
        screen = pyte.Screen(cols, rows)
        stream = pyte.ByteStream(screen)

    def pump(t):
        nonlocal out
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    d = os.read(fd, 65536)
                except OSError:
                    return
                if not d:
                    return
                out += d
                if stream:
                    stream.feed(d)

    pump(1.0)
    for k in keys:
        os.write(fd, k.encode())
        pump(wait)
    pump(0.5)
    snap = "\n".join(screen.display) if screen else ""
    try:
        _, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        status = 0
    return out.decode("utf-8", "replace"), snap, status, pid


def finish(pid):
    for _ in range(30):
        try:
            p, st = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return 0
        if p:
            return os.waitstatus_to_exitcode(st)
        time.sleep(0.1)
    os.kill(pid, 9)
    return None


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        check.failed = True


check.failed = False

# 1. open chat 2 (alice), send a message, see echo reply after a poll, quit
out, snap, _, pid = run(["/go 2\r", "hello world\r"], wait=0.5)
check("alice" in out, "sidebar/title shows alice")
check("hello world" in out, "sent message is displayed")
check("reel/C0ffee12345" in out, "reel shown as a link")
os.kill(pid, 9)
os.waitpid(pid, 0)

# 2. full session with quit: exit code must be 0 and terminal restored
out, snap, _, pid = run(["/help\r", "/go 3\r", "/links\r", "/dm bob\r", "/bogus\r",
                         "\x0e", "\x10", "\x1b2", "/quit\r"], wait=0.3)
code = finish(pid)
check(code == 0, f"clean exit code (got {code})")
check("Traceback" not in out, "no traceback")
check("/dm USER" in out, "/help printed")
check("unknown command" in out, "unknown command reported")
check("bringing snacks" in out or "snacks" in out, "group buffer content shown")
check("\x1b[?1049l" in out, "alternate screen left on exit")

# 3. tiny terminal does not crash
out, snap, _, pid = run(["hello\r", "\x1b[5~", "/quit\r"], rows=5, cols=20, wait=0.2)
code = finish(pid)
check(code == 0 and "Traceback" not in out, f"tiny terminal survives (exit {code})")

# 4. narrow terminal (no sidebar) + wide chars + escape-injection safe input
out, snap, _, pid = run(["/go 4\r", "日本語 😀 test\r", "\x1b[5~", "\x1b[6~", "/quit\r"],
                        rows=24, cols=50, wait=0.2)
code = finish(pid)
check(code == 0 and "Traceback" not in out, f"narrow terminal + wide chars (exit {code})")

# 5. Ctrl-C exits cleanly
out, snap, _, pid = run(["\x03"], wait=0.4)
code = finish(pid)
check(code == 0 and "Traceback" not in out, f"Ctrl-C exits cleanly (exit {code})")

sys.exit(1 if check.failed else 0)
