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

# Если ссылок (собранных из всех сообщений подряд) больше этого числа —
# все видео отправляются одним zip-архивом, файлы внутри пронумерованы 1, 2, 3...
ZIP_THRESHOLD = 2

# Сколько секунд ждать после последнего сообщения перед тем, как спросить
# имя архива. Нужно, чтобы успеть собрать все пересланные подряд ссылки
# (каждая пересылка в Telegram приходит отдельным сообщением).
DEBOUNCE_SECONDS = 2.0

MAX_SIZE_MB = 50
# Небольшой запас под служебные заголовки zip-архива
ZIP_SAFETY_MARGIN_MB = 2
MAX_ZIP_PAYLOAD_MB = MAX_SIZE_MB - ZIP_SAFETY_MARGIN_MB

TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)

# Накопленные ссылки и отложенная задача на каждого пользователя
pending: dict[int, list[str]] = {}
pending_tasks: dict[int, asyncio.Task] = {}
pending_chat: dict[int, int] = {}

# Пользователи, которых сейчас ждём с ответом на "как назвать zip"
awaiting_zip_name: dict[int, list[str]] = {}


def safe_zip_filename(name: str) -> str:
    """Убирает запрещённые символы и гарантирует расширение .zip"""
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|\r\n]+', "", name)
    name = name.strip(". ")
    if not name:
        name = "videos"
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return name


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


async def process_urls(context: ContextTypes.DEFAULT_TYPE, user_id: int, zip_name: str | None = None):
    urls = pending.pop(user_id, [])
    pending_tasks.pop(user_id, None)
    chat_id = pending_chat.pop(user_id, None)
    if not urls or chat_id is None:
        return

    count = len(urls)
    use_zip = count > ZIP_THRESHOLD

    status_msg = await context.bot.send_message(chat_id, f"⏳ Скачиваю 0 из {count} видео...")

    success = 0
    files_for_zip: list[tuple[str, str]] = []

    for i, url in enumerate(urls, 1):
        video_path = None
        try:
            video_data = await download_tiktok(url)
            if not video_data:
                await context.bot.send_message(chat_id, f"❌ Не удалось скачать видео {i}")
                continue

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(video_data)
                tmp_path = f.name

            loop = asyncio.get_event_loop()
            video_path = await loop.run_in_executor(None, reencode, tmp_path)

            if not video_path or not os.path.exists(video_path):
                await context.bot.send_message(chat_id, f"❌ Не удалось скачать видео {i}")
                continue

            size_mb = os.path.getsize(video_path) / (1024 * 1024)
            if size_mb > MAX_SIZE_MB:
                await context.bot.send_message(
                    chat_id, f"❌ Видео {i} слишком большое ({size_mb:.0f} МБ, лимит — {MAX_SIZE_MB} МБ)"
                )
                os.unlink(video_path)
                continue

            if use_zip:
                # Копим файлы для архива, подписываем цифрой
                files_for_zip.append((f"{i}.mp4", video_path))
            else:
                with open(video_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id,
                        document=f,
                        filename=f"{i}.mp4",
                        write_timeout=120,
                        read_timeout=120,
                    )
                os.unlink(video_path)

            success += 1
            try:
                await status_msg.edit_text(f"⏳ Скачиваю {success} из {count} видео...")
            except Exception:
                pass

        except Exception as e:
            await context.bot.send_message(chat_id, f"❌ Ошибка видео {i}: {str(e)[:300]}")
            if video_path and os.path.exists(video_path):
                try:
                    os.unlink(video_path)
                except Exception:
                    pass

    if use_zip and files_for_zip:
        try:
            await status_msg.edit_text(f"📦 Упаковываю {len(files_for_zip)} видео в архив(ы)...")
        except Exception:
            pass

        base_name = safe_zip_filename(zip_name).rsplit(".zip", 1)[0] if zip_name else "videos"

        # Разбиваем файлы на группы так, чтобы каждый архив не превышал лимит
        max_payload_bytes = MAX_ZIP_PAYLOAD_MB * 1024 * 1024
        parts: list[list[tuple[str, str]]] = []
        current_part: list[tuple[str, str]] = []
        current_size = 0

        for arcname, path in files_for_zip:
            file_size = os.path.getsize(path)
            if file_size > max_payload_bytes:
                # Само видео больше лимита одного архива — кладём отдельным архивом как есть
                if current_part:
                    parts.append(current_part)
                    current_part = []
                    current_size = 0
                parts.append([(arcname, path)])
                continue
            if current_part and current_size + file_size > max_payload_bytes:
                parts.append(current_part)
                current_part = []
                current_size = 0
            current_part.append((arcname, path))
            current_size += file_size

        if current_part:
            parts.append(current_part)

        total_parts = len(parts)
        sent_parts = 0
        zip_paths_to_cleanup: list[str] = []

        try:
            for part_idx, part_files in enumerate(parts, 1):
                zip_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zf:
                        zip_path = zf.name
                    zip_paths_to_cleanup.append(zip_path)

                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                        for arcname, path in part_files:
                            zf.write(path, arcname=arcname)

                    zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)

                    if total_parts > 1:
                        final_name = f"{base_name}_{part_idx}.zip"
                    else:
                        final_name = f"{base_name}.zip"

                    if zip_size_mb > MAX_SIZE_MB:
                        await context.bot.send_message(
                            chat_id,
                            f"❌ Часть {part_idx} получилась слишком большой "
                            f"({zip_size_mb:.0f} МБ). Попробуй прислать видео меньшими партиями.",
                        )
                        continue

                    if total_parts > 1:
                        try:
                            await status_msg.edit_text(
                                f"📤 Отправляю архив {part_idx} из {total_parts}..."
                            )
                        except Exception:
                            pass

                    with open(zip_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id,
                            document=f,
                            filename=final_name,
                            write_timeout=180,
                            read_timeout=180,
                        )
                    sent_parts += 1
                except Exception as e:
                    await context.bot.send_message(
                        chat_id, f"❌ Ошибка при отправке части {part_idx}: {str(e)[:300]}"
                    )
        finally:
            for zp in zip_paths_to_cleanup:
                if os.path.exists(zp):
                    try:
                        os.unlink(zp)
                    except Exception:
                        pass
            for _, path in files_for_zip:
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

    # Удаляем статусное сообщение вместо того чтобы оставлять "Готово!"
    try:
        await status_msg.delete()
    except Exception:
        pass


