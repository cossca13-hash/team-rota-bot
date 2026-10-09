"""Team rota bot for Telegram.

Two rolling weekly activities:

1. WEEKLY DUTY  - Sun: Misge, Tue: Eden, Thu: Kal, Fri: Mhret
   * Announced in the morning on the person's day.
   * If the person hasn't tapped Done, THEY get re-pinged every following
     morning (only them - not whoever's day it is) until they tap Done.

2. CLEANING     - pairs alternate weekly (Kal+Misge, then Eden+Mhret, ...)
   * Announced every Thursday night to that week's pair.
   * Anyone in the pair who hasn't tapped Done is re-pinged every following
     Friday morning (starting the next week) until they do.

Setup: add the bot to the team group, send /setgroup there, and have each
person send /iam <name> once so the bot can tag them.
"""
import html
import json
import logging
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ----------------------------------------------------------------- config
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dubai"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "data.json"))


def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m), tzinfo=TZ)


MORNING_TIME = hhmm(os.environ.get("MORNING_TIME", "08:00"))
CLEANING_TIME = hhmm(os.environ.get("CLEANING_TIME", "20:00"))  # Thursday night

# Python weekday(): Monday=0 ... Sunday=6
DUTY_ROTA = {6: "misge", 1: "eden", 3: "kal", 4: "mhret"}
CLEANING_PAIRS = [["kal", "misge"], ["eden", "mhret"]]
# The Thursday whose week uses pair #0 (Kal + Misge). Pairs alternate weekly from here.
CLEANING_ANCHOR = date(2026, 10, 8)
THURSDAY, FRIDAY = 3, 4

NAMES = sorted(set(DUTY_ROTA.values()) | {n for p in CLEANING_PAIRS for n in p})

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rotabot")


# ----------------------------------------------------------------- storage
def load() -> dict:
    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}
    data.setdefault("group_chat_id", None)
    data.setdefault("users", {})  # name -> {"tg_id": int}
    data.setdefault("instances", {})  # id -> instance
    data.setdefault("next_id", 1)
    return data


def save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


def today() -> date:
    return datetime.now(TZ).date()


# ----------------------------------------------------------------- rendering
def esc(s: str) -> str:
    return html.escape(s)


def mention(name: str, data: dict) -> str:
    user = data["users"].get(name)
    if user:
        return f'<a href="tg://user?id={user["tg_id"]}">{esc(name.capitalize())}</a>'
    return f"{esc(name.capitalize())} (not linked yet)"


def render(inst: dict, data: dict, prefix: str = "") -> str:
    icon = "🧹" if inst["kind"] == "cleaning" else "📌"
    lines = [f"{prefix}{icon} <b>{esc(inst['label'])}</b>", ""]
    for name in inst["assignees"]:
        if name in inst["done_by"]:
            lines.append(f"✅ {esc(name.capitalize())}")
        else:
            lines.append(f"⏳ {mention(name, data)}")
    if inst["closed"]:
        lines.append("\n🎉 All done.")
    return "\n".join(lines)


def keyboard(inst: dict):
    if inst["closed"]:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Done", callback_data=f"done:{inst['id']}")]]
    )


# ----------------------------------------------------------------- messaging
async def post(context, data: dict, inst: dict, prefix: str = "") -> None:
    """Send the instance to the team group (tagging pending people) + best-effort DMs."""
    group = data["group_chat_id"]
    if group is None:
        log.warning("No group set - send /setgroup in the team group.")
        return
    msg = await context.bot.send_message(
        group,
        render(inst, data, prefix),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard(inst),
    )
    inst["messages"].append([group, msg.message_id])
    for name in inst["assignees"]:
        user = data["users"].get(name)
        if user and name not in inst["done_by"]:
            try:  # works only if the person has started a private chat with the bot
                await context.bot.send_message(
                    user["tg_id"],
                    f"{prefix}{inst['label']} - please mark it Done in the team group.",
                )
            except Exception:
                pass


async def refresh(context, data: dict, inst: dict) -> None:
    for chat_id, message_id in inst["messages"]:
        try:
            await context.bot.edit_message_text(
                render(inst, data),
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard(inst),
            )
        except Exception:
            pass  # message deleted / unchanged


def create(data: dict, kind: str, assignees: list, label: str, key: str):
    """Create an instance unless one with the same key already exists."""
    if any(i["key"] == key for i in data["instances"].values()):
        return None
    iid = data["next_id"]
    data["next_id"] += 1
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
    data["instances"][str(iid)] = inst
    return inst


# ----------------------------------------------------------------- scheduled work
async def do_morning(context) -> None:
    data = load()
    d = today()

    # 1) announce today's weekly duty
    name = DUTY_ROTA.get(d.weekday())
    if name:
        inst = create(
            data,
            "duty",
            [name],
            f"Weekly duty - {name.capitalize()} ({d:%a %d %b})",
            key=f"duty:{name}:{d}",
        )
        if inst:
            await post(context, data, inst)

    # 2) re-remind whoever still hasn't finished
    for inst in data["instances"].values():
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
            await post(context, data, inst, prefix="🔔 Still pending - ")
            inst["last_reminded"] = d.isoformat()

    save(data)


