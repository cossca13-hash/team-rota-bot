"""Team rota bot for Telegram - GitHub Actions edition.

GitHub starts this script every ~5 minutes. Each run:
  1. reads new Telegram messages / Done-button taps and reacts to them,
  2. sends any notification that is due (morning duty, Thursday cleaning, reminders),
  3. saves its memory (encrypted) to state.enc, which the workflow commits back.

Rota
  Weekly duty (08:00): Sun Misge, Tue Eden, Thu Kal, Fri Mhret.
    Not done -> that person is re-pinged every following morning until Done.
  Cleaning (Thursday 20:00): pairs alternate weekly (Kal+Misge, Eden+Mhret).
    Not done -> re-pinged every following Friday morning (from next week) until Done.
"""
import base64
import hashlib
import html
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

# ----------------------------------------------------------------- config
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TZ = ZoneInfo(os.environ.get("TIMEZONE") or "Asia/Dubai")
STATE_FILE = Path("state.enc")


def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


MORNING_TIME = hhmm(os.environ.get("MORNING_TIME") or "08:00")
CLEANING_TIME = hhmm(os.environ.get("CLEANING_TIME") or "20:00")  # Thursday night

# Python weekday(): Monday=0 ... Sunday=6
DUTY_ROTA = {6: "misge", 1: "eden", 3: "kal", 4: "mhret"}
CLEANING_PAIRS = [["kal", "misge"], ["eden", "mhret"]]
# A Thursday in a week where pair #0 (Kal + Misge) cleans. Pairs alternate weekly from here.
CLEANING_ANCHOR = date(2026, 10, 8)
THURSDAY, FRIDAY = 3, 4

NAMES = sorted(set(DUTY_ROTA.values()) | {n for p in CLEANING_PAIRS for n in p})

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rotabot")


# ----------------------------------------------------------------- Telegram API
def tg(method: str, **params):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30)
        body = r.json()
    except Exception as e:  # network problem etc.
        log.warning("%s failed: %s", method, e)
        return None
    if not body.get("ok"):
        log.warning("%s -> %s", method, body.get("description"))
        return None
    return body["result"]


# ----------------------------------------------------------------- state (encrypted)
def fernet() -> Fernet:
    # Key derived from the bot token, so no extra secret is needed and the
    # committed state file is unreadable to anyone without the token.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(TOKEN.encode()).digest()))


def load() -> dict:
    state = {}
    if STATE_FILE.exists():
        state = json.loads(fernet().decrypt(STATE_FILE.read_bytes()))
    defaults = {
        "offset": 0,
        "group_chat_id": None,
        "users": {},  # name -> {"tg_id": int}
        "instances": {},  # id -> instance
        "next_id": 1,
        "last_morning": None,
        "last_cleaning": None,
    }
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def save(state: dict, original: str) -> None:
    current = json.dumps(state, sort_keys=True)
    if current != original:  # only rewrite (and so only commit) when something changed
        STATE_FILE.write_bytes(fernet().encrypt(current.encode()))


def today() -> date:
    return datetime.now(TZ).date()


# ----------------------------------------------------------------- rendering
def esc(s: str) -> str:
    return html.escape(s)


def mention(name: str, state: dict) -> str:
    user = state["users"].get(name)
    disp = esc(name.capitalize())
    if user:
        return f'<a href="tg://user?id={user["tg_id"]}">{disp}</a>'
    return f"{disp} (not linked yet)"


def render(inst: dict, state: dict, prefix: str = "") -> str:
    icon = "🧹" if inst["kind"] == "cleaning" else "📌"
    lines = [f"{prefix}{icon} <b>{esc(inst['label'])}</b>", ""]
    for name in inst["assignees"]:
        if name in inst["done_by"]:
            lines.append(f"✅ {esc(name.capitalize())}")
        else:
            lines.append(f"⏳ {mention(name, state)}")
    if inst["closed"]:
        lines.append("\n🎉 All done.")
    return "\n".join(lines)


def keyboard(inst: dict) -> dict:
    if inst["closed"]:
        return {"inline_keyboard": []}
    return {"inline_keyboard": [[{"text": "✅ Done", "callback_data": f"done:{inst['id']}"}]]}


