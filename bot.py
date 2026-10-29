import asyncio
import logging
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

load_dotenv(".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "prayer.db")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Tunis"))
METHOD = os.getenv("METHOD", "18")  # 18 = وزارة الشؤون الدينية التونسية
DEFAULT_BEFORE = int(os.getenv("DEFAULT_BEFORE_MIN", "10"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("prayer-bot")

# البلديات التونسية: تُحمّل حدودها الجغرافية عند التشغيل، ثم تُقدّر إحداثيات كل بلدية
# من منتصف صندوق حدودها. المصدر الجغرافي مفتوح البيانات (نسخة 2018)، وقد لا يشمل
# البلديات التي أُحدثت بعد ذلك.
MUNICIPALITIES_URL = (
    "https://data-dev.nadhour.org/s/resources/"
    "decoupage-de-la-tunisie-en-municipalites-geojson/20180504-160551/Municipalites.geojson"
)

FALLBACK_GOVERNORATES = [
    ("تونس", 36.8065, 10.1815), ("أريانة", 36.8665, 10.1647),
    ("بن عروس", 36.7531, 10.2189), ("منوبة", 36.8101, 10.0863),
    ("نابل", 36.4561, 10.7376), ("بنزرت", 37.2744, 9.8739),
    ("باجة", 36.7256, 9.1817), ("جندوبة", 36.5011, 8.7802),
    ("الكاف", 36.1826, 8.7148), ("سليانة", 36.0850, 9.3708),
    ("زغوان", 36.4029, 10.1429), ("سوسة", 35.8256, 10.6369),
    ("المنستير", 35.7643, 10.8113), ("المهدية", 35.5047, 11.0622),
    ("القيروان", 35.6781, 10.0963), ("القصرين", 35.1676, 8.8365),
    ("سيدي بوزيد", 35.0382, 9.4849), ("صفاقس", 34.7406, 10.7603),
    ("قفصة", 34.4250, 8.7842), ("توزر", 33.9197, 8.1335),
    ("قابس", 33.8815, 10.0982), ("مدنين", 33.3549, 10.5055),
    ("تطاوين", 32.9297, 10.4518), ("قبلي", 33.7050, 8.9690),
]

def _walk_coords(obj, out):
    if isinstance(obj, (list, tuple)):
        if len(obj) >= 2 and isinstance(obj[0], (int, float)) and isinstance(obj[1], (int, float)):
            out.append((float(obj[0]), float(obj[1])))
        else:
            for item in obj:
                _walk_coords(item, out)

def _pick_prop(props, candidates, contains=()):
    lowered = {str(k).lower(): v for k, v in props.items()}
    for key in candidates:
        if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
            return str(lowered[key.lower()]).strip()
    for key, value in lowered.items():
        if value in (None, ""):
            continue
        if any(term in key for term in contains):
            return str(value).strip()
    return ""

def load_municipalities():
    try:
        local_data = os.path.join(os.path.dirname(__file__), "municipalites.geojson")
        if os.path.exists(local_data):
            with open(local_data, "r", encoding="utf-8") as source_file:
                data = json.load(source_file)
        else:
            response = httpx.get(MUNICIPALITIES_URL, timeout=35, follow_redirects=True,
                                 headers={"User-Agent": "TunisiaPrayerTimesBot/1.0"})
            response.raise_for_status()
            data = response.json()
            with open(local_data, "w", encoding="utf-8") as target_file:
                json.dump(data, target_file, ensure_ascii=False)
        found = []
        for feature in data.get("features", []):
            props = feature.get("properties") or {}
            name = _pick_prop(
                props,
                ("NOM_MUN_AR", "nom_mun_ar", "NAME_EN_AR", "name_en_ar",
                 "MUNICIPALITE_AR", "municipalite_ar", "NAME_AR", "name_ar",
                 "NOM_MUN", "nom_mun", "NOM_COMMUNE", "nom_commune",
                 "MUNICIPALITE", "municipalite", "MUNICIPALITY", "municipality",
                 "COMMUNE", "commune", "NAME_EN", "name_en", "NAME", "name"),
                ("mun", "commune", "name", "nom")
            )
            governorate = _pick_prop(
                props,
                ("GOUVERNORAT_AR", "gouvernorat_ar", "GOUV_NAME_AR", "gouv_name_ar",
                 "GOVERNORATE_AR", "governorate_ar", "GOVERNORAT", "gouvernorat",
                 "GOUV_NAME", "gouv_name", "GOVERNORATE", "governorate", "WILAYA", "wilaya"),
                ("gouv", "govern", "wilaya")
            )
            coords = []
            _walk_coords((feature.get("geometry") or {}).get("coordinates", []), coords)
            if not name or not coords:
                continue
            # صندوق الحدود يعطي نقطة تقريبية داخل/قرب البلدية، وليس عنوانًا دقيقًا.
            lons = [p[0] for p in coords]
            lats = [p[1] for p in coords]
            lon = (min(lons) + max(lons)) / 2
            lat = (min(lats) + max(lats)) / 2
            if 7.0 <= lon <= 12.0 and 30.0 <= lat <= 38.0:
                found.append((name, lat, lon, governorate or "ولايات أخرى"))
        # إزالة التكرار مع الحفاظ على الأسماء المختلفة.
        unique = {}
        for item in found:
            unique[(item[0], item[3])] = item
        found = sorted(unique.values(), key=lambda x: (x[3], x[0]))
        if len(found) >= 100:
            log.info("تم تحميل %s منطقة من ملف الحدود الجغرافي", len(found))
            return found
        raise RuntimeError("ملف البلديات لا يحتوي على قائمة قابلة للاستخدام.")
    except Exception as exc:
        log.exception("تعذر تحميل ملف البلديات.")
        raise RuntimeError(
            "لم أتمكن من تحميل قائمة البلديات. تأكد من اتصال الإنترنت ثم أعد تشغيل البوت."
        ) from exc

CITIES = load_municipalities()

PRAYERS = [
    ("Fajr", "الفجر", "🌅"),
    ("Dhuhr", "الظهر", "☀️"),
    ("Asr", "العصر", "🌤️"),
    ("Maghrib", "المغرب", "🌇"),
    ("Isha", "العشاء", "🌙"),
]
DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
BEFORE_OPTIONS = [(0, "عند الأذان فقط"), (5, "قبل 5 دقائق"), (10, "قبل 10 دقائق"),
                  (15, "قبل 15 دقيقة"), (30, "قبل 30 دقيقة")]


# ---------- قاعدة البيانات ----------
def q(sql, params=(), fetch=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(sql, params)
        if fetch == "one":
            res = cur.fetchone()
        elif fetch == "all":
            res = cur.fetchall()
        else:
            res = None
        conn.commit()
        return res
    finally:
        conn.close()


q(
    "CREATE TABLE IF NOT EXISTS users ("
    "chat_id INTEGER PRIMARY KEY, city INTEGER NOT NULL, "
    "before_min INTEGER NOT NULL DEFAULT 10)"
)
q(
    "CREATE TABLE IF NOT EXISTS sent ("
    "chat_id INTEGER, day TEXT, tag TEXT, PRIMARY KEY (chat_id, day, tag))"
)


def set_city(chat_id, idx):
    q(
        "INSERT INTO users(chat_id, city, before_min) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET city=excluded.city",
        (chat_id, idx, DEFAULT_BEFORE),
    )
    return q("SELECT before_min FROM users WHERE chat_id=?", (chat_id,), "one")[0]


def get_user(chat_id):
    return q("SELECT city, before_min FROM users WHERE chat_id=?", (chat_id,), "one")


def set_before(chat_id, minutes):
    q("UPDATE users SET before_min=? WHERE chat_id=?", (minutes, chat_id))


def delete_user(chat_id):
    q("DELETE FROM users WHERE chat_id=?", (chat_id,))


def all_users():
    return q("SELECT chat_id, city, before_min FROM users", (), "all")


def claim(chat_id, day, tag):
    """يرجع True مرة واحدة فقط لكل تنبيه، لمنع التكرار."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sent(chat_id, day, tag) VALUES(?,?,?)",
            (chat_id, day, tag),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def cleanup(before_day):
    q("DELETE FROM sent WHERE day < ?", (before_day,))


# ---------- المواقيت ----------
_cache = {}


async def get_timings(idx, day):
    key = (idx, day.isoformat())
    if key in _cache:
        return _cache[key]
    _, lat, lon, _governorate = CITIES[idx]
    url = f"https://api.aladhan.com/v1/timings/{day.strftime('%d-%m-%Y')}"
    params = {"latitude": lat, "longitude": lon, "method": METHOD}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        timings = r.json()["data"]["timings"]
    clean = {k: str(v).split()[0] for k, v in timings.items()}
    for old in [k for k in _cache if k[1] < day.isoformat()]:
        del _cache[old]
    _cache[key] = clean
    return clean


def at(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


async def next_prayer(idx, now):
    day = now.date()
    t = await get_timings(idx, day)
    for key, name, _ in PRAYERS:
        dt = at(day, t[key])
        if dt > now:
            return name, dt
    tomorrow = day + timedelta(days=1)
    t2 = await get_timings(idx, tomorrow)
    return "الفجر", at(tomorrow, t2["Fajr"])


def remaining(now, dt):
    minutes = int((dt - now).total_seconds() // 60)
    h, m = divmod(minutes, 60)
    return f"{h} س {m} د" if h else f"{m} د"


async def build_times(idx):
    now = datetime.now(TZ)
    day = now.date()
    t = await get_timings(idx, day)
    lines = [
        f"🕌 مواقيت الصلاة — {CITIES[idx][0]}",
        f"📅 {day.isoformat()} ({DAYS[day.weekday()]})",
        "",
    ]
    for key, name, emo in PRAYERS:
        lines.append(f"{emo} {name}: {t[key]}")
        if key == "Fajr":
            lines.append(f"🌄 الشروق: {t['Sunrise']}")
    name, dt = await next_prayer(idx, now)
    lines += ["", f"⏳ الصلاة القادمة: {name} بعد {remaining(now, dt)}"]
    return "\n".join(lines)


# ---------- الأوامر ----------
def governorate_keyboard():
    names = sorted({c[3] for c in CITIES})
    buttons = [InlineKeyboardButton(name, callback_data=f"g:{i}")
               for i, name in enumerate(names)]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])

def municipality_keyboard(governorate):
    matches = [(i, c[0]) for i, c in enumerate(CITIES) if c[3] == governorate]
    buttons = [InlineKeyboardButton(name[:60], callback_data=f"c:{idx}")
               for idx, name in matches]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])

async def on_governorate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    names = sorted({c[3] for c in CITIES})
    try:
        governorate = names[int(query.data.split(":")[1])]
    except (IndexError, ValueError):
        await query.answer("اختيار غير صالح", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        f"اختر البلدية التابعة لـ {governorate}:",
        reply_markup=municipality_keyboard(governorate)
    )


def before_keyboard():
    buttons = [
        InlineKeyboardButton(label, callback_data=f"b:{m}") for m, label in BEFORE_OPTIONS
    ]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "السلام عليكم 🌙\nاختر الولاية أولاً، ثم البلدية:",
        reply_markup=governorate_keyboard(),
    )


async def on_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    idx = int(query.data.split(":")[1])
    chat_id = query.message.chat_id
    before = await asyncio.to_thread(set_city, chat_id, idx)
    await query.answer("تم الاختيار ✅")
    extra = f"قبل الأذان بـ {before} دقائق وعند الأذان" if before else "عند الأذان"
    await query.edit_message_text(
        f"✅ تم اختيار {CITIES[idx][0]}.\n"
        f"سأرسل لك تنبيه الصلاة {extra}.\n\n"
        "/times مواقيت اليوم\n/next الصلاة القادمة\n"
        "/before تغيير وقت التنبيه\n/city تغيير المدينة\n/stop للإيقاف"
    )
    try:
        await context.bot.send_message(chat_id, await build_times(idx))
    except Exception:
        log.exception("فشل جلب المواقيت")
        await context.bot.send_message(chat_id, "تعذر جلب المواقيت الآن، حاول بعد قليل.")


async def times_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await asyncio.to_thread(get_user, update.effective_chat.id)
    if not user:
        await update.message.reply_text("اختر مدينتك أولاً: /start")
        return
    try:
        await update.message.reply_text(await build_times(user[0]))
    except Exception:
        log.exception("فشل جلب المواقيت")
        await update.message.reply_text("تعذر جلب المواقيت الآن، حاول بعد قليل.")


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await asyncio.to_thread(get_user, update.effective_chat.id)
    if not user:
        await update.message.reply_text("اختر مدينتك أولاً: /start")
        return
    try:
        now = datetime.now(TZ)
        name, dt = await next_prayer(user[0], now)
        await update.message.reply_text(
            f"⏳ الصلاة القادمة: {name} الساعة {dt:%H:%M}\nبعد {remaining(now, dt)}"
        )
    except Exception:
        log.exception("فشل جلب المواقيت")
        await update.message.reply_text("تعذر جلب المواقيت الآن، حاول بعد قليل.")


async def before_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await asyncio.to_thread(get_user, update.effective_chat.id)
    if not user:
        await update.message.reply_text("اختر مدينتك أولاً: /start")
        return
    await update.message.reply_text(
        "متى تريد التنبيه؟", reply_markup=before_keyboard()
    )


async def on_before(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    minutes = int(query.data.split(":")[1])
    await asyncio.to_thread(set_before, query.message.chat_id, minutes)
    await query.answer("تم ✅")
    label = dict(BEFORE_OPTIONS).get(minutes, f"قبل {minutes} دقيقة")
    await query.edit_message_text(f"✅ تم الضبط: {label}")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(delete_user, update.effective_chat.id)
    await update.message.reply_text("تم إيقاف التنبيهات. للعودة اكتب /start")


# ---------- التنبيهات ----------
async def tick(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    day = now.date()
    try:
        users = await asyncio.to_thread(all_users)
        await asyncio.to_thread(cleanup, (day - timedelta(days=2)).isoformat())
    except Exception:
        log.exception("فشل قراءة المشتركين")
        return

    groups = {}
    for chat_id, idx, before in users:
        groups.setdefault(idx, []).append((chat_id, before))

    for idx, members in groups.items():
        try:
            t = await get_timings(idx, day)
        except Exception:
            log.exception("فشل جلب مواقيت %s", CITIES[idx][0])
            continue

        for key, name, emo in PRAYERS:
            ptime = at(day, t[key])
            is_friday_noon = key == "Dhuhr" and day.weekday() == 4
            title = "الجمعة" if is_friday_noon else name
            for chat_id, before in members:
                events = []
                if before > 0:
                    events.append(
                        ("before", ptime - timedelta(minutes=before), ptime,
                         f"⏰ بقي {before} دقيقة على صلاة {title} ({ptime:%H:%M})")
                    )
                events.append(
                    ("at", ptime, ptime + timedelta(minutes=10),
                     f"{emo} حان الآن موعد أذان {title} ({ptime:%H:%M}) — {CITIES[idx][0]}\n"
                     "حيّ على الصلاة 🕌")
                )
                for kind, start_t, end_t, text in events:
                    if start_t <= now < end_t:
                        if await asyncio.to_thread(
                            claim, chat_id, day.isoformat(), f"{key}:{kind}"
                        ):
                            try:
                                await context.bot.send_message(chat_id, text)
                            except Forbidden:
                                await asyncio.to_thread(delete_user, chat_id)
                            except Exception:
                                log.exception("فشل الإرسال إلى %s", chat_id)
                            await asyncio.sleep(0.05)


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "اختيار المدينة"),
            BotCommand("times", "مواقيت اليوم"),
            BotCommand("next", "الصلاة القادمة"),
            BotCommand("before", "تغيير وقت التنبيه"),
            BotCommand("city", "تغيير المدينة"),
            BotCommand("stop", "إيقاف التنبيهات"),
        ]
    )


def main():
    if not BOT_TOKEN:
        sys.exit("ضع BOT_TOKEN في ملف .env")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "city"], start))
    app.add_handler(CommandHandler("times", times_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("before", before_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(on_governorate, pattern=r"^g:\d+$"))
    app.add_handler(CallbackQueryHandler(on_city, pattern=r"^c:\d+$"))
    app.add_handler(CallbackQueryHandler(on_before, pattern=r"^b:\d+$"))
    app.job_queue.run_repeating(tick, interval=60, first=10)
        app.job_queue.run_daily(
        send_adhkar,
        time=time(6, 0, tzinfo=TZ),
        data="🌅 أذكار الصباح\n\nسبحان الله وبحمده، سبحان الله العظيم.\n\nأستغفر الله وأتوب إليه.\n\nاللهم بك أصبحنا وبك أمسينا، وبك نحيا وبك نموت وإليك النشور."
    )

    app.job_queue.run_daily(
        send_adhkar,
        time=time(18, 0, tzinfo=TZ),
        data="🌇 أذكار المساء\n\nسبحان الله وبحمده، سبحان الله العظيم.\n\nأستغفر الله وأتوب إليه.\n\nاللهم بك أمسينا وبك أصبحنا، وبك نحيا وبك نموت وإليك المصير."
    )
    log.info("بوت الصلاة يعمل")
    app.run_polling()
async def send_adhkar(context):
    message = context.job.data
    for user in all_users():
        try:
            await context.bot.send_message(
                chat_id=user[0],
                text=message
            )
        except Exception as e:
            print(f"Error sending adhkar: {e}")


if __name__ == "__main__":
    main()
