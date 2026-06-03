import os
import re
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # https://your-app.up.railway.app
PORT = int(os.environ.get("PORT", 8080))

TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+", re.IGNORECASE)


async def download_tiktok(url: str) -> tuple[bytes | None, str | None]:
    """Скачивает видео через tikwm.com API. Возвращает (bytes, filename) или (None, None)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            "https://tikwm.com/api/",
            params={"url": url, "hd": "1"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = r.json()

    if data.get("code") != 0 or not data.get("data"):
        return None, None

    video_url = data["data"].get("hdplay") or data["data"].get("play")
    if not video_url:
        return None, None

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        vr = await c.get(video_url, headers={"Referer": "https://www.tiktok.com/"})

    if vr.status_code != 200:
        return None, None

    filename = f"tiktok_{data['data'].get('id', 'video')}.mp4"
    return vr.content, filename


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = TIKTOK_RE.findall(text)

    if not urls:
        await update.message.reply_text("Отправь ссылку на TikTok 🎬")
        return

    msg = await update.message.reply_text("⏳ Скачиваю...")

    video_bytes, filename = await download_tiktok(urls[0])

    if not video_bytes:
        await msg.edit_text("❌ Не удалось скачать видео. Попробуй другую ссылку.")
        return

    size_mb = len(video_bytes) / 1024 / 1024
    if size_mb > 50:
        await msg.edit_text(f"❌ Видео слишком большое ({size_mb:.1f} МБ, лимит 50 МБ)")
        return

    await msg.edit_text("📤 Отправляю...")
    await update.message.reply_video(
        video=video_bytes,
        filename=filename,
        supports_streaming=True,
        write_timeout=120,
        read_timeout=120,
    )
    await msg.delete()


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"Бот запущен: {WEBHOOK_URL}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/webhook",
        url_path="webhook",
    )


if __name__ == "__main__":
    main()
