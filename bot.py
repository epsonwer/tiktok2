import re
import os
import json
import textwrap
import tempfile
import asyncio
import subprocess
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # https://your-app.up.railway.app  (без слеша в конце!)
PORT = int(os.environ.get("PORT", 8080))

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

STYLE_EXAMPLES = """
Обычное утро: проверяешь Drazze и понимаешь, что всё идёт по плану | Ссылка в профиле
Drazze – путь к успеху | Ссылка в профиле
Тот самый момент, когда твой счёт с Drazze растёт быстрее, чем твои сомнения. | Ссылка в профиле
Drazze даёт свои плоды и ты тоже можешь получить кусочек) | Ссылка в профиле
Раньше ты смотрел на ценники, теперь ты смотришь, как Drazze меняет твою жизнь. | Все в био
""".strip()

PROMPT = f"""Ты генерируешь короткие рекламные тексты для TikTok-видео бренда Drazze.

Стиль — цепляющий, мотивирующий, с лёгкой интригой. Название Drazze всегда с заглавной буквы.

Примеры:

{STYLE_EXAMPLES}

Ответ СТРОГО в JSON без пояснений и без markdown:

{{"top_text": "Основной текст до 60 символов", "bottom_text": "Ссылка в профиле"}}

bottom_text — только "Ссылка в профиле" или "Все в био"."""

TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+", re.IGNORECASE)

pending_video: dict[int, str] = {}
user_mode: dict[int, str] = {}


async def generate_text() -> dict:
    payload = {"contents": [{"parts": [{"text": PROMPT + "\n\nСгенерируй новый текст."}]}]}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(GEMINI_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"```json|```", "", raw).strip()
    d = json.loads(raw)
    return {
        "top_text": d.get("top_text", "Drazze меняет правила"),
        "bottom_text": d.get("bottom_text", "Ссылка в профиле"),
    }


def overlay_text(input_path: str, top: str, bottom: str) -> str:
    out = input_path.replace(".mp4", "_out.mp4")
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    def esc(s):
        return "\n".join(textwrap.wrap(s, 28)).replace("'", "\\'").replace(":", "\\:")

    vf = (
        f"drawbox=x=0:y=0:w=iw:h=ih*0.22:color=black@0.5:t=fill,"
        f"drawbox=x=0:y=ih*0.82:w=iw:h=ih*0.18:color=black@0.5:t=fill,"
        f"drawtext=text='{esc(top)}':fontfile='{font}':fontsize=38:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.04:line_spacing=8:shadowcolor=black@0.6:shadowx=2:shadowy=2,"
        f"drawtext=text='{esc(bottom)}':fontfile='{font}':fontsize=34:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.86:line_spacing=6:shadowcolor=black@0.6:shadowx=2:shadowy=2"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
        "-preset", "fast", "-crf", "23", out,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    return out if r.returncode == 0 and os.path.exists(out) else input_path


async def download_tiktok(url: str) -> bytes | None:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://tikwm.com/api/?url={url}&hd=1",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = r.json()
    if data.get("code") != 0:
        return None
    video_url = data["data"].get("hdplay") or data["data"].get("play")
    if not video_url:
        return None
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        vr = await c.get(video_url, headers={"Referer": "https://www.tiktok.com/"})
    return vr.content


def reencode(path: str) -> str:
    out = path.replace(".mp4", "_fixed.mp4")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-c:a", "aac",
         "-movflags", "+faststart", "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23", out],
        capture_output=True, timeout=300,
    )
    if r.returncode == 0 and os.path.exists(out):
        os.unlink(path)
        return out
    return path


