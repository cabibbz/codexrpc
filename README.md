# codexrpc — Discord Rich Presence for OpenAI Codex

A live Discord activity banner for OpenAI Codex — both the **CLI** and the **desktop app**. Codex exposes no hook system covering both clients, so `codex-rpc.exe` attaches to a process you pick (by PID) and derives a coarse state from its CPU activity, labeling the banner with the process's **window title** (what Task Manager shows), tracked live.

Sibling project: [clauderpc](https://github.com/cabibbz/clauderpc) does the same for Claude Code via its hook system.

## How it works

- `codex-rpc.exe` (double-click) — UI: process picker (filter defaults to `codex`), **Attach & start** (writes `%TEMP%\codex_rpc_target.json` atomically and starts the daemon if needed), **Detach**, **Stop daemon**, live status card, daemon log tail.
- `codex-rpc.exe daemon` — owns the Discord RPC connection (default app ID baked in; override with `CODEX_DISCORD_APP_ID`). Samples the attached process tree every 5 s.

**Window titles as labels:** the picker shows each process's window title, and the Discord details line tracks it **live** — rename the window, the banner follows within ~5 s. Console apps like the Codex CLI have no window of their own, so the title is resolved through the process's children and parent chain (i.e. the hosting terminal's title, which Codex/Claude Code set per task). Leading spinner glyphs (`⠋`, `✳`…) are stripped so an animating title doesn't spam presence updates, and title renames don't reset the elapsed timer — only state changes do. Processes with no window anywhere fall back to "Codex CLI"/"Codex" labels.

**States** (honest process-activity semantics, not Codex internals):

- `Attached` — just attached, no delta sample yet, or the process denies CPU access (some sandboxed codex processes do)
- `Active` — process-tree CPU above threshold, 2 consecutive samples to flip
- `Idle` — below threshold, 2 consecutive samples to flip

**Threshold:** Codex is network-bound — measured, a live turn peaks around 1.4% of one core and idle is ~0.0% — so the default threshold is **0.5%** (`CODEX_RPC_CPU_THRESHOLD` env var, in percent, to tune).

**PID safety:** the target file stores PID + exe path + process creation time; a recycled PID is never trusted. If the target exits, presence is cleared. With *auto-reattach* checked (default), the daemon adopts the newest process with the same exe path when one appears.

## Attaching to CLI vs desktop app

- **Codex CLI** (npm): processes named `codex.exe` under `...\npm\node_modules\@openai\codex\...`.
- **Codex desktop app** (Microsoft Store): processes under the `OpenAI.Codex_...` WindowsApps package (its Electron processes are named `ChatGPT.exe`) or agent processes under `AppData\Local\OpenAI\Codex\bin\`.

Re-attach any time from the UI; the daemon follows within 5 s without restarting.

## Setup

1. Create a Discord application at <https://discord.com/developers/applications> — its **name** is the banner title (e.g. "Codex"). Upload a 512×512 PNG asset named exactly **`codex`** under Rich Presence → Art Assets. Enter the Application ID in the UI's **Application ID** field and hit **Save** (stored in `%APPDATA%\codex-rpc.json`; a running daemon restarts automatically). The `CODEX_DISCORD_APP_ID` env var overrides the saved value if set.
2. Build (Windows, Python 3.11+):

```powershell
py -m pip install pypresence psutil pyinstaller
py -m PyInstaller --onedir --noconsole --name codex-rpc --noconfirm codex_rpc.py
```

3. Double-click `dist\codex-rpc\codex-rpc.exe`, pick your Codex process, **Attach & start**.

Files: `%TEMP%\codex_rpc_target.json` (attachment), `%TEMP%\codex_rpc_daemon.json` (heartbeat), `%TEMP%\codex_rpc_daemon.log` (log).

## Showing this and clauderpc at once

One Discord client displays only **one** local RPC activity — with two daemons on the same client, only one card shows (and which one can flip when a daemon reconnects). Discord merges activities **across clients**, though: run a second Discord client (PTB/Canary) logged into the same account and each client carries one banner. codex-rpc automatically prefers the second client's IPC pipe (`discord-ipc-1`) and falls back to the first; pin an exact pipe with `CODEX_RPC_PIPE`.
