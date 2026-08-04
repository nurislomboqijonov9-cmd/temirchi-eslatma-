"""
TEMIRCHI — Follow-up eslatma boti (alohida bot).

Erkin matn yoki ovoz: "Akmal 998901234567 8-avgust lesa olmoqchi"
  -> AI ajratadi: ism, telefon, sana, izoh
  -> "Shunday tushundim… [✅ Saqlash] [✏️ Bekor]" tasdiq
  -> saqlaydi
Har kuni REPORT_HOUR (9:00) da o'sha kungi eslatmalar otchot bo'lib keladi.

ENV (Railway Variables):
  BOT_TOKEN        - @BotFather tokeni (SHART)
  GEMINI_API_KEY   - Google Gemini kaliti (SHART)
  GEMINI_MODEL     - default gemini-2.5-flash
  REPORT_HOUR      - kunlik otchot soati (default 9)
  TZ               - Asia/Tashkent
  DATA_DIR         - /data (Railway volume — saqlanib qolishi uchun)
  ESLATMA_ADMINS   - (ixtiyoriy) vergul bilan ruxsatli chat id'lar; bo'sh = hamma
"""
import os
import re
import sqlite3
import asyncio
import logging
from io import BytesIO
from datetime import datetime, date, timedelta

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))
except Exception:
    TZ = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eslatma")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
DATA_DIR = os.getenv("DATA_DIR", "/data")
_admins = [x.strip() for x in (os.getenv("ESLATMA_ADMINS", "") or "").split(",") if x.strip()]
ADMINS = set(int(x) for x in _admins if x.lstrip("-").isdigit())

DB_PATH = os.path.join(DATA_DIR, "eslatma.db")


# ---------------- Vaqt ----------------
def now_tk():
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def today_tk():
    return now_tk().date()


# ---------------- Baza ----------------
def _con():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _con()
    con.execute("""CREATE TABLE IF NOT EXISTS eslatmalar(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, ism TEXT, tel TEXT, sana TEXT, izoh TEXT,
        yuborildi INTEGER DEFAULT 0, created TEXT)""")
    try:
        con.execute("ALTER TABLE eslatmalar ADD COLUMN vaqt TEXT")  # HH:MM (ixtiyoriy)
    except Exception:
        pass
    con.commit()
    con.close()


def eslatma_qosh(chat_id, ism, tel, sana, izoh, vaqt=None):
    con = _con()
    cur = con.execute(
        "INSERT INTO eslatmalar(chat_id,ism,tel,sana,izoh,vaqt,yuborildi,created) VALUES(?,?,?,?,?,?,0,?)",
        (chat_id, ism, tel, str(sana)[:10], izoh, (vaqt or None), now_tk().isoformat()))
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def eslatma_ochir(rid, chat_id=None):
    con = _con()
    if chat_id is None:
        con.execute("DELETE FROM eslatmalar WHERE id=?", (rid,))
    else:
        con.execute("DELETE FROM eslatmalar WHERE id=? AND chat_id=?", (rid, chat_id))
    con.commit()
    con.close()