async def ask_zip_name_or_process(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Срабатывает после debounce: если ссылок больше порога — спрашивает
    имя архива и ждёт ответа, иначе сразу начинает скачивание."""
    urls = pending.get(user_id, [])
    if not urls:
        return

    if len(urls) > ZIP_THRESHOLD:
        chat_id = pending_chat.get(user_id)
        if chat_id is None:
            return
        awaiting_zip_name[user_id] = pending[user_id]
        # ссылки остаются в pending — process_urls заберёт их позже
        await context.bot.send_message(
            chat_id,
            f"📦 Нашёл {len(urls)} ссылок. Как назвать zip-архив? "
            f"(просто напиши имя, без расширения — добавлю .zip сам; "
            f"если видео не влезут в один архив — разобью на несколько частей)",
        )
    else:
        await process_urls(context, user_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    # Если бот ждёт от этого пользователя имя архива — это сообщение
    # считаем ответом, а не новой пачкой ссылок.
    if user_id in awaiting_zip_name:
        zip_name = text
        awaiting_zip_name.pop(user_id, None)
        await process_urls(context, user_id, zip_name=zip_name)
        return

    found = TIKTOK_RE.findall(text)

    if not found:
        await update.message.reply_text(
            "Отправь мне одну или несколько ссылок на TikTok — и я скачаю видео "
            "без водяного знака 🎬\n\n"
            f"Если ссылок больше {ZIP_THRESHOLD} (можно пересылать по одной подряд) — "
            "спрошу, как назвать архив, и пришлю всё одним zip-файлом, видео внутри "
            "будут пронумерованы (1, 2, 3...)."
        )
        return

    pending.setdefault(user_id, []).extend(found)
    pending_chat[user_id] = chat_id

    # Если уже была запланирована обработка — отменяем и планируем заново,
    # чтобы собрать ссылки из всех сообщений, присланных подряд.
    if user_id in pending_tasks:
        pending_tasks[user_id].cancel()

    async def delayed():
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await ask_zip_name_or_process(context, user_id)

    task = asyncio.ensure_future(delayed())
    pending_tasks[user_id] = task


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен...", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