async def send_with_overlay(update: Update, video_path: str, top: str, bottom: str):
    loop = asyncio.get_event_loop()
    final = await loop.run_in_executor(None, overlay_text, video_path, top, bottom)
    if os.path.getsize(final) / 1024 / 1024 > 50:
        await update.effective_message.reply_text("❌ Видео слишком большое (лимит 50 МБ)")
        return
    with open(final, "rb") as f:
        await update.effective_message.reply_video(
            video=f,
            caption=f"📝 {top}\n{bottom}",
            supports_streaming=True,
            write_timeout=120,
            read_timeout=120,
        )
    for p in {video_path, final}:
        try:
            os.unlink(p)
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    if user_mode.get(user_id) == "custom":
        video_path = pending_video.pop(user_id, None)
        user_mode.pop(user_id, None)
        if not video_path:
            await update.message.reply_text("❌ Видео не найдено, отправь ссылку заново.")
            return
        top, bottom = (text.split("|", 1) + ["Ссылка в профиле"])[:2]
        await update.message.reply_text("⏳ Накладываю текст...")
        await send_with_overlay(update, video_path, top.strip(), bottom.strip())
        return

    urls = TIKTOK_RE.findall(text)
    if not urls:
        await update.message.reply_text("Отправь ссылку на TikTok 🎬")
        return

    msg = await update.message.reply_text("⏳ Скачиваю...")
    video_data = await download_tiktok(urls[0])
    if not video_data:
        await msg.edit_text("❌ Не удалось скачать видео")
        return

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_data)
        tmp_path = f.name

    loop = asyncio.get_event_loop()
    video_path = await loop.run_in_executor(None, reencode, tmp_path)
    pending_video[user_id] = video_path

    await msg.delete()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 AI текст", callback_data=f"ai_{user_id}")],
        [InlineKeyboardButton("✏️ Свой текст", callback_data=f"custom_{user_id}")],
        [InlineKeyboardButton("⏭ Без текста", callback_data=f"skip_{user_id}")],
    ])
    await update.message.reply_text("✅ Видео скачано! Что делаем с текстом?", reply_markup=kb)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # ── FIX: правильный парсинг callback_data ──────────────────────────────
    # Формат: "action_userid", где userid — число. rsplit гарантирует,
    # что action может содержать "_" (например, если добавишь новые кнопки).
    action, uid = q.data.rsplit("_", 1)
    user_id = int(uid)
    # ───────────────────────────────────────────────────────────────────────

    video_path = pending_video.get(user_id)
    if not video_path or not os.path.exists(video_path):
        await q.edit_message_text("❌ Видео не найдено, отправь ссылку заново.")
        return

    if action == "skip":
        pending_video.pop(user_id, None)
        await q.edit_message_text("⏳ Отправляю...")
        with open(video_path, "rb") as f:
            await q.message.reply_video(
                video=f, supports_streaming=True, write_timeout=120, read_timeout=120
            )
        os.unlink(video_path)
        await q.edit_message_text("✅ Готово!")

    elif action == "ai":
        await q.edit_message_text("🤖 Генерирую текст...")
        try:
            texts = await generate_text()
            pending_video.pop(user_id, None)
            await q.edit_message_text(
                f"⏳ Накладываю:\n📌 {texts['top_text']}\n📌 {texts['bottom_text']}"
            )
            # ── FIX: передаём update, а не q ──────────────────────────────
            await send_with_overlay(update, video_path, texts["top_text"], texts["bottom_text"])
            # ──────────────────────────────────────────────────────────────
            await q.edit_message_text("✅ Готово!")
        except Exception as e:
            await q.edit_message_text(f"❌ Ошибка: {str(e)[:200]}")

    elif action == "custom":
        user_mode[user_id] = "custom"
        await q.edit_message_text(
            "✏️ Введи текст:\n\n`Верхний текст | Нижний текст`\n\n"
            "Пример:\n`Drazze меняет правила | Ссылка в профиле`",
            parse_mode="Markdown",
        )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print(f"Бот запущен на webhook: {WEBHOOK_URL}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/webhook",
        url_path="webhook",
    )


if __name__ == "__main__":
    main()
