"""
The agent: one command that keeps everything running.

  python -m jobagent.run               # loop forever in this window (Ctrl+C to stop)
  python -m jobagent.run --once        # one full cycle, then stop (good for testing)
  pythonw -m jobagent.run --background # hidden; logs to data/agent.log (used by auto-start)

Each cycle:
  1. fetch new posts from every chat in config/sources.txt (read-only; joined or not)
  2. screen new job posts with the AI (daily spending cap applies)      } skipped while
  3. add new rows to your Google Sheet                                    } paused with
  4. send instant alerts for strong matches (not during quiet hours)      } /pause
  5. send the daily digest once, at the set time                          }
Meanwhile it answers your bot commands (/status /top /digest /export /rate /learn /rules /pause
/resume /help) and saves your 👍/🤔/👎 taps (and the reasons you tap) on job cards.

Learning (see learn.py): a weekly review proposes rules from your ratings (you approve each),
occasional spot-check rating cards (fewer as accuracy rises), and an automatic model re-test
every time you reach another 15 👍. Long jobs run in the background, so checks keep going.

Safety nets: only one copy can run at a time; if a step fails the others still run and
your bot tells you (at most once an hour); if the whole agent crashes it restarts itself.
"""
import argparse
import asyncio
import json
import os
import socket
import sys
import time as _time
import traceback
from datetime import datetime, timedelta, timezone

ERROR_NOTICE_EVERY = 3600     # seconds between "something failed" messages
LOCK_PORT = 47321             # a local port held while the agent runs = "only one copy" lock
LOG_MAX_BYTES = 5_000_000

REQUIRED = {"telethon": "telethon", "anthropic": "anthropic", "gspread": "gspread", "yaml": "pyyaml",
            "bs4": "beautifulsoup4", "pydantic": "pydantic", "requests": "requests", "dotenv": "python-dotenv"}


def log(msg: str):
    print(f"[{datetime.now(timezone(timedelta(hours=8))):%d %b %H:%M:%S}] {msg}", flush=True)


# ---------------- start-up checks ----------------

def preflight():
    """Stop early, with a clear fix, if the wrong Python is running or a package is missing."""
    import importlib.util
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not in_venv:
        raise SystemExit(f"Not running inside the virtual environment (Python: {sys.executable}).\n"
                         "Run  .venv\\Scripts\\Activate.ps1  first, then start the agent again.")
    if missing:
        raise SystemExit("Missing packages: " + ", ".join(missing) +
                         "\nRun  pip install " + " ".join(missing) + "  then start the agent again.")