# ----------------------------------------------------------------- messaging
def post(state: dict, inst: dict, prefix: str = "") -> None:
    """Send an instance to the team group (tagging pending people) + best-effort DMs."""
    group = state["group_chat_id"]
    if group is None:
        return
    msg = tg(
        "sendMessage",
        chat_id=group,
        text=render(inst, state, prefix),
        parse_mode="HTML",
        reply_markup=keyboard(inst),
    )
    if msg:
        inst["messages"].append([group, msg["message_id"]])
    for name in inst["assignees"]:
        user = state["users"].get(name)
        if user and name not in inst["done_by"]:
            # only works if that person has pressed Start in a private chat with the bot
            tg(
                "sendMessage",
                chat_id=user["tg_id"],
                text=f"{prefix}{inst['label']} - please mark it Done in the team group.",
            )


def refresh(state: dict, inst: dict) -> None:
    for chat_id, message_id in inst["messages"]:
        tg(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=render(inst, state),
            parse_mode="HTML",
            reply_markup=keyboard(inst),
        )


def create(state: dict, kind: str, assignees: list, label: str, key: str):
    """Create an item unless one with the same key already exists (prevents duplicates)."""
    if any(i["key"] == key for i in state["instances"].values()):
        return None
    iid = state["next_id"]
    state["next_id"] += 1
    inst = {
        "id": iid,
        "key": key,
        "kind": kind,
        "label": label,
        "assignees": assignees,
        "done_by": [],
        "closed": False,
        "created": today().isoformat(),
        "last_reminded": None,
        "messages": [],
    }
    state["instances"][str(iid)] = inst
    return inst


# ----------------------------------------------------------------- scheduled work
def do_morning(state: dict) -> None:
    d = today()

    # 1) announce today's weekly duty
    name = DUTY_ROTA.get(d.weekday())
    if name:
        inst = create(
            state,
            "duty",
            [name],
            f"Weekly duty - {name.capitalize()} ({d:%a %d %b})",
            key=f"duty:{name}:{d}",
        )
        if inst:
            post(state, inst)

    # 2) re-remind whoever still hasn't finished
    for inst in list(state["instances"].values()):
        if inst["closed"] or inst["last_reminded"] == d.isoformat():
            continue
        created = date.fromisoformat(inst["created"])
        if created >= d:
            continue
        if inst["kind"] == "duty":
            due = True  # every morning until done
        else:
            # cleaning: every Friday morning, starting the week after it was assigned
            due = d.weekday() == FRIDAY and (d - created).days >= 7
        if due:
            post(state, inst, prefix="🔔 Still pending - ")
            inst["last_reminded"] = d.isoformat()


def do_cleaning(state: dict) -> bool:
    d = today()
    week = (d - CLEANING_ANCHOR).days // 7
    pair = CLEANING_PAIRS[week % len(CLEANING_PAIRS)]
    names = " & ".join(n.capitalize() for n in pair)
    inst = create(
        state, "cleaning", list(pair), f"Cleaning - {names} (week of {d:%d %b})", key=f"cleaning:{week}"
    )
    if inst:
        post(state, inst)
    return inst is not None


def run_scheduled(state: dict) -> None:
    if state["group_chat_id"] is None:
        return  # nothing to do until /setgroup
    now = datetime.now(TZ)
    d = now.date()

    if now.time() >= MORNING_TIME and state["last_morning"] != d.isoformat():
        do_morning(state)
        state["last_morning"] = d.isoformat()

    if (
        d.weekday() == THURSDAY
        and now.time() >= CLEANING_TIME
        and state["last_cleaning"] != d.isoformat()
    ):
        do_cleaning(state)
        state["last_cleaning"] = d.isoformat()

    # tidy: forget finished items older than 60 days
    cutoff = (d - timedelta(days=60)).isoformat()
    for iid in [i for i, x in state["instances"].items() if x["closed"] and x["created"] < cutoff]:
        del state["instances"][iid]


# ----------------------------------------------------------------- commands
def schedule_text() -> str:
    days = {6: "Sunday", 1: "Tuesday", 3: "Thursday", 4: "Friday"}
    duty = "\n".join(f"• {days[k]} - {DUTY_ROTA[k].capitalize()}" for k in (6, 1, 3, 4))
    wk = (today() - CLEANING_ANCHOR).days // 7
    this_pair = " & ".join(n.capitalize() for n in CLEANING_PAIRS[wk % 2])
    next_pair = " & ".join(n.capitalize() for n in CLEANING_PAIRS[(wk + 1) % 2])
    return (
        f"Weekly duty (morning {MORNING_TIME:%H:%M}):\n{duty}\n\n"
        f"Cleaning (Thursday {CLEANING_TIME:%H:%M}):\n"
        f"• This week: {this_pair}\n• Next week: {next_pair}"
    )


