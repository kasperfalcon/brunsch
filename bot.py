"""
Bot Telegram simplu de pontaj (Check-in / Check-out) cu confirmare prin
locație și curățare automată a mesajelor din chat.

Flux:
  1. Meniul auxiliar de lângă bara de text are mereu 2 butoane:
     "✅ Чек-ин" / "🔴 Чек-аут" (nu e nevoie de nicio comandă pentru asta —
     tastatura e persistentă, o dată setată rămâne).
  2. Userul apasă un buton -> mesajul lui e șters instant -> botul cere
     locația ("Trimite locația").
  3. Userul trimite locația:
       - dacă e greșită (prea departe) -> se șterge mesajul cu locația,
         apare "❌ Locație greșită", userul trimite din nou.
       - dacă e corectă -> se șterg TOATE mesajele intermediare (cererea de
         locație, mesajele de "locație greșită", locația în sine) și rămâne
         un singur mesaj: "(nume) Checked In".
  4. La Check-out se repetă pasul 2-3, iar la final se șterg mesajele
     intermediare + mesajul de "Checked In" de la check-in, rămânând un
     singur mesaj final cu orele lucrate.
  5. Orice user poate ponta, indiferent dacă apare sau nu într-o listă —
     nu există nicio verificare de program/orar.
  6. Dacă apare orice eroare, mesajele intermediare se șterg (nu rămâne
     "gunoi" în chat).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from math import radians, sin, cos, sqrt, asin
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Aici reținem ID-urile tuturor chat-urilor/grupurilor în care botul a fost
# folosit vreodată — ca la fiecare pornire să poată trimite tastatura peste
# tot, nu doar într-un singur chat fixat (vezi ALLOWED_CHAT_ID mai jos).
KNOWN_CHATS_FILE = Path(__file__).resolve().parent / "known_chats.json"


def get_env(name: str, default=None):
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
    return value


BOT_TOKEN = get_env("BOT_TOKEN")
SPREADSHEET_ID = get_env("SPREADSHEET_ID")
SHEET_NAME = get_env("SHEET_NAME", "Sheet1")
SUMMARY_SHEET_NAME = get_env("SUMMARY_SHEET_NAME", "Sumar Ore")
CREDENTIALS_FILE = get_env("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# (opțional) restricționează botul la un singur grup / un singur topic (forum)
ALLOWED_CHAT_ID = get_env("ALLOWED_CHAT_ID")
if ALLOWED_CHAT_ID:
    ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID)
ALLOWED_TOPIC_ID = get_env("ALLOWED_TOPIC_ID")
if ALLOWED_TOPIC_ID:
    ALLOWED_TOPIC_ID = int(ALLOWED_TOPIC_ID)

# Locația localului — pontarea e permisă doar în raza asta
LOCATION_LAT = float(get_env("LOCATION_LAT", "47.020641"))
LOCATION_LON = float(get_env("LOCATION_LON", "28.820717"))
LOCATION_RADIUS_METERS = float(get_env("LOCATION_RADIUS_METERS", "150"))

# Dacă userul nu trimite locația în X minute după ce a apăsat butonul,
# cererea se anulează automat (ca să nu rămână "agățată").
PENDING_TIMEOUT_MINUTES = int(get_env("PENDING_TIMEOUT_MINUTES", "10") or 10)

if not BOT_TOKEN:
    raise SystemExit("Не найден BOT_TOKEN в .env — скопируй .env.example в .env и заполни его.")
if not SPREADSHEET_ID:
    raise SystemExit("Не найден SPREADSHEET_ID в .env — скопируй .env.example в .env и заполни его.")

LOCAL_TZ = timezone(timedelta(hours=3))  # Chișinău vara = UTC+3

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

_SPREADSHEET_CACHE = {"spreadsheet": None}
_WORKSHEET_CACHE = {"worksheet": None}
_SUMMARY_WORKSHEET_CACHE = {"worksheet": None}


def get_spreadsheet():
    if _SPREADSHEET_CACHE["spreadsheet"] is not None:
        return _SPREADSHEET_CACHE["spreadsheet"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    _SPREADSHEET_CACHE["spreadsheet"] = spreadsheet
    return spreadsheet


def get_worksheet():
    if _WORKSHEET_CACHE["worksheet"] is not None:
        return _WORKSHEET_CACHE["worksheet"]
    spreadsheet = get_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=6)
        worksheet.append_row(["Data", "Ora", "Nume", "Tip", "Mesaj original"])
    _WORKSHEET_CACHE["worksheet"] = worksheet
    return worksheet


def append_row_safe(row) -> bool:
    try:
        get_worksheet().append_row(row)
        return True
    except Exception:
        logger.exception("Eroare la scrierea în Google Sheets")
        return False


def get_summary_worksheet():
    if _SUMMARY_WORKSHEET_CACHE["worksheet"] is not None:
        return _SUMMARY_WORKSHEET_CACHE["worksheet"]
    spreadsheet = get_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(SUMMARY_SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows=1000, cols=6)
        worksheet.append_row(["Data", "Nume", "Check-in", "Check-out", "Ore lucrate"])
    _SUMMARY_WORKSHEET_CACHE["worksheet"] = worksheet
    return worksheet


def append_summary_row(data_str, name, checkin_ora, checkout_ora, worked_hours) -> bool:
    try:
        get_summary_worksheet().append_row(
            [data_str, name, checkin_ora, checkout_ora, round(worked_hours, 2)]
        )
        return True
    except Exception:
        # Nu blocăm check-out-ul dacă doar "Sumar Ore" nu s-a putut scrie —
        # logul brut din Sheet1 e deja salvat, deci nu se pierde nimic.
        logger.exception('Eroare la scrierea în tab-ul "Sumar Ore"')
        return False


def get_open_checkin(name: str):
    """Verifică în Sheets (nu doar în memorie) dacă persoana are un check-in
    deschis, nepereche cu niciun check-out — adică ultimul ei eveniment
    înregistrat e un "Check-in". Se uită pe TOT istoricul (nu doar azi), ca
    să prindă și schimburi care trec de miezul nopții.

    Returnează (data, ora) dacă există un check-in deschis, altfel None.
    Dacă citirea din Sheets eșuează, ridică excepția mai departe — apelantul
    decide cum tratează cazul (nu ghicim silențios "nu are check-in")."""
    rows = get_worksheet().get_all_records()
    expected_name = normalize_text(name)
    entries = []
    for row in rows:
        if normalize_text(row.get("Nume")) != expected_name:
            continue
        tip = normalize_text(row.get("Tip"))
        if tip not in ("check-in", "check-out"):
            continue
        data_val = str(row.get("Data") or "").strip()
        ora_val = str(row.get("Ora") or "").strip()
        if not data_val or not ora_val:
            continue
        entries.append((data_val, ora_val, tip))
    if not entries:
        return None
    entries.sort(key=lambda e: (e[0], e[1]))
    last_data, last_ora, last_tip = entries[-1]
    if last_tip == "check-in":
        return last_data, last_ora
    return None


def normalize_text(value):
    return str(value or "").strip().lower()


# ---------------------------------------------------------------------------
# Tastatura persistentă (meniul auxiliar de lângă bara de text)
# ---------------------------------------------------------------------------

CHECKIN_LABEL = "✅ Чек-ин"
CHECKOUT_LABEL = "🔴 Чек-аут"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[CHECKIN_LABEL, CHECKOUT_LABEL]],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=False,
)

# ---------------------------------------------------------------------------
# Stare în memorie
# ---------------------------------------------------------------------------

# user_id -> {"type": "Check-in"/"Check-out", "chat_id": int, "name": str,
#             "message_ids": [id, ...], "timeout_job": Job}
PENDING = {}

# user_id -> {"message_id": int, "chat_id": int, "name": str, "dt": datetime, "ora": str}
ACTIVE_CHECKIN = {}

# chat_id -> [message_id, ...] — mesaje "efemere" (avertizări, anunțuri) care
# trebuie șterse la următorul mesaj nou din chat, NU după un timer fix.
EPHEMERAL_MESSAGES = {}


def mark_ephemeral(chat_id, message_id) -> None:
    """Marchează un mesaj temporar (avertizare/anunț) pentru ștergere —
    va fi șters imediat ce apare orice alt mesaj nou în acel chat."""
    if message_id is None:
        return
    EPHEMERAL_MESSAGES.setdefault(chat_id, []).append(message_id)


async def ensure_active_checkin_state(user_id, name, chat_id):
    """Confirmă dacă persoana are un check-in activ — verificând ÎNTÂI
    memoria (rapid), iar dacă lipsește de-acolo (de regulă pentru că botul
    a fost repornit), verifică direct în Google Sheets, sursa de adevăr,
    și reconstruiește starea în memorie dacă găsește un check-in deschis.

    Returnează True / False, sau None dacă citirea din Sheets a eșuat și
    nu se poate confirma nimic sigur."""
    if user_id in ACTIVE_CHECKIN:
        return True
    try:
        open_checkin = get_open_checkin(name)
    except Exception:
        logger.exception("Eroare la verificarea check-in-ului activ în Sheets pentru %s", name)
        return None
    if not open_checkin:
        return False
    open_data, open_ora = open_checkin
    try:
        open_dt = datetime.combine(
            datetime.strptime(open_data, "%Y-%m-%d").date(),
            datetime.strptime(open_ora, "%H:%M:%S").time(),
            tzinfo=LOCAL_TZ,
        )
    except Exception:
        open_dt = None
    ACTIVE_CHECKIN[user_id] = {
        "message_id": None,  # nu avem mesajul original de "Checked In" (botul a fost repornit între timp)
        "chat_id": chat_id,
        "name": name,
        "dt": open_dt,
        "ora": open_ora,
    }
    return True


def load_known_chats() -> set:
    try:
        with open(KNOWN_CHATS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def save_known_chats(chats: set) -> None:
    try:
        with open(KNOWN_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(chats), f)
    except Exception:
        logger.exception("Nu am putut salva known_chats.json")


KNOWN_CHATS = load_known_chats()


def remember_chat(chat_id: int) -> None:
    """Reține un chat ca fiind unul în care botul a fost folosit, ca la
    următoarea pornire să-i trimită automat tastatura și acolo."""
    if chat_id in KNOWN_CHATS:
        return
    KNOWN_CHATS.add(chat_id)
    save_known_chats(KNOWN_CHATS)


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return R * c


async def delete_now(bot, chat_id, message_id):
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        # Cea mai frecventă cauză: botul nu e admin în grup (sau nu are
        # dreptul "Delete messages") — un bot fără drepturi de admin poate
        # șterge doar mesajele proprii, nu și pe ale altor useri.
        logger.warning("Nu am putut șterge mesajul %s din chat %s: %s", message_id, chat_id, exc)


async def delete_many(bot, chat_id, message_ids):
    for mid in message_ids:
        await delete_now(bot, chat_id, mid)


def _chat_allowed(update: Update) -> bool:
    if ALLOWED_CHAT_ID and update.effective_chat.id != ALLOWED_CHAT_ID:
        return False
    if ALLOWED_TOPIC_ID:
        msg = update.effective_message
        if msg is not None and getattr(msg, "message_thread_id", None) != ALLOWED_TOPIC_ID:
            return False
    return True


async def _job_pending_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dacă userul nu trimite locația în timp util, anulăm cererea și
    curățăm mesajele, ca să nu rămână o stare "agățată"."""
    data = context.job.data
    user_id = data["user_id"]
    pending = PENDING.get(user_id)
    if not pending:
        return
    PENDING.pop(user_id, None)
    await delete_many(context.bot, pending["chat_id"], pending["message_ids"])


