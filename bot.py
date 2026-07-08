import os
import sys
import json
import logging
import zipfile
import io
from datetime import datetime
from collections import defaultdict

from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

from checker import (
    load_proxies,
    build_session,
    check_session,
    log,
    PROXY_FILE
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    log.error("BOT_TOKEN не задан в .env")
    sys.exit(1)

user_files = defaultdict(list)

bot_logger = logging.getLogger("bot")
bot_logger.setLevel(logging.INFO)
fh = logging.FileHandler("bot.log", encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
bot_logger.addHandler(fh)

# Допустимые расширения для файлов с куками
ALLOWED_EXTENSIONS = {'.json', '.txt', '.cookie', '.cookies', '.dat'}

def check_one_account(cookies: list, proxy_dict: dict = None) -> dict:
    try:
        session = build_session(cookies, proxy_dict)
        result = check_session(session, "temp")
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я помогу проверить аккаунты Kleinanzeigen на валидность.\n\n"
        "📤 Отправь мне файлы с куками (JSON, TXT, COOKIE, DAT) или ZIP-архив с ними.\n"
        "После загрузки всех файлов напиши команду /done, чтобы начать проверку.\n\n"
        "Команды:\n"
        "/start — показать это сообщение\n"
        "/done — запустить проверку всех загруженных аккаунтов\n"
        "/send — получить ZIP-архив с валидными куками\n"
        "/cancel — очистить загруженные файлы"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    user_id = update.effective_user.id

    if doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ Файл слишком большой (макс. 5 МБ)")
        return

    filename = doc.file_name
    # Обработка ZIP
    if filename.lower().endswith(".zip"):
        await update.message.reply_text("📦 Обрабатываю ZIP-архив...")
        try:
            file = await doc.get_file()
            raw = await file.download_as_bytearray()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                count = 0
                for item in zf.namelist():
                    if any(item.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
                        with zf.open(item) as f:
                            data = json.load(f)
                            if isinstance(data, list):
                                user_files[user_id].append((item, data))
                                count += 1
                await update.message.reply_text(f"✅ Добавлено {count} файлов из ZIP.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка обработки ZIP: {e}")
        return

    # Проверка расширения
    if not any(filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
        await update.message.reply_text(
            f"❌ Поддерживаются только файлы с расширениями: {', '.join(ALLOWED_EXTENSIONS)}"
        )
        return

    try:
        file = await doc.get_file()
        raw = await file.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения файла: {e}")
        return

    if not isinstance(data, list):
        await update.message.reply_text("❌ Файл должен содержать массив кук (список объектов).")
        return

    user_files[user_id].append((filename, data))
    await update.message.reply_text(f"✅ Файл {filename} загружен. Всего: {len(user_files[user_id])}")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_files or not user_files[user_id]:
        await update.message.reply_text("❌ Нет загруженных аккаунтов. Сначала отправь файлы.")
        return

    await update.message.reply_text(f"⏳ Начинаю проверку {len(user_files[user_id])} аккаунтов...")

    proxies = load_proxies(PROXY_FILE)
    if not proxies:
        await update.message.reply_text("❌ Нет прокси для проверки. Обратитесь к администратору.")
        return

    results = []
    for idx, (filename, cookies) in enumerate(user_files[user_id]):
        proxy_dict = proxies[idx % len(proxies)]
        result = check_one_account(cookies, proxy_dict)
        result['filename'] = filename
        results.append(result)

    context.user_data['last_results'] = results
    valid_cookies = [cookies for (_, cookies), res in zip(user_files[user_id], results) if res.get('status') == 'valid']
    context.user_data['valid_cookies'] = valid_cookies

    valid_count = sum(1 for r in results if r.get('status') == 'valid')
    invalid_count = sum(1 for r in results if r.get('status') == 'invalid')
    blocked_count = sum(1 for r in results if r.get('status') == 'blocked')
    error_count = sum(1 for r in results if r.get('status') == 'error')

    summary = f"✅ Валидных: {valid_count}\n❌ Невалидных: {invalid_count}\n🚫 Заблокировано: {blocked_count}\n⚠️ Ошибок: {error_count}"

    report_lines = []
    for i, res in enumerate(results):
        status = res.get('status', 'error')
        if status == 'valid':
            email = res.get('email', '—')
            name = res.get('name', '—')
            userid = res.get('userid', '—')
            rating = res.get('rating', '—')
            reg_date = res.get('reg_date', '—')
            acc_type = res.get('type', '—')
            report_lines.append(
                f"✅ {res.get('filename', f'account_{i}')}\n"
                f"email: {email}\n"
                f"name: {name}\n"
                f"userid: {userid}\n"
                f"rating: {rating}\n"
                f"reg-date: {reg_date}\n"
                f"type: {acc_type}\n"
            )
        elif status == 'invalid':
            report_lines.append(f"❌ {res.get('filename', f'account_{i}')} — невалидный")
        elif status == 'blocked':
            report_lines.append(f"🚫 {res.get('filename', f'account_{i}')} — прокси заблокирован")
        else:
            report_lines.append(f"⚠️ {res.get('filename', f'account_{i}')} — ошибка: {res.get('detail', 'неизвестно')}")

    context.user_data['summary'] = summary
    context.user_data['report_lines'] = report_lines

    if len(results) <= 10:
        full_report = summary + "\n\n" + "\n".join(report_lines)
        if len(full_report) > 4096:
            for i in range(0, len(full_report), 4096):
                await update.message.reply_text(full_report[i:i+4096])
        else:
            await update.message.reply_text(full_report)
    else:
        keyboard = [
            [InlineKeyboardButton("📦 Получить ZIP с валидными", callback_data="send_zip")],
            [InlineKeyboardButton("📄 Получить результаты по одному", callback_data="send_one_by_one")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📊 Проверка завершена.\n{summary}\n\nВыбери способ получения результатов:",
            reply_markup=reply_markup
        )

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'valid_cookies' not in context.user_data or not context.user_data['valid_cookies']:
        await update.message.reply_text("❌ Нет валидных аккаунтов для отправки. Сначала выполни /done.")
        return

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        for idx, cookies in enumerate(context.user_data['valid_cookies']):
            zf.writestr(f"account_{idx+1}.json", json.dumps(cookies, ensure_ascii=False, indent=2))

    zip_buffer.seek(0)
    await update.message.reply_document(
        document=zip_buffer,
        filename=f"valid_cookies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_files:
        del user_files[user_id]
    context.user_data.clear()
    await update.message.reply_text("🗑️ Все загруженные файлы очищены.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "send_zip":
        await send_command(update, context)
    elif query.data == "send_one_by_one":
        if 'report_lines' in context.user_data:
            for line in context.user_data['report_lines']:
                await update.message.reply_text(line)
        else:
            await update.message.reply_text("Нет данных.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_callback))

    bot_logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