def eslatmalar_kutilayotgan(chat_id, limit=50):
    con = _con()
    rows = con.execute(
        "SELECT * FROM eslatmalar WHERE chat_id=? AND sana>=? ORDER BY sana ASC LIMIT ?",
        (chat_id, str(today_tk()), limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def eslatmalar_bugun_hammasi():
    """9:00 otchot uchun — VAQTI YO'Q eslatmalar (vaqtlilar aniq vaqtida boradi)."""
    con = _con()
    rows = con.execute(
        "SELECT * FROM eslatmalar WHERE yuborildi=0 AND (vaqt IS NULL OR vaqt='') AND sana<=? ORDER BY chat_id, sana",
        (str(today_tk()),)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def eslatmalar_vaqtli_due(bugun, hhmm):
    """Aniq vaqti kelgan (yoki o'tib ketgan) vaqtli eslatmalar."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT * FROM eslatmalar WHERE yuborildi=0 AND vaqt IS NOT NULL AND vaqt!='' "
            "AND (sana < ? OR (sana = ? AND vaqt <= ?)) ORDER BY sana, vaqt",
            (bugun, bugun, hhmm)).fetchall()
    except Exception:
        rows = []
    con.close()
    return [dict(r) for r in rows]


def eslatma_belgila(rid):
    con = _con()
    con.execute("UPDATE eslatmalar SET yuborildi=1 WHERE id=?", (rid,))
    con.commit()
    con.close()


# ---------------- AI (Gemini) ----------------
_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_KEY)
    return _client


class Eslatma(BaseModel):
    ism: str | None = Field(default=None, description="Mijoz ismi")
    tel: str | None = Field(default=None, description="Telefon raqami (bo'lsa)")
    sana: str | None = Field(default=None, description="Eslatiladigan sana, ISO YYYY-MM-DD")
    vaqt: str | None = Field(default=None, description="Aniq vaqt (soat) — HH:MM, agar aytilsa. Aytilmasa null")
    izoh: str | None = Field(default=None, description="Nima uchun eslatish: masalan 'lesa olmoqchi', 'lesa oborish kerak'")


def _now_context():
    return (f"(Bugun: {today_tk().isoformat()}, hozir soat {now_tk().strftime('%H:%M')} — "
            f"sana va vaqtni shunga qarab hisobla. Yil aytilmasa, eng yaqin kelasi shu sanani ol.)")


def parse_text(matn):
    """Erkin matndan eslatma ma'lumotini ajratadi."""
    sys = (
        "Sen ijara biznesi uchun eslatma yordamchisisan. Foydalanuvchi erkin matn yozadi. "
        "Undan quyidagilarni ajrat: ism, tel (telefon raqami), sana (ISO YYYY-MM-DD), "
        "vaqt (aniq soat HH:MM — agar aytilsa), izoh (nima qilish kerak). "
        "Masalan: 'Akmal 998901234567 8-avgust lesa olmoqchi' -> ism=Akmal, tel=998901234567, "
        "sana=2026-08-08, vaqt=null, izoh='lesa olmoqchi'. "
        "'bugun 21:31 da Akmalga qo'ng'iroq' -> sana=bugun, vaqt=21:31. "
        "'ertaga soat 9 da' -> vaqt=09:00. 'kechqurun 8 da' -> vaqt=20:00. "
        "Soat aytilmasa vaqt=null qoldir. Topilmagan maydonni null qoldir. "
        "Sana va vaqtni bugungi kun va hozirgi soatdan kelib chiqib hisobla."
    )
    resp = client().models.generate_content(
        model=MODEL,
        contents=[_now_context(), matn],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Eslatma,
            system_instruction=sys),
    )
    if getattr(resp, "parsed", None) is not None:
        return resp.parsed
    import json
    return Eslatma(**json.loads(resp.text))


def transcribe(audio_bytes, mime="audio/ogg"):
    part = types.Part.from_bytes(data=audio_bytes, mime_type=mime)
    resp = client().models.generate_content(
        model=MODEL,
        contents=[part, "Ushbu ovozli xabarni AYNAN o'zbekcha matnga o'gir. Faqat matn."])
    return (resp.text or "").strip()


# ---------------- Yordamchilar ----------------
def _ruxsat(uid):
    return (not ADMINS) or (uid in ADMINS)


def _dmy(s):
    try:
        p = str(s)[:10].split("-")
        return f"{p[2]}.{p[1]}.{p[0]}"
    except Exception:
        return str(s)


def _vaqt_norm(v):
    """'21:31','9','21.31','9 30' -> 'HH:MM'; noto'g'ri bo'lsa ''."""
    if not v:
        return ""
    import re as _re
    m = _re.search(r"(\d{1,2})[:\.\s]?(\d{2})\b", v) or _re.search(r"\b(\d{1,2})\b", v)
    if not m:
        return ""
    h = int(m.group(1))
    mi = int(m.group(2)) if (m.lastindex and m.lastindex >= 2 and m.group(2)) else 0
    if h > 23 or mi > 59:
        return ""
    return f"{h:02d}:{mi:02d}"


def _kartochka(e):
    v = (e.get("vaqt") or "").strip()
    return (f"👤 {e.get('ism') or '—'}\n"
            f"📞 {e.get('tel') or '—'}\n"
            f"📅 {_dmy(e.get('sana'))}" + (f" ⏰ {v}" if v else "") + "\n"
            f"📝 {e.get('izoh') or '—'}")


# ---------------- Handlerlar ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ruxsat(update.effective_user.id):
        await update.message.reply_text("Ruxsat yo'q.")
        return
    await update.message.reply_text(
        "👋 Salom! Bu — eslatma boti.\n\n"
        "Shunchaki yozing yoki ayting:\n"
        "«Akmal 998901234567 8-avgust lesa olmoqchi»\n"
        "«bugun 21:31 da Akmalga qo'ng'iroq»\n\n"
        "Men ism, telefon, sana, soat va izohni ajrataman — tasdiqlasangiz saqlayman.\n"
        f"• Soat aytsangiz — aynan o'sha vaqtda eslataman.\n"
        f"• Soat aytmasangiz — o'sha kuni {REPORT_HOUR}:00 da eslataman.\n\n"
        "/royxat — kutilayotgan eslatmalar\n"
        "/bugun — bugungi eslatmalar\n"
        "/ochir <id> — o'chirish")


async def royxat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ruxsat(update.effective_user.id):
        return
    es = eslatmalar_kutilayotgan(update.effective_chat.id)
    if not es:
        await update.message.reply_text("Kutilayotgan eslatma yo'q.")
        return
    lines = ["📋 Kutilayotgan eslatmalar:\n"]
    for e in es:
        v = (e.get("vaqt") or "").strip()
        vaqt = f" ⏰ {v}" if v else ""
        lines.append(f"#{e['id']} · 📅 {_dmy(e['sana'])}{vaqt} · {e['ism'] or '—'} — {e['izoh'] or ''}")
    await update.message.reply_text("\n".join(lines))


async def bugun_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ruxsat(update.effective_user.id):
        return
    con = _con()
    rows = con.execute("SELECT * FROM eslatmalar WHERE chat_id=? AND sana=? ORDER BY id",
                       (update.effective_chat.id, str(today_tk()))).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("Bugun eslatma yo'q.")
        return
    lines = ["🔔 Bugungi eslatmalar:\n"]
    for r in rows:
        lines.append(_kartochka(dict(r)) + f"\n(#{r['id']})\n")
    await update.message.reply_text("\n".join(lines))


async def ochir_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ruxsat(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /ochir <id>")
        return
    eslatma_ochir(int(ctx.args[0]), update.effective_chat.id)
    await update.message.reply_text("🗑 O'chirildi.")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _ruxsat(update.effective_user.id):
        await update.message.reply_text("Ruxsat yo'q.")
        return
    msg = update.effective_message

    # matn yoki ovoz
    matn = (msg.text or "").strip()
    if not matn and msg.voice:
        kut = await msg.reply_text("🎧 Ovozni tinglayapman…")
        try:
            f = await msg.voice.get_file()
            bio = BytesIO()
            await f.download_to_memory(bio)
            matn = transcribe(bio.getvalue(), msg.voice.mime_type or "audio/ogg")
        except Exception:
            log.exception("transcribe")
            matn = ""
        try:
            await ctx.bot.delete_message(update.effective_chat.id, kut.message_id)
        except Exception:
            pass
    if not matn:
        await msg.reply_text("Tushunmadim. Masalan: «Akmal 998901234567 8-avgust lesa olmoqchi»")
        return

    try:
        e = parse_text(matn)
    except Exception:
        log.exception("parse")
        await msg.reply_text("AI bilan bog'lanishda xato. Qaytadan urinib ko'ring.")
        return

    ism = (e.ism or "").strip()
    sana = (e.sana or "").strip()
    if not ism and not sana:
        await msg.reply_text(f"Tushunmadim 🤔\nEshitganim: «{matn}»\nMasalan: «Akmal 8-avgust lesa olmoqchi»")
        return
    if not sana:
        await msg.reply_text(f"Sanani topolmadim. Masalan «8-avgust» deb qo'shing.\nEshitganim: «{matn}»")
        return

    vaqt = _vaqt_norm((e.vaqt or "").strip())
    d = {"ism": ism, "tel": (e.tel or "").strip(), "sana": sana, "vaqt": vaqt, "izoh": (e.izoh or "").strip()}
    ctx.user_data["pending"] = d
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Saqlash", callback_data="eok"),
        InlineKeyboardButton("✏️ Bekor", callback_data="ebekor"),
    ]])
    await msg.reply_text("🎤 Shunday tushundim:\n\n" + _kartochka(d) + "\n\nSaqlaymizmi?", reply_markup=kb)


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ebekor":
        ctx.user_data.pop("pending", None)
        await q.edit_message_text("❌ Bekor qilindi. Qaytadan yozing.")
        return
    if q.data == "eok":
        d = ctx.user_data.get("pending")
        if not d:
            await q.edit_message_text("Ma'lumot topilmadi, qaytadan yozing.")
            return
        rid = eslatma_qosh(q.message.chat_id, d["ism"], d["tel"], d["sana"], d["izoh"], d.get("vaqt"))
        ctx.user_data.pop("pending", None)
        if d.get("vaqt"):
            qachon = f"📅 {_dmy(d['sana'])} kuni ⏰ {d['vaqt']} da eslataman."
        else:
            qachon = f"📅 {_dmy(d['sana'])} kuni {REPORT_HOUR}:00 da eslataman."
        await q.edit_message_text(f"✅ Saqlandi (#{rid})\n\n" + _kartochka(d) + "\n\n" + qachon)