def _start_pending_timeout(context, user_id):
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None:
        return None
    return job_queue.run_once(
        _job_pending_timeout,
        when=PENDING_TIMEOUT_MINUTES * 60,
        data={"user_id": user_id},
    )


# ---------------------------------------------------------------------------
# Comenzi
# ---------------------------------------------------------------------------

async def send_main_keyboard(bot, chat_id, thread_id=None):
    """Trimite (sau re-afișează) tastatura persistentă de pontaj într-un chat."""
    return await bot.send_message(
        chat_id=chat_id,
        text="Meniul de pontaj e activ — folosește butoanele de lângă bara de text.",
        reply_markup=MAIN_KEYBOARD,
        message_thread_id=thread_id,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setează (sau re-afișează) manual tastatura persistentă de pontaj.
    Nu mai e obligatoriu — tastatura se trimite acum automat la pornirea
    botului — dar comanda rămâne utilă dacă cineva a ascuns tastatura din
    greșeală sau a intrat nou în grup înainte ca botul să fi fost repornit."""
    if not _chat_allowed(update):
        return
    chat_id = update.effective_chat.id
    if update.message is not None:
        await delete_now(context.bot, chat_id, update.message.message_id)
    msg = await send_main_keyboard(context.bot, chat_id, thread_id=ALLOWED_TOPIC_ID)
    mark_ephemeral(chat_id, msg.message_id)


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Când intră cineva nou în grup, re-afișăm tastatura, ca s-o vadă și ei
    direct, fără să aștepte un /start manual."""
    if not _chat_allowed(update):
        return
    msg = update.message
    if msg is None or not msg.new_chat_members:
        return
    chat_id = msg.chat.id
    remember_chat(chat_id)
    await delete_now(context.bot, chat_id, msg.message_id)
    keyboard_msg = await send_main_keyboard(context.bot, chat_id, thread_id=ALLOWED_TOPIC_ID)
    mark_ephemeral(chat_id, keyboard_msg.message_id)


async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rulează silențios pe orice mesaj primit — reține chat-ul (necesar
    când ALLOWED_CHAT_ID e gol, modul "orice chat") și șterge orice mesaje
    efemere rămase din interacțiunea anterioară (avertizări, anunțuri),
    declanșat de sosirea acestui mesaj nou, nu de un timer."""
    chat = update.effective_chat
    if chat is None:
        return
    remember_chat(chat.id)
    pending_cleanup = EPHEMERAL_MESSAGES.pop(chat.id, None)
    if pending_cleanup:
        await delete_many(context.bot, chat.id, pending_cleanup)


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Când botul e adăugat într-un grup nou, trimite imediat tastatura,
    fără să aștepte un restart (relevant doar în modul "orice chat")."""
    result = update.my_chat_member
    if result is None:
        return
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status
    became_member = new_status in ("member", "administrator") and old_status not in (
        "member",
        "administrator",
    )
    if not became_member:
        return
    chat_id = result.chat.id
    remember_chat(chat_id)
    if ALLOWED_CHAT_ID:
        return  # în modul cu chat fixat, nu trimitem automat în alte chat-uri noi
    try:
        await send_main_keyboard(context.bot, chat_id)
    except Exception:
        logger.exception("Nu am putut trimite tastatura la adăugarea în chat-ul %s", chat_id)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid — arată ID-ul chat-ului curent (și al topicului, dacă e cazul),
    ca să poți completa ALLOWED_CHAT_ID în .env fără să cauți prin loguri."""
    chat = update.effective_chat
    msg = update.effective_message
    thread_id = getattr(msg, "message_thread_id", None) if msg else None
    text = f"🆔 Chat ID: `{chat.id}`"
    if thread_id:
        text += f"\n🧵 Topic ID: `{thread_id}`"
    await context.bot.send_message(
        chat_id=chat.id, text=text, parse_mode="Markdown", message_thread_id=thread_id
    )


async def cmd_ore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ore — arată orele lucrate în luna curentă pentru cel care scrie comanda."""
    if not _chat_allowed(update):
        return
    user = update.effective_user
    name = user.full_name if user else "Necunoscut"
    now = datetime.now(LOCAL_TZ)
    month_prefix = now.strftime("%Y-%m")
    try:
        rows = get_worksheet().get_all_records()
    except Exception:
        logger.exception("Eroare la citirea din Sheets pentru /ore")
        await update.effective_chat.send_message("⚠️ Eroare la citirea din Sheets, încearcă din nou.")
        return

    entries = []
    for row in rows:
        if normalize_text(row.get("Nume")) != normalize_text(name):
            continue
        date_val = str(row.get("Data") or "").strip()
        time_val = str(row.get("Ora") or "").strip()
        tip = normalize_text(row.get("Tip"))
        if tip not in ("check-in", "check-out") or not date_val or not time_val:
            continue
        try:
            dt = datetime.strptime(f"{date_val} {time_val}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        entries.append((dt, tip))
    entries.sort(key=lambda e: e[0])

    total_seconds = 0
    open_checkin = None
    incomplete = 0
    for dt, tip in entries:
        if tip == "check-in":
            if open_checkin is not None:
                incomplete += 1
            open_checkin = dt
        elif tip == "check-out":
            if open_checkin is None:
                continue
            if open_checkin.strftime("%Y-%m") == month_prefix:
                total_seconds += (dt - open_checkin).total_seconds()
            open_checkin = None
    if open_checkin is not None:
        incomplete += 1

    total_hours = total_seconds / 3600.0
    text = f"📊 {name} — luna {month_prefix}: {total_hours:.1f} ore pontate."
    if incomplete:
        text += f"\n⚠️ {incomplete} tură(e) fără pereche check-in/checkout (nu sunt incluse)."
    await update.effective_chat.send_message(text)


# ---------------------------------------------------------------------------
# Apăsare buton (Check-in / Check-out)
# ---------------------------------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or not msg.text:
        return
    text = msg.text.strip()
    if text not in (CHECKIN_LABEL, CHECKOUT_LABEL):
        return
    if not _chat_allowed(update):
        return

    chat_id = msg.chat.id
    user = msg.from_user
    user_id = user.id if user else None
    name = user.full_name if user else "Necunoscut"

    # Ștergem instant mesajul cu textul butonului apăsat — nu trebuie să
    # rămână vizibil, doar rezultatul final contează.
    await delete_now(context.bot, chat_id, msg.message_id)

    entry_type = "Check-in" if text == CHECKIN_LABEL else "Check-out"

    has_active = await ensure_active_checkin_state(user_id, name, chat_id)

    if has_active is None:
        warn = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name}, nu am putut verifica în Sheets dacă ai un check-in activ (eroare temporară) — încearcă din nou peste puțin timp.",
            message_thread_id=ALLOWED_TOPIC_ID,
        )
        mark_ephemeral(chat_id, warn.message_id)
        return
    if entry_type == "Check-in" and has_active:
        warn = await context.bot.send_message(
            chat_id=chat_id, text=f"{name}, ai deja un check-in activ.", message_thread_id=ALLOWED_TOPIC_ID,
        )
        mark_ephemeral(chat_id, warn.message_id)
        return
    if entry_type == "Check-out" and not has_active:
        warn = await context.bot.send_message(
            chat_id=chat_id, text=f"{name}, nu ai niciun check-in activ.", message_thread_id=ALLOWED_TOPIC_ID,
        )
        mark_ephemeral(chat_id, warn.message_id)
        return
    if user_id in PENDING:
        reminder = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name}, ai deja o cerere de locație activă — trimite locația pentru a finaliza.",
            message_thread_id=ALLOWED_TOPIC_ID,
        )
        mark_ephemeral(chat_id, reminder.message_id)
        return

    prompt = await context.bot.send_message(
        chat_id=chat_id,
        text=f"📍 {name}, trimite locația ta (agrafa de atașare → Location) pentru a confirma {entry_type.lower()}.",
        message_thread_id=ALLOWED_TOPIC_ID,
    )
    PENDING[user_id] = {
        "type": entry_type,
        "chat_id": chat_id,
        "name": name,
        "message_ids": [prompt.message_id],
    }
    PENDING[user_id]["timeout_job"] = _start_pending_timeout(context, user_id)