def single_instance_lock():
    """Hold a local port; a second copy can't, so it exits instead of fighting the first."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        raise SystemExit("The job agent is already running (probably the auto-started one). "
                         "Use /status in your bot, or stop it with stop_agent.ps1 first.")
    return s  # keep a reference so the port stays held


def log_to_file():
    """Background mode has no window: send all output to data/agent.log (rotated at 5 MB)."""
    from .settings import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "agent.log"
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        old = DATA_DIR / "agent.log.1"
        if old.exists():
            old.unlink()
        path.rename(old)
    f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = f
    return path


def keep_awake(mode: str):
    """Windows only: stop the PC sleeping while the agent runs ('plugged_in', 'always' or 'never').
    The screen can still turn off. Must be called from the main thread; re-checked every cycle."""
    if os.name != "nt" or mode == "never":
        return
    import ctypes

    class PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ps = PowerStatus()
    on_ac = bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(ps))) and ps.ACLineStatus == 1
    want = mode == "always" or (mode == "plugged_in" and on_ac)
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))


# ---------------- the agent ----------------

class Agent:
    def __init__(self):
        from .config import load_agent
        self.cfg = load_agent()
        self.acfg = self.cfg.get("agent") or {}
        self.last_error_notice = 0.0
        self.chats = {}
        self.started = datetime.now(timezone.utc)
        self.last_cycle = None
        self.next_cycle = None
        self.wake = None  # asyncio.Event, created inside the loop
        self.bg = None    # the background job (learning review / model re-test), if one is running

    # pause state survives restarts
    @property
    def paused(self) -> bool:
        from . import notify
        return bool(notify._load_state().get("paused"))

    def set_paused(self, value: bool):
        from . import notify
        st = notify._load_state()
        st["paused"] = value
        notify._save_state(st)

    async def resolve_chats(self, client):
        from telethon import utils
        from . import notify
        from .settings import load_sources
        await client.get_dialogs()
        info = {}
        for src in load_sources():
            try:
                ent = await client.get_entity(src)
                peer = utils.get_peer_id(ent)
                self.chats[peer] = (ent, utils.get_display_name(ent))
                info[str(peer)] = notify.chat_link_info(ent)
            except Exception as e:
                log(f"! can't open {src}: {type(e).__name__}. Skipping it.")
        notify.save_chat_info(info)   # lets alerts link to posts in a way that actually opens
        log(f"Watching {len(self.chats)} chats.")

    async def fetch_new(self, client) -> tuple[int, int]:
        from .store import Store
        from .tg import to_record
        store = Store()
        new = cand = 0
        first_window = datetime.now(timezone.utc) - timedelta(days=1)
        for peer_id, (ent, title) in self.chats.items():
            last = store.conn.execute("SELECT MAX(msg_id) FROM messages WHERE chat_id = ?", (peer_id,)).fetchone()[0]
            kwargs = dict(min_id=last, reverse=True, limit=500) if last else dict(offset_date=first_window, reverse=True, limit=500)
            try:
                async for msg in client.iter_messages(ent, **kwargs):
                    rec = to_record(msg, peer_id, title)
                    if rec and store.save(rec):
                        new += 1
                        cand += rec["is_candidate"]
            except Exception as e:
                log(f"! couldn't read {title}: {type(e).__name__}: {str(e)[:120]}")
        return new, cand

    def _export(self):
        """/export: write a dated CSV (a new name, so an open Excel file can't block it) and send it."""
        from . import notify
        from .screen import export
        from .settings import DATA_DIR
        path = DATA_DIR / f"screened_{datetime.now(timezone(timedelta(hours=8))):%Y-%m-%d_%H%M}.csv"
        n, by = export(path)
        summary = ", ".join(f"{k} {v}" for k, v in sorted(by.items()))
        notify.send_file(path, f"📎 <b>{n} screened jobs</b> ({notify.e(summary)}). "
                               "Open in Excel or Google Sheets; sort by fit_score.")
        path.unlink(missing_ok=True)  # the copy now lives in Telegram; don't let them pile up

    def _screen(self):
        from .screen import Screener
        Screener().run(self.acfg.get("screen_per_cycle", 60), self.acfg.get("screen_max_age_days", 3), quiet=True)

    async def step(self, name, fn):
        """Run one step; on failure log it, tell you via the bot (rate-limited), and carry on."""
        try:
            return await fn()
        except SystemExit as e:   # config problems (missing key etc.) have readable messages
            await self.report(name, str(e))
        except Exception as e:
            traceback.print_exc()
            await self.report(name, f"{type(e).__name__}: {str(e)[:300]}")
        return None

    async def report(self, step, message):
        from . import notify
        log(f"! step '{step}' failed: {message}")
        if _time.time() - self.last_error_notice < ERROR_NOTICE_EVERY:
            return
        self.last_error_notice = _time.time()
        try:
            await asyncio.to_thread(notify.send, f"⚠️ <b>Job agent problem</b> in step <i>{notify.e(step)}</i>:\n"
                                                 f"<code>{notify.e(message)}</code>")
        except Exception:
            pass  # if Telegram itself is the problem, the log still has it

    async def cycle(self, client):
        from . import notify, sheet
        keep_awake(self.acfg.get("keep_awake", "plugged_in"))
        res = await self.step("fetch", lambda: self.fetch_new(client))
        if res:
            log(f"fetched {res[0]} new messages ({res[1]} look like jobs)")
        if self.paused:
            log("paused: skipping screening, alerts and digest")
            return
        await self.step("screen", lambda: asyncio.to_thread(self._screen))
        n = await self.step("sheet", lambda: asyncio.to_thread(sheet.sync))
        if n:
            log(f"added {n} rows to the sheet")
        n = await self.step("alerts", lambda: asyncio.to_thread(notify.send_alerts))
        if n:
            log(f"sent {n} alert(s)")
        if await self.step("digest", lambda: asyncio.to_thread(notify.send_digest_if_due)):
            log("sent the daily digest")
        await self.step("learning", self.learning_checks)

    # ---------- learning ----------
    async def learning_checks(self):
        from . import learn, notify
        now = datetime.now(timezone.utc)
        if await asyncio.to_thread(learn.review_due):
            self.start_background("learning review", learn.review,
                                  "🧠 Weekly learning review started (about 10 min). Proposals will follow.")
        elif await asyncio.to_thread(learn.retest_due):
            self.start_background("model re-test", learn.retest,
                                  "🧪 You've collected enough 👍 for a new model test. Running it now (~20-30 min).")
        quiet = notify._in_quiet_hours(now.astimezone(notify.MYT),
                                       (self.cfg.get("notify") or {}).get("quiet_hours", "23:00-08:00"))
        if not quiet and await asyncio.to_thread(learn.spot_due, now):
            if await asyncio.to_thread(self._send_rate_card, True):
                learn.mark_spot(now)
                log("sent a spot-check rating card")

    def start_background(self, name, fn, notice=None) -> bool:
        """Run a long job (several minutes) without blocking checks or bot commands."""
        from . import notify
        if self.bg and not self.bg.done():
            notify.send(f"⏳ Already busy with the {self.bg.get_name()}. Try again when it finishes.")
            return False
        if notice:
            notify.send(notice)

        async def job():
            await self.step(name, lambda: asyncio.to_thread(fn, lambda s: log(f"[{name}] {s}")))
            log(f"{name} finished")

        self.bg = asyncio.create_task(job(), name=name)
        return True

    # ---------- bot commands ----------
    async def handle(self, cmd: str):
        from . import notify
        if cmd in ("start", "help"):
            reply = notify.help_text()
        elif cmd == "status":
            reply = await asyncio.to_thread(notify.status_text, {
                "running_since": self.started, "last_cycle": self.last_cycle,
                "next_cycle": self.next_cycle, "chats": len(self.chats), "paused": self.paused})
        elif cmd == "top":
            await asyncio.to_thread(notify.send_top, 5)
            return
        elif cmd == "rate":
            await asyncio.to_thread(self._send_rate_card)
            return
        elif cmd == "learn":
            from . import learn
            self.start_background("learning review", learn.review,
                                  "🧠 Reviewing your ratings now (about 10 min). I'll send any proposed rules.")
            return
        elif cmd == "rules":
            from . import learn
            text, kb = await asyncio.to_thread(learn.rules_message)
            await asyncio.to_thread(notify._call, "sendMessage", chat_id=notify.my_chat_id(), text=text,
                                    parse_mode="HTML", **({"reply_markup": kb} if kb else {}))
            return
        elif cmd == "digest":
            await asyncio.to_thread(notify.send_digest_if_due, None, True)
            return
        elif cmd == "export":
            await asyncio.to_thread(self._export)
            return
        elif cmd == "pause":
            self.set_paused(True)
            reply = ("⏸ Paused. I'll keep collecting posts but won't screen, alert or spend until /resume.")
        elif cmd == "resume":
            was = self.paused
            self.set_paused(False)
            reply = "▶️ Resumed. Checking now." if was else "Already running. Checking now."
            if self.wake:
                self.wake.set()
        else:
            reply = f"Unknown command /{notify.e(cmd)}. Try /help."
        await asyncio.to_thread(notify.send, reply)

    def _send_rate_card(self, spot: bool = False) -> bool:
        from . import feedback, notify
        r, band, done = feedback.next_to_rate()
        if r is None:
            if not spot:
                notify.send(f"Nothing new to rate from the last {feedback.RATE_WINDOW_DAYS} days "
                            f"({done} blind ratings so far). Try again after more jobs come in.")
            return False
        if spot:
            notify.send("🔁 <b>Quick spot-check</b>: one tap keeps the agent honest. "
                        "These get rarer as it matches you better.")
        notify.send_job(feedback.blind_card(r, done), r, "r", with_next=not spot)
        return True

    def _tap(self, ev):
        """Buttons under messages: ratings, reasons, /rate next, and rule approvals."""
        from . import feedback, notify
        data = ev["data"].split("|")
        mid_ = ev.get("message_id")
        if data[0] == "rate" and data[1:] == ["next"]:
            notify.answer_tap(ev["id"])
            self._send_rate_card()
            return
        if data[0] == "lr" and len(data) == 3:
            from . import learn, learned
            act, rid = data[1], data[2]
            if act == "d":
                gone = learned.remove(rid)
                notify.answer_tap(ev["id"], "Removed" if gone else "Already removed")
                if mid_:
                    text, kb = learn.rules_message()
                    notify._call("editMessageText", chat_id=notify.my_chat_id(), message_id=mid_, text=text,
                                 parse_mode="HTML", **({"reply_markup": kb} if kb else {}))
                return
            msg = learn.decide(rid, act == "a")
            notify.answer_tap(ev["id"], msg[:190])
            if mid_:
                notify.mark_choice(mid_, {"inline_keyboard": [[{"text": msg[:60], "callback_data": "noop"}]]})
            return
        if data[0] == "noop":
            notify.answer_tap(ev["id"])
            return
        if data[0] in ("fb", "rs") and len(data) == 6:
            _, source, cid, mid, idx, code = data
        elif data[0] == "rd" and len(data) == 5:
            _, source, cid, mid, idx = data
            code = None
        else:
            notify.answer_tap(ev["id"], "Unknown button")
            return
        cid, mid, idx = int(cid), int(mid), int(idx)
        nxt = source == "r"
        if data[0] == "fb":
            label = feedback.save(cid, mid, idx, code, source)
            asking = code in ("m", "n")
            notify.answer_tap(ev["id"], f"Saved: {label}" + (". Why? Tap reasons, then Done." if asking else ""))
            kb = feedback.keyboard(cid, mid, idx, source, chosen=code, with_next=nxt, asking=asking)
        elif data[0] == "rs":
            picked = feedback.toggle_reason(cid, mid, idx, code)
            notify.answer_tap(ev["id"], ", ".join(feedback.reason_labels(picked)) or "No reason picked")
            kb = feedback.keyboard(cid, mid, idx, source, chosen=feedback.verdict_code(cid, mid, idx),
                                   with_next=nxt, asking=True, reasons=picked)
        else:
            notify.answer_tap(ev["id"], "Thanks, saved.")
            kb = feedback.keyboard(cid, mid, idx, source, chosen=feedback.verdict_code(cid, mid, idx), with_next=nxt)
        if mid_:
            notify.mark_choice(mid_, kb)

    async def listen_commands(self):
        from . import notify
        st = notify._load_state()
        offset = st.get("update_offset")
        while True:
            try:
                events, offset = await asyncio.to_thread(notify.get_events, offset, 25)
                if offset != st.get("update_offset"):
                    st = notify._load_state()
                    st["update_offset"] = offset
                    notify._save_state(st)
                for ev in events:
                    if ev["type"] == "tap":
                        log(f"button {ev['data']}")
                        await asyncio.to_thread(self._tap, ev)
                    else:
                        log(f"bot command /{ev['name']}")
                        await self.handle(ev["name"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"! bot command listener: {type(e).__name__}: {str(e)[:150]}")
                await asyncio.sleep(30)

    # ---------- main loop ----------
    async def main(self, once: bool):
        from . import notify
        from .tg import make_client
        client = make_client()
        await client.start()
        await self.resolve_chats(client)
        if not self.chats:
            raise SystemExit("No chats could be opened from sources.txt.")
        minutes = self.acfg.get("poll_minutes", 15)
        self.wake = asyncio.Event()
        listener = None
        if not once:
            try:
                await asyncio.to_thread(notify.set_commands)
            except Exception as e:
                log(f"! couldn't set the bot menu: {e}")
            listener = asyncio.create_task(self.listen_commands())
        try:
            while True:
                log("cycle start")
                await self.cycle(client)
                self.last_cycle = datetime.now(timezone.utc)
                if once:
                    break
                self.next_cycle = self.last_cycle + timedelta(minutes=minutes)
                log(f"sleeping {minutes} min")
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=minutes * 60)
                except asyncio.TimeoutError:
                    pass
        finally:
            if listener:
                listener.cancel()
            await client.disconnect()


def _tell(message: str):
    try:
        from . import notify
        notify.send(message)
    except Exception:
        pass


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one cycle and stop")
    ap.add_argument("--background", action="store_true", help="no window: log to data/agent.log (auto-start)")
    args = ap.parse_args()

    if args.background:
        path = log_to_file()
        log(f"--- agent starting in background (log: {path}) ---")
    try:
        preflight()
        lock = single_instance_lock()  # noqa: F841 (held for the life of the process)
    except SystemExit as e:
        log(str(e))
        if args.background and "already running" not in str(e):
            _tell(f"⚠️ <b>Job agent couldn't start</b>\n<code>{e}</code>")
        raise

    if args.once:
        asyncio.run(Agent().main(once=True))
        return

    if args.background:
        _tell("🟢 <b>Job agent started.</b> Tap / in this chat for commands.")
    restarts = 0
    while True:
        try:
            asyncio.run(Agent().main(once=False))
            break
        except KeyboardInterrupt:
            log("stopped by you")
            break
        except SystemExit as e:
            log(f"stopped: {e}")
            _tell(f"🛑 <b>Job agent stopped</b>\n<code>{e}</code>")
            break
        except Exception as e:
            restarts += 1
            traceback.print_exc()
            wait = min(60 * restarts, 900)  # 1 min, 2 min, ... up to 15 min
            log(f"! crashed ({type(e).__name__}); restarting in {wait // 60} min")
            if restarts in (1, 5, 20):
                _tell(f"⚠️ <b>Job agent crashed</b> ({type(e).__name__}); restarting automatically.")
            _time.sleep(wait)


if __name__ == "__main__":
    cli()
