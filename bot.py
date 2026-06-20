import re
import os
import tempfile
import asyncio
import subprocess
import zipfile

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СВОЙ_TOKEN_СЮДА")

# Если ссылок в одном сообщении больше этого числа — все видео
# отправляются одним zip-архивом, файлы внутри пронумерованы 1, 2, 3...
ZIP_THRESHOLD = 2

MAX_SIZE_MB = 50

TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)


def reencode(path: str) -> str:
    """Перекодирует видео в H264/AAC для гарантированного воспроизведения."""
    out = path + "_out.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-i", path,
        "-c:v", "libx264",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-c:a", "aac",
        "-ac", "2",
        "-ar", "44100",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-preset", "ultrafast",
        "-crf", "26",
        out,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=240)
    if result.returncode == 0 and os.path.exists(out):
        os.unlink(path)
        return out
    print(f"[ffmpeg error] {result.stderr.decode()[:500]}", flush=True)
    return path


async def download_tiktok(url: str) -> bytes | None:
    """Скачивает TikTok-видео без водяного знака через tikwm.com."""
    api_url = f"https://tikwm.com/api/?url={url}&hd=1"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") != 0:
        return None

    d = data["data"]
    video_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
    if not video_url:
        return None

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        video_resp = await client.get(
            video_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tiktok.com/",
                "Accept": "*/*",
            },
        )
        video_resp.raise_for_status()
        return video_resp.content


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = TIKTOK_RE.findall(text)

    if not urls:
        await update.message.reply_text(
            "Отправь мне одну или несколько ссылок на TikTok — и я скачаю видео "
            "без водяного знака 🎬\n\n"
            f"Если ссылок больше {ZIP_THRESHOLD} — пришлю всё одним zip-архивом, "
            "видео внутри будут пронумерованы (1, 2, 3...)."
        )
        return

    count = len(urls)
    use_zip = count > ZIP_THRESHOLD

    status_msg = await update.message.reply_text(f"⏳ Скачиваю 0 из {count} видео...")

    success = 0
    files_for_zip: list[tuple[str, str]] = []

    for i, url in enumerate(urls, 1):
        video_path = None
        try:
            video_data = await download_tiktok(url)
            if not video_data:
                await update.message.reply_text(f"❌ Не удалось скачать видео {i}")
                continue

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(video_data)
                tmp_path = f.name

            loop = asyncio.get_event_loop()
            video_path = await loop.run_in_executor(None, reencode, tmp_path)

            if not video_path or not os.path.exists(video_path):
                await update.message.reply_text(f"❌ Не удалось скачать видео {i}")
                continue

            size_mb = os.path.getsize(video_path) / (1024 * 1024)
            if size_mb > MAX_SIZE_MB:
                await update.message.reply_text(
                    f"❌ Видео {i} слишком большое ({size_mb:.0f} МБ, лимит — {MAX_SIZE_MB} МБ)"
                )
                os.unlink(video_path)
                continue

            if use_zip:
                # Копим файлы для архива, подписываем цифрой
                files_for_zip.append((f"{i}.mp4", video_path))
            else:
                with open(video_path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=f"{i}.mp4",
                        write_timeout=120,
                        read_timeout=120,
                    )
                os.unlink(video_path)

            success += 1
            await status_msg.edit_text(f"⏳ Скачиваю {success} из {count} видео...")

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка видео {i}: {str(e)[:300]}")
            if video_path and os.path.exists(video_path):
                try:
                    os.unlink(video_path)
                except Exception:
                    pass

    if use_zip and files_for_zip:
        await status_msg.edit_text(f"📦 Упаковываю {len(files_for_zip)} видео в zip...")
        zip_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zf:
                zip_path = zf.name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for arcname, path in files_for_zip:
                    zf.write(path, arcname=arcname)

            zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if zip_size_mb > MAX_SIZE_MB:
                await update.message.reply_text(
                    f"❌ Архив получился слишком большим ({zip_size_mb:.0f} МБ, "
                    f"лимит — {MAX_SIZE_MB} МБ). Пришли ссылки меньшими партиями."
                )
            else:
                with open(zip_path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename="videos.zip",
                        write_timeout=180,
                        read_timeout=180,
                    )
        finally:
            if zip_path and os.path.exists(zip_path):
                try:
                    os.unlink(zip_path)
                except Exception:
                    pass
            for _, path in files_for_zip:
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

    await status_msg.edit_text(f"✅ Готово! Скачано {success} из {count} видео.")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен...", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