async def do_cleaning(context) -> bool:
    data = load()
    d = today()
    week = (d - CLEANING_ANCHOR).days // 7
    pair = CLEANING_PAIRS[week % len(CLEANING_PAIRS)]
    names = " & ".join(n.capitalize() for n in pair)
    inst = create(data, "cleaning", list(pair), f"Cleaning - {names} (week of {d:%d %b})", key=f"cleaning:{week}")
    if inst:
        await post(context, data, inst)
    save(data)
    return inst is not None


async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    await do_morning(context)


async def cleaning_job(context: ContextTypes.DEFAULT_TYPE):
    if today().weekday() == THURSDAY:
        await do_cleaning(context)


# ----------------------------------------------------------------- commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Team rota bot\n\n"
        "Setup:\n"
        "/setgroup - run once inside the team group\n"
        f"/iam <name> - link yourself ({', '.join(NAMES)})\n\n"
        "Info:\n"
        "/schedule - show the rota\n"
        "/pending - open items\n"
        "/team - who is linked\n\n"
        "Manual triggers (e.g. if the bot was offline):\n"
        "/runmorning - today's duty announcement + reminders\n"
        "/runcleaning - this week's cleaning announcement"
    )


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run /setgroup inside the team group.")
        return
    data = load()
    data["group_chat_id"] = update.effective_chat.id
    save(data)
    await update.message.reply_text("✅ This group will receive the rota notifications.")


async def iam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.args[0].lower() if context.args else ""
    if name not in NAMES:
        await update.message.reply_text(f"Usage: /iam <name>\nNames: {', '.join(NAMES)}")
        return
    data = load()
    uid = update.effective_user.id
    for other, u in list(data["users"].items()):  # one Telegram account = one name
        if u["tg_id"] == uid and other != name:
            del data["users"][other]
    data["users"][name] = {"tg_id": uid}
    save(data)
    await update.message.reply_text(f"Linked you as {name.capitalize()} ✅")


async def team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load()
    lines = [f"{'✅' if n in data['users'] else '❌'} {n.capitalize()}" for n in NAMES]
    await update.message.reply_text("Linked members:\n" + "\n".join(lines))


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = {6: "Sunday", 1: "Tuesday", 3: "Thursday", 4: "Friday"}
    duty = "\n".join(f"• {days[k]} - {DUTY_ROTA[k].capitalize()}" for k in (6, 1, 3, 4))
    wk = (today() - CLEANING_ANCHOR).days // 7
    this_pair = " & ".join(n.capitalize() for n in CLEANING_PAIRS[wk % 2])
    next_pair = " & ".join(n.capitalize() for n in CLEANING_PAIRS[(wk + 1) % 2])
    await update.message.reply_text(
        f"Weekly duty (morning {MORNING_TIME:%H:%M}):\n{duty}\n\n"
        f"Cleaning (Thursday {CLEANING_TIME:%H:%M}):\n"
        f"• This week: {this_pair}\n• Next week: {next_pair}"
    )


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load()
    rows = []
    for inst in data["instances"].values():
        if inst["closed"]:
            continue
        waiting = ", ".join(n.capitalize() for n in inst["assignees"] if n not in inst["done_by"])
        rows.append(f"• {inst['label']}\n   waiting on: {waiting}")
    await update.message.reply_text("\n".join(rows) if rows else "Nothing pending 🎉")


async def runmorning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_morning(context)
    await update.message.reply_text("Morning run done.")


async def runcleaning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    created = await do_cleaning(context)
    await update.message.reply_text(
        "Cleaning announced." if created else "This week's cleaning was already announced."
    )


# ----------------------------------------------------------------- Done button
async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    iid = q.data.split(":")[1]
    data = load()
    inst = data["instances"].get(iid)
    if not inst:
        await q.answer("Not found.")
        return

    uid = q.from_user.id
    name = next((n for n, u in data["users"].items() if u["tg_id"] == uid), None)
    if name is None:
        await q.answer("Send /iam <your name> first.", show_alert=True)
        return
    if name not in inst["assignees"]:
        await q.answer("This one isn't yours 🙂", show_alert=True)
        return
    if name in inst["done_by"]:
        await q.answer("Already marked done.")
        return

    inst["done_by"].append(name)
    inst["closed"] = set(inst["assignees"]) <= set(inst["done_by"])
    save(data)
    await q.answer("Marked as done ✅")
    await refresh(context, data, inst)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("iam", iam))
    app.add_handler(CommandHandler("team", team))
    app.add_handler(CommandHandler("schedule", schedule))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("runmorning", runmorning))
    app.add_handler(CommandHandler("runcleaning", runcleaning))
    app.add_handler(CallbackQueryHandler(on_done, pattern=r"^done:"))

    app.job_queue.run_daily(morning_job, MORNING_TIME, name="morning")
    app.job_queue.run_daily(cleaning_job, CLEANING_TIME, name="cleaning")
    log.info("Rota bot running (tz=%s)", TZ)
    app.run_polling()


if __name__ == "__main__":
    main()