# ---------------- Kunlik otchot (9:00) ----------------
async def _otchot_yubor(app):
    es = eslatmalar_bugun_hammasi()
    # chat_id bo'yicha guruhlash
    by_chat = {}
    for e in es:
        by_chat.setdefault(e["chat_id"], []).append(e)
    for chat_id, items in by_chat.items():
        lines = ["🔔 *Bugungi eslatmalar:*\n"]
        for e in items:
            lines.append(f"👤 *{e['ism'] or '—'}* — {e['izoh'] or ''}\n📞 {e['tel'] or '—'}  (#{e['id']})")
        try:
            await app.bot.send_message(chat_id, "\n\n".join(lines), parse_mode="Markdown")
            for e in items:
                eslatma_belgila(e["id"])
        except Exception:
            log.exception("otchot yuborish (%s)", chat_id)


async def _send_one(app, e):
    v = (e.get("vaqt") or "").strip()
    txt = (f"🔔 *Eslatma!*\n\n👤 *{e['ism'] or '—'}* — {e['izoh'] or ''}\n"
           f"📞 {e['tel'] or '—'}" + (f"  ⏰ {v}" if v else "") + f"  (#{e['id']})")
    try:
        await app.bot.send_message(e["chat_id"], txt, parse_mode="Markdown")
        eslatma_belgila(e["id"])
    except Exception:
        log.exception("vaqtli eslatma (%s)", e.get("id"))


_last_daily = None


async def scheduler_loop(app):
    global _last_daily
    while True:
        try:
            now = now_tk()
            bugun = now.date().isoformat()
            hhmm = now.strftime("%H:%M")
            # 1) Vaqtli eslatmalar — aniq belgilangan soatda
            for e in eslatmalar_vaqtli_due(bugun, hhmm):
                await _send_one(app, e)
            # 2) Kunlik otchot (vaqtsizlar) — REPORT_HOUR da kuniga bir marta
            if now.hour == REPORT_HOUR and _last_daily != bugun:
                _last_daily = bugun
                await _otchot_yubor(app)
        except Exception:
            log.exception("scheduler_loop")
        await asyncio.sleep(40)


async def _post_init(app):
    asyncio.create_task(scheduler_loop(app))


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN yo'q — Railway Variables'ga qo'ying.")
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("royxat", royxat_cmd))
    app.add_handler(CommandHandler("bugun", bugun_cmd))
    app.add_handler(CommandHandler("ochir", ochir_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.VOICE, on_message))
    log.info("Eslatma boti ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