# ---------------------------------------------------------------------------
# Primire locație
# ---------------------------------------------------------------------------

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or not msg.location:
        return
    if not _chat_allowed(update):
        return

    user = msg.from_user
    user_id = user.id if user else None
    chat_id = msg.chat.id

    pending = PENDING.get(user_id)
    if not pending:
        # Nicio cerere activă pentru userul ăsta — ignorăm silențios, ca să
        # nu facem spam în chat pentru o locație trimisă fără context.
        return

    pending["message_ids"].append(msg.message_id)

    loc = msg.location
    dist = haversine_distance(LOCATION_LAT, LOCATION_LON, loc.latitude, loc.longitude)

    if dist > LOCATION_RADIUS_METERS:
        await delete_now(context.bot, chat_id, msg.message_id)
        warn = await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ {pending['name']}, locație greșită ({int(dist)} m distanță). Trimite din nou locația corectă.",
            message_thread_id=ALLOWED_TOPIC_ID,
        )
        pending["message_ids"].append(warn.message_id)
        return

    # Locație corectă -> finalizăm pontajul
    entry_type = pending["type"]
    name = pending["name"]
    now = datetime.now(LOCAL_TZ)
    data_str = now.strftime("%Y-%m-%d")
    ora_str = now.strftime("%H:%M:%S")

    job = pending.pop("timeout_job", None)
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass

    saved = append_row_safe([data_str, ora_str, name, entry_type, "location"])
    if not saved:
        await delete_many(context.bot, chat_id, pending["message_ids"])
        PENDING.pop(user_id, None)
        err = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {name}, eroare la salvare — încearcă din nou.",
            message_thread_id=ALLOWED_TOPIC_ID,
        )
        mark_ephemeral(chat_id, err.message_id)
        return

    # Ștergem toate mesajele intermediare (cerere locație, locații greșite, locația finală)
    await delete_many(context.bot, chat_id, pending["message_ids"])
    PENDING.pop(user_id, None)

    if entry_type == "Check-in":
        confirmation = await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ {name} — Checked In — {ora_str}",
            message_thread_id=ALLOWED_TOPIC_ID,
        )
        ACTIVE_CHECKIN[user_id] = {
            "message_id": confirmation.message_id,
            "chat_id": chat_id,
            "name": name,
            "dt": now,
            "ora": ora_str,
        }
        return

    # Check-out
    active = ACTIVE_CHECKIN.pop(user_id, None)
    if active:
        await delete_now(context.bot, active.get("chat_id", chat_id), active.get("message_id"))
        checkin_dt = active.get("dt")
        checkin_ora = active.get("ora")
    else:
        # Rar: botul a fost repornit exact între apăsarea butonului și
        # trimiterea locației — recitim din Sheets ca ultimă soluție.
        checkin_ora = None
        checkin_dt = None
        try:
            open_checkin = get_open_checkin(name)
        except Exception:
            logger.exception("Eroare la citirea din Sheets pentru fallback checkout")
            open_checkin = None
        if open_checkin:
            open_data, checkin_ora = open_checkin
            try:
                checkin_dt = datetime.combine(
                    datetime.strptime(open_data, "%Y-%m-%d").date(),
                    datetime.strptime(checkin_ora, "%H:%M:%S").time(),
                    tzinfo=LOCAL_TZ,
                )
            except Exception:
                checkin_dt = None

    if checkin_dt is not None:
        worked_hours = (now - checkin_dt).total_seconds() / 3600.0
        text = f"🏁 {name} — {checkin_ora} – {ora_str} ({worked_hours:.1f} ore lucrate)"
        append_summary_row(data_str, name, checkin_ora, ora_str, worked_hours)
    else:
        text = f"🏁 {name} — Check-out — {ora_str} (nu am găsit ora de check-in pentru calcul)"

    await context.bot.send_message(chat_id=chat_id, text=text, message_thread_id=ALLOWED_TOPIC_ID)


