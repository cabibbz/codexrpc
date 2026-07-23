"""Discord Rich Presence for OpenAI Codex (CLI and desktop app).

One binary, two modes:
  codex-rpc.exe          - UI: pick a Codex process (PID) to attach, daemon control, status
  codex-rpc.exe daemon   - long-running process that owns the Discord RPC connection

Unlike Claude Code, Codex exposes no hook system covering both the CLI and the
desktop app, so this attaches to a chosen process and derives a coarse state
from its CPU activity (measured on this machine: idle ~0.0%, active turn ~1.4%
of one core - Codex is network-bound, hence the low default threshold):

  Attached - just attached, no delta sample yet, or CPU unreadable (access denied)
  Active   - process tree CPU above threshold (2 consecutive samples to flip)
  Idle     - process tree CPU below threshold (2 consecutive samples to flip)

Target selection is written atomically to %TEMP%\codex_rpc_target.json with the
process's exe path and creation time, not just the PID, so a recycled PID is
never trusted. Re-attaching just rewrites the file; the daemon follows on its
next tick. If auto-reattach is enabled and the target exits, the daemon adopts
the newest process with the same exe path.
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import time

TEMP = os.environ.get("TEMP") or "."
TARGET_FILE = os.path.join(TEMP, "codex_rpc_target.json")
HEARTBEAT_FILE = os.path.join(TEMP, "codex_rpc_daemon.json")
LOG_FILE = os.path.join(TEMP, "codex_rpc_daemon.log")

DEFAULT_APP_ID = "1529700871901020310"

TICK_SECONDS = 5          # CPU sample + heartbeat cadence
CPU_THRESHOLD = float(os.environ.get("CODEX_RPC_CPU_THRESHOLD", "0.5"))  # % of one core
FLIP_SAMPLES = 2          # consecutive samples needed to flip Active/Idle


# ---------------------------------------------------------------- shared bits

def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_heartbeat(**fields) -> None:
    fields["pid"] = os.getpid()
    fields["ts"] = int(time.time())
    try:
        atomic_write_json(HEARTBEAT_FILE, fields)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def daemon_pid() -> int:
    hb = read_json(HEARTBEAT_FILE)
    pid = hb.get("pid", 0)
    return pid if pid != os.getpid() and pid_alive(pid) else 0


def friendly_label(exe: str, name: str) -> str:
    e = (exe or "").lower()
    if "node_modules" in e or "\\npm\\" in e:
        return "Codex CLI"
    if "windowsapps" in e or "\\openai\\codex" in e:
        return "Codex"
    return name or "Process"


def clean_title(title: str) -> str:
    """Strip leading spinner/status glyphs (braille spinners, ✳ etc.) so the
    label stays stable while a CLI animates its title, and fit Discord's
    128-char details limit."""
    return re.sub(r"^[^0-9A-Za-z]+", "", title).strip()[:128]


def window_titles_by_pid() -> dict:
    """pid -> first visible top-level window title (like Task Manager shows)."""
    titles = {}
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                titles.setdefault(pid.value, buf.value)
        return True

    user32.EnumWindows(cb, 0)
    return titles


def live_title(proc, titles: dict) -> str:
    """Window title for a process: its own window, else a child's, else a
    parent's (console apps like the Codex CLI have no window of their own -
    the title lives on the terminal that hosts them)."""
    try:
        if proc.pid in titles:
            return clean_title(titles[proc.pid])
        for c in proc.children(recursive=True):
            if c.pid in titles:
                return clean_title(titles[c.pid])
        for p in proc.parents():
            if p.pid in titles:
                return clean_title(titles[p.pid])
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- daemon mode

def resolve_target(psutil, target: dict):
    """Return a live psutil.Process matching the stored identity, else None.

    Identity = pid + exe path + creation time (1s tolerance), so a recycled
    PID is rejected. With auto_reattach, falls back to the newest process
    with the same exe path.
    """
    pid, exe, created = target.get("pid"), target.get("exe"), target.get("created")
    if pid:
        try:
            p = psutil.Process(pid)
            if (not exe or (p.exe() or "") == exe) and \
               (not created or abs(p.create_time() - created) < 1.0):
                return p
        except Exception:
            pass
    if target.get("auto_reattach") and exe:
        best = None
        for p in psutil.process_iter(["exe", "create_time"]):
            try:
                if (p.info["exe"] or "") == exe:
                    if best is None or p.info["create_time"] > best.info["create_time"]:
                        best = p
            except Exception:
                pass
        if best is not None:
            log(f"auto-reattached to pid {best.pid} ({exe})")
            target.update(pid=best.pid, created=best.info["create_time"])
            try:
                atomic_write_json(TARGET_FILE, target)
            except OSError:
                pass
            return psutil.Process(best.pid)
    return None


def tree_cpu_seconds(proc) -> float:
    """Total user+system CPU seconds of the process tree; None if the root
    process is unreadable. Unreadable children are skipped."""
    try:
        ts = proc.cpu_times()
        total = ts.user + ts.system
        for c in proc.children(recursive=True):
            try:
                cts = c.cpu_times()
                total += cts.user + cts.system
            except Exception:
                pass
        return total
    except Exception:
        return None


def connect(app_id: str):
    """Connect to Discord. One Discord client displays only ONE local RPC
    activity, so to show Claude and Codex banners simultaneously the user runs
    a second Discord client (PTB/Canary) on the same account: codex-rpc
    prefers the second client's pipe (discord-ipc-1) and falls back to any.
    CODEX_RPC_PIPE pins an exact pipe index instead."""
    from pypresence import Presence

    preferred = os.environ.get("CODEX_RPC_PIPE")
    candidates = [int(preferred)] if preferred else [1, None]
    while True:
        exc = None
        for pipe in candidates:
            rpc = Presence(app_id, pipe=pipe) if pipe is not None else Presence(app_id)
            try:
                rpc.connect()
                log(f"connected to Discord RPC (pipe {'auto' if pipe is None else pipe})")
                return rpc
            except Exception as e:
                exc = e
        log(f"Discord not reachable ({exc.__class__.__name__}); retrying in 15s")
        write_heartbeat(discord="connecting")
        time.sleep(15)


def daemon_mode() -> int:
    import psutil

    app_id = os.environ.get("CODEX_DISCORD_APP_ID") or DEFAULT_APP_ID

    if daemon_pid():
        log("daemon already running; exiting")
        return 0

    try:
        os.remove(LOG_FILE)
    except OSError:
        pass
    log(f"daemon starting (pid {os.getpid()}, app id {app_id})")
    write_heartbeat(discord="connecting")

    rpc = connect(app_id)
    prev_sample = None        # (pid, cpu_seconds, monotonic)
    label = None              # currently pushed state label
    pending, pending_n = None, 0
    since = 0
    pushed_key = None         # (pid, label) last sent to Discord
    presence_visible = False

    def push(target_label, state_label, start):
        nonlocal pushed_key, presence_visible
        rpc.update(details=target_label, state=state_label,
                   large_image="codex", start=start)
        presence_visible = True
        log(f"presence -> {target_label} / {state_label}")

    try:
        while True:
            target = read_json(TARGET_FILE)
            proc = resolve_target(psutil, target) if target.get("pid") else None

            if proc is None:
                status = "exited" if target.get("pid") else "none"
                if presence_visible:
                    try:
                        rpc.clear()
                    except Exception:
                        pass
                    presence_visible = False
                    pushed_key = None
                    log("target gone; presence cleared")
                prev_sample, label, pending, pending_n, since = None, None, None, 0, 0
                write_heartbeat(discord="connected", target_status=status)
            else:
                now = time.monotonic()
                cpu = tree_cpu_seconds(proc)
                new_label = label or "Attached"
                if cpu is None:
                    new_label = "Attached"  # alive but unreadable (access denied)
                elif prev_sample and prev_sample[0] == proc.pid:
                    pct = (cpu - prev_sample[1]) / max(now - prev_sample[2], 1) * 100
                    raw = "Active" if pct >= CPU_THRESHOLD else "Idle"
                    if raw != label:
                        pending_n = pending_n + 1 if pending == raw else 1
                        pending = raw
                        if pending_n >= FLIP_SAMPLES or label in (None, "Attached"):
                            new_label = raw
                            pending, pending_n = None, 0
                    else:
                        pending, pending_n = None, 0
                if cpu is not None:
                    prev_sample = (proc.pid, cpu, now)

                tlabel = live_title(proc, window_titles_by_pid()) \
                    or target.get("label") or "Codex"
                key = (proc.pid, new_label, tlabel)
                if key != pushed_key:
                    # a mere window-title rename must not reset the timer
                    if pushed_key is None or new_label != pushed_key[1] \
                            or proc.pid != pushed_key[0] or not since:
                        since = int(time.time())
                    try:
                        push(tlabel, new_label, since)
                        pushed_key = key
                        label = new_label
                    except Exception as exc:
                        log(f"update failed ({exc.__class__.__name__}); reconnecting")
                        try:
                            rpc.close()
                        except Exception:
                            pass
                        rpc = connect(app_id)
                        pushed_key = None
                        continue
                write_heartbeat(discord="connected", target_status="ok",
                                target_pid=proc.pid, target_label=tlabel,
                                activity=label, since=since)
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        log("shutting down")
        try:
            rpc.clear()
            rpc.close()
        except Exception:
            pass
        try:
            os.remove(HEARTBEAT_FILE)
        except OSError:
            pass
        return 0


# -------------------------------------------------------------------- UI mode

BG = "#1e1f22"
CARD = "#2b2d31"
FG = "#dbdee1"
DIM = "#949ba4"
GREEN = "#23a55a"
RED = "#f23f43"
YELLOW = "#f0b232"


def exe_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, os.path.abspath(__file__), *args]


def ui_mode() -> int:
    import tkinter as tk
    import psutil

    root = tk.Tk()
    root.title("Codex RPC")
    root.configure(bg=BG, padx=16, pady=14)
    root.resizable(False, False)

    tk.Label(root, text="Codex — Discord Rich Presence", bg=BG, fg=FG,
             font=("Segoe UI Semibold", 11)).grid(row=0, column=0, columnspan=3,
                                                  sticky="w", pady=(0, 10))

    # ---- process picker
    picker = tk.Frame(root, bg=CARD, padx=12, pady=10)
    picker.grid(row=1, column=0, columnspan=3, sticky="ew")

    tk.Label(picker, text="Filter", bg=CARD, fg=DIM,
             font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
    filter_var = tk.StringVar(value="codex")
    filter_entry = tk.Entry(picker, textvariable=filter_var, bg=BG, fg=FG,
                            insertbackground=FG, relief="flat", width=24,
                            font=("Segoe UI", 9))
    filter_entry.grid(row=0, column=1, sticky="w", padx=(8, 8))

    listbox = tk.Listbox(picker, width=74, height=8, bg=BG, fg=FG, relief="flat",
                         font=("Consolas", 8), selectbackground="#404249",
                         selectforeground=FG, activestyle="none")
    listbox.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    rows = []  # parallel to listbox lines: (pid, name, exe, created, title)

    def refresh_processes():
        needle = filter_var.get().strip().lower()
        titles = window_titles_by_pid()
        rows.clear()
        listbox.delete(0, "end")
        matched = []
        for p in psutil.process_iter(["name", "exe", "create_time"]):
            try:
                name, exe = p.info["name"] or "", p.info["exe"] or ""
                if needle and needle not in name.lower() and needle not in exe.lower():
                    continue
                matched.append((p, name, exe))
            except Exception:
                continue
        for p, name, exe in matched:
            try:
                title = live_title(p, titles)  # own window, else child's/terminal's
            except Exception:
                title = ""
            rows.append((p.pid, name, exe, p.info["create_time"] or 0, title))
        # windowed processes first, then by name
        order = sorted(range(len(rows)),
                       key=lambda i: (not rows[i][4], rows[i][1].lower(), rows[i][0]))
        rows[:] = [rows[i] for i in order]
        for pid, name, exe, _, title in rows:
            shown = title or (exe[-46:] if exe else "(path unavailable)")
            listbox.insert("end", f"{pid:>6}  {name[:18]:<18} {shown[:48]}")

    auto_var = tk.BooleanVar(value=True)
    tk.Checkbutton(picker, text="Auto-reattach when a new process with the same exe appears",
                   variable=auto_var, bg=CARD, fg=DIM, selectcolor=BG,
                   activebackground=CARD, activeforeground=FG,
                   font=("Segoe UI", 8)).grid(row=2, column=0, columnspan=2,
                                              sticky="w", pady=(6, 0))

    def styled_btn(parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=CARD, fg=FG,
                         activebackground="#404249", activeforeground=FG,
                         relief="flat", font=("Segoe UI", 9), padx=14, pady=4)

    msg_var = tk.StringVar(value="")

    def start_daemon():
        if not daemon_pid():
            flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen(exe_command("daemon"), creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)

    def attach():
        sel = listbox.curselection()
        if not sel:
            msg_var.set("Select a process first.")
            return
        pid, name, exe, created, title = rows[sel[0]]
        if not pid_alive(pid):
            msg_var.set(f"PID {pid} is no longer running — refresh the list.")
            refresh_processes()
            return
        label = title or friendly_label(exe, name)
        try:
            atomic_write_json(TARGET_FILE, {
                "pid": pid, "exe": exe, "created": created,
                "label": label,
                "auto_reattach": bool(auto_var.get()),
            })
        except OSError as exc:
            msg_var.set(f"Could not write target file: {exc}")
            return
        msg_var.set(f"Attached to {pid} ({label}).")
        start_daemon()

    def detach():
        try:
            os.remove(TARGET_FILE)
        except OSError:
            pass
        msg_var.set("Detached.")

    def stop_daemon():
        pid = daemon_pid()
        if pid:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, creationflags=0x08000000)
            except Exception:
                pass
        try:
            os.remove(HEARTBEAT_FILE)
        except OSError:
            pass

    btnrow = tk.Frame(picker, bg=CARD)
    btnrow.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
    styled_btn(btnrow, "Refresh", refresh_processes).pack(side="left")
    styled_btn(btnrow, "Attach & start", attach).pack(side="left", padx=(8, 0))
    styled_btn(btnrow, "Detach", detach).pack(side="left", padx=(8, 0))
    filter_entry.bind("<Return>", lambda e: refresh_processes())

    tk.Label(root, textvariable=msg_var, bg=BG, fg=YELLOW,
             font=("Segoe UI", 8)).grid(row=2, column=0, columnspan=3,
                                        sticky="w", pady=(6, 0))

    # ---- status card
    card = tk.Frame(root, bg=CARD, padx=14, pady=12)
    card.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def row(r, name):
        tk.Label(card, text=name, bg=CARD, fg=DIM, font=("Segoe UI", 9),
                 width=9, anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        val = tk.Label(card, text="—", bg=CARD, fg=FG, font=("Segoe UI", 10), anchor="w")
        val.grid(row=r, column=1, sticky="w", pady=2)
        return val

    daemon_val = row(0, "Daemon")
    discord_val = row(1, "Discord")
    target_val = row(2, "Target")
    activity_val = row(3, "Activity")
    timer_val = row(4, "Elapsed")

    ctlrow = tk.Frame(root, bg=BG)
    ctlrow.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
    styled_btn(ctlrow, "Stop daemon", stop_daemon).pack(side="left")

    log_box = tk.Text(root, height=5, width=86, bg=CARD, fg=DIM, relief="flat",
                      font=("Consolas", 8), state="disabled", padx=8, pady=6)
    log_box.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))

    def fmt_elapsed(since):
        if not since:
            return "—"
        s = max(0, int(time.time()) - int(since))
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    def refresh_status():
        pid = daemon_pid()
        hb = read_json(HEARTBEAT_FILE)
        if pid:
            daemon_val.config(text=f"● Running (pid {pid})", fg=GREEN)
            if hb.get("discord") == "connected":
                discord_val.config(text="Connected", fg=GREEN)
            else:
                discord_val.config(text="Waiting for Discord…", fg=YELLOW)
            status = hb.get("target_status")
            if status == "ok":
                target_val.config(
                    text=f"{hb.get('target_label', '?')} (pid {hb.get('target_pid', '?')})",
                    fg=GREEN)
                activity_val.config(text=hb.get("activity") or "sampling…", fg=FG)
                timer_val.config(text=fmt_elapsed(hb.get("since")), fg=FG)
            elif status == "exited":
                target_val.config(text="Target exited — attach a new process", fg=YELLOW)
                activity_val.config(text="—", fg=DIM)
                timer_val.config(text="—", fg=DIM)
            else:
                target_val.config(text="No target — pick a process and Attach", fg=DIM)
                activity_val.config(text="—", fg=DIM)
                timer_val.config(text="—", fg=DIM)
        else:
            daemon_val.config(text="○ Stopped (attach to start)", fg=RED)
            discord_val.config(text="—", fg=DIM)
            target_val.config(text="—", fg=DIM)
            activity_val.config(text="—", fg=DIM)
            timer_val.config(text="—", fg=DIM)

        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                tail = "".join(f.readlines()[-5:])
        except OSError:
            tail = ""
        log_box.config(state="normal")
        log_box.delete("1.0", "end")
        log_box.insert("1.0", tail or "(no daemon log yet)")
        log_box.config(state="disabled")

        root.after(1000, refresh_status)

    refresh_processes()
    refresh_status()
    root.mainloop()
    return 0


# ------------------------------------------------------------------- dispatch

def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].lower() == "ui":
        return ui_mode()
    if sys.argv[1].lower() == "daemon":
        return daemon_mode()
    log(f"error: unknown mode '{sys.argv[1]}'")
    return 2


if __name__ == "__main__":
    sys.exit(main())