HELP = (
    "Team rota bot\n\n"
    "Setup:\n"
    "/setgroup - run once inside the team group\n"
    f"/iam <name> - link yourself ({', '.join(NAMES)})\n\n"
    "Info:\n"
    "/schedule - show the rota\n"
    "/pending - open items\n"
    "/team - who is linked\n\n"
    "Manual triggers:\n"
    "/runmorning - today's duty announcement + reminders\n"
    "/runcleaning - this week's cleaning announcement\n\n"
    "Note: I check for messages every few minutes, so replies and Done taps are not instant."
)


def handle_message(state: dict, msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]
    chat = msg["chat"]
    user = msg["from"]

    def reply(t: str) -> None:
        tg("sendMessage", chat_id=chat["id"], text=t)

    if cmd in ("start", "help"):
        reply(HELP)

    elif cmd == "setgroup":
        if chat["type"] == "private":
            reply("Run /setgroup inside the team group.")
            return
        state["group_chat_id"] = chat["id"]
        reply("✅ This group will receive the rota notifications.")

    elif cmd == "iam":
        name = args[0].lower() if args else ""
        if name not in NAMES:
            reply(f"Usage: /iam <name>\nNames: {', '.join(NAMES)}")
            return
        for other, u in list(state["users"].items()):  # one Telegram account = one name
            if u["tg_id"] == user["id"] and other != name:
                del state["users"][other]
        state["users"][name] = {"tg_id": user["id"]}
        reply(f"Linked you as {name.capitalize()} ✅")

    elif cmd == "team":
        lines = [f"{'✅' if n in state['users'] else '❌'} {n.capitalize()}" for n in NAMES]
        reply("Linked members:\n" + "\n".join(lines))

    elif cmd == "schedule":
        reply(schedule_text())

    elif cmd == "pending":
        rows = []
        for inst in state["instances"].values():
            if inst["closed"]:
                continue
            waiting = ", ".join(n.capitalize() for n in inst["assignees"] if n not in inst["done_by"])
            rows.append(f"• {inst['label']}\n   waiting on: {waiting}")
        reply("\n".join(rows) if rows else "Nothing pending 🎉")

    elif cmd == "runmorning":
        if state["group_chat_id"] is None:
            reply("Run /setgroup in the team group first.")
            return
        do_morning(state)
        reply("Morning run done.")

    elif cmd == "runcleaning":
        if state["group_chat_id"] is None:
            reply("Run /setgroup in the team group first.")
            return
        reply("Cleaning announced." if do_cleaning(state) else "This week's cleaning was already announced.")


def handle_callback(state: dict, cq: dict) -> None:
    data = cq.get("data", "")
    if not data.startswith("done:"):
        return
    inst = state["instances"].get(data.split(":")[1])
    uid = cq["from"]["id"]
    name = next((n for n, u in state["users"].items() if u["tg_id"] == uid), None)

    def answer(text: str, alert: bool = False) -> None:
        # may fail if the tap is older than Telegram's answer window - that's fine
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=text, show_alert=alert)

    if not inst:
        answer("Not found.")
        return
    if name is None:
        answer("Send /iam <your name> first.", True)
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        if chat_id:
            tg(
                "sendMessage",
                chat_id=chat_id,
                text=f"{cq['from'].get('first_name', 'Hi')}, send /iam <your name> first so I know who you are.",
            )
        return
    if name not in inst["assignees"]:
        answer("This one isn't yours 🙂", True)
        return
    if name in inst["done_by"]:
        answer("Already marked done.")
        return

    inst["done_by"].append(name)
    inst["closed"] = set(inst["assignees"]) <= set(inst["done_by"])
    answer("Marked as done ✅")
    refresh(state, inst)


# ----------------------------------------------------------------- entry point
def main() -> None:
    state = load()
    original = json.dumps(state, sort_keys=True)

    updates = (
        tg("getUpdates", offset=state["offset"], timeout=0, allowed_updates=["message", "callback_query"])
        or []
    )
    for u in updates:
        state["offset"] = u["update_id"] + 1
        try:
            if "message" in u:
                handle_message(state, u["message"])
            elif "callback_query" in u:
                handle_callback(state, u["callback_query"])
        except Exception:
            log.exception("failed to handle update %s", u.get("update_id"))

    run_scheduled(state)
    save(state, original)
    log.info("run complete (%d updates)", len(updates))


if __name__ == "__main__":
    main()