async def _post_init(application: Application) -> None:
    """Trimite automat tastatura persistentă la pornirea botului, ca să nu
    mai fie nevoie ca cineva să scrie /start manual.

    - Dacă ALLOWED_CHAT_ID e completat: trimite doar acolo (mod chat fixat).
    - Dacă ALLOWED_CHAT_ID e gol: trimite în TOATE chat-urile/grupurile în
      care botul a mai fost folosit vreodată (reținute în known_chats.json).
    """
    if ALLOWED_CHAT_ID:
        try:
            msg = await send_main_keyboard(application.bot, ALLOWED_CHAT_ID, thread_id=ALLOWED_TOPIC_ID)
            logger.info("Tastatura de pontaj a fost trimisă automat la pornire (mesaj %s).", msg.message_id)
            mark_ephemeral(ALLOWED_CHAT_ID, msg.message_id)
        except Exception:
            logger.exception("Nu am putut trimite automat tastatura la pornire")
        return

    if not KNOWN_CHATS:
        logger.warning(
            "Niciun chat cunoscut încă (known_chats.json e gol) — tastatura NU se trimite automat. "
            "Scrie /start (sau orice mesaj) o dată în fiecare grup/chat ca botul să-l rețină; "
            "de atunci încolo va primi tastatura automat la fiecare pornire."
        )
        return

    sent, failed = 0, 0
    for chat_id in list(KNOWN_CHATS):
        try:
            msg = await send_main_keyboard(application.bot, chat_id)
            mark_ephemeral(chat_id, msg.message_id)
            sent += 1
        except Exception:
            failed += 1
            logger.warning("Nu am putut trimite tastatura în chat-ul %s (posibil botul a fost scos de acolo)", chat_id)
    logger.info("Tastatura de pontaj trimisă automat în %d chat-uri cunoscute (%d eșuate).", sent, failed)


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("meniu", cmd_start))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler("ore", cmd_ore))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    application.add_handler(MessageHandler(filters.Regex(rf'^(?:{CHECKIN_LABEL}|{CHECKOUT_LABEL})$'), handle_button))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    # group=-1 => rulează pe orice update, în paralel cu handlerele de mai sus,
    # doar ca să reținem chat-ul (necesar pentru modul "orice chat", fără ALLOWED_CHAT_ID)
    application.add_handler(MessageHandler(filters.ALL, track_chat), group=-1)
    application.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))

    if application.job_queue is None:
        logger.warning(
            "JobQueue nu e disponibil — instalează cu: pip install \"python-telegram-bot[job-queue]\""
        )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio

    try:
        # Python 3.14 nu mai creează automat un event loop în thread-ul
        # principal — trebuie creat explicit înainte ca run_polling()
        # (din python-telegram-bot) să-l caute intern.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        main()
    except KeyboardInterrupt:
        pass