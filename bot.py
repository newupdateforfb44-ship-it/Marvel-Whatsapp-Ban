# bot.py
"""
WhatsApp Report Bot — Telegram frontend + Selenium (Chromium) backend.
Built for deployment on Render (Docker runtime).

Startup flow:
1. Reads the token from the TELEGRAM_BOT_TOKEN environment variable
   (Render → Environment), with an optional code fallback below.
2. VALIDATES the token with Telegram BEFORE starting polling, and
   logs the bot's @username so you know which bot to chat with.
3. Retries automatically on Conflict (old instance) and network errors.

Linking methods for WhatsApp:
- Pair Code (recommended: code arrives as text in Telegram)
- QR Code (photo sent in Telegram)
"""
import asyncio
import logging
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= CONFIGURATION =================
# OPTION A (recommended): set TELEGRAM_BOT_TOKEN in Render → Environment.
# OPTION B (fallback): paste your token between the quotes below.
# If both exist, the environment variable wins.
TOKEN_IN_CODE = ""  # e.g. "8801234567:AAE..."

TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or TOKEN_IN_CODE.strip())

PORT = int(os.environ.get("PORT", 10000))

# Persistent profile folder. Render disk is mounted at /data (see render.yaml).
DATA_DIR = "/data" if os.path.isdir("/data") else os.path.join(os.getcwd(), "chrome-profile")

WHATSAPP_URL = "https://web.whatsapp.com"
REPORTS_PER_RUN = int(os.environ.get("REPORTS_PER_RUN", "60"))
DELAY_BETWEEN_REPORTS = float(os.environ.get("DELAY_BETWEEN_REPORTS", "2"))
PROGRESS_UPDATE_INTERVAL = int(os.environ.get("PROGRESS_UPDATE_INTERVAL", "10"))
MAX_LOGIN_ATTEMPTS = 8
PAIR_CODE_TIMEOUT = 120  # seconds to wait for the user to enter the code on their phone

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ================= LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)   # hide token-bearing URLs from logs

# ================= STATE =================
_driver = None
_driver_lock = asyncio.Lock()

job = {
    "running": False,
    "cancel": False,
    "number": None,
    "done": 0,
    "total": 0,
    "success": 0,
}
lifetime_success = 0
linked_flag = {"value": False}


# ================= RENDER HEALTH CHECK =================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = b"WhatsApp Report Bot is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health check server listening on port %s", PORT)


def start_keepalive_ping():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return

    def _ping():
        while True:
            try:
                urllib.request.urlopen(url, timeout=30)
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=_ping, daemon=True).start()
    logger.info("Keep-alive pings started for %s", url)


# ================= SELENIUM LAYER =================
def _build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,900")
    options.add_argument(f"--user-data-dir={DATA_DIR}")
    options.add_argument(f"user-agent={UA}")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    cd_path = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(cd_path) if cd_path else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


async def get_driver():
    global _driver
    async with _driver_lock:
        if _driver is None:
            try:
                _driver = await asyncio.to_thread(_build_driver)
            except Exception as e:
                logger.error("Driver setup failed: %s", e)
                return None
        return _driver


class LoginRequired(Exception):
    pass


def _is_logged_in_sync(driver) -> bool:
    return bool(driver.find_elements(By.CSS_SELECTOR, "div[contenteditable='true'][data-tab='3']"))


def _mark_logged_in():
    linked_flag["value"] = True


MENU_BUTTONS = [
    "//button[@aria-label='Menu']",
    "//header//div[@aria-label='Menu']",
    "//div[@aria-label='Menu options']",
    "//div[@title='Menu options']",
    "//span[@data-icon='menu']",
]
REPORT_MENU_ITEMS = [
    "//ul//li[contains(., 'Report')]",
    "//div[@role='menuitem']//*[contains(., 'Report')]",
]
REPORT_CONFIRM_BUTTONS = [
    "//div[@role='dialog']//button[normalize-space()='Report']",
    "//div[@role='dialog']//div[@role='button'][normalize-space()='Report']",
    "//div[@role='dialog']//button[contains(., 'Report')]",
]
PAIR_LINK_XPATHS = [
    "//*[contains(text(), 'Link with phone number')]",
    "//a[contains(., 'Link with phone number')]",
    "//div[contains(text(), 'phone number')]",
]


def _click_first(driver, xpaths, timeout=12) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for xp in xpaths:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    try:
                        el.click()
                        return True
                    except Exception:
                        pass
        time.sleep(0.5)
    return False


def _wait_logged_in_sync(driver, seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _is_logged_in_sync(driver):
            return True
        time.sleep(2)
    return False


def get_pair_code_sync(driver, number: str):
    """Drives the 'Link with phone number' flow and returns the 8-char pair code."""
    driver.get(WHATSAPP_URL)
    time.sleep(6)

    if _is_logged_in_sync(driver):
        _mark_logged_in()
        return "ALREADY_LINKED"

    if not _click_first(driver, PAIR_LINK_XPATHS, timeout=20):
        logger.warning("Pair-code link not found on login screen")
        return None
    time.sleep(2)

    phone_input = None
    for _ in range(10):
        inputs = driver.find_elements(By.XPATH, "//input")
        inputs += driver.find_elements(By.XPATH, "//div[@contenteditable='true']")
        for el in inputs:
            if el.is_displayed():
                phone_input = el
                break
        if phone_input:
            break
        time.sleep(1)
    if phone_input is None:
        logger.warning("Phone input not found for pair code")
        return None

    try:
        phone_input.click()
        phone_input.clear()
        phone_input.send_keys(number)
    except Exception as e:
        logger.warning("Typing phone number failed: %s", e)
        return None
    time.sleep(1)

    clicked = _click_first(driver, [
        "//button//div[contains(text(), 'Next')]",
        "//button[contains(., 'Next')]",
        "//div[@role='button']//*[contains(text(), 'Next')]",
    ], timeout=10)
    if not clicked:
        logger.warning("Next button not found for pair code")
        return None

    code_re = re.compile(r"\b([A-Z0-9]{4}-?[A-Z0-9]{4})\b")
    deadline = time.time() + 30
    while time.time() < deadline:
        body = driver.find_element(By.TAG_NAME, "body").text
        m = code_re.search(body)
        if m:
            return m.group(1)
        time.sleep(2)
    logger.warning("Pair code did not appear in time")
    return None


def report_number_sync(driver, number) -> bool:
    """One report attempt. Runs inside a worker thread — safe to block."""
    try:
        driver.get(f"{WHATSAPP_URL}/send/?phone={number}")
        ok_page = False
        for _ in range(30):
            if _is_logged_in_sync(driver):
                ok_page = True
                break
            time.sleep(1)
        if not ok_page:
            linked_flag["value"] = False
            raise LoginRequired()
        _mark_logged_in()

        time.sleep(2)
        if not _click_first(driver, MENU_BUTTONS):
            return False
        time.sleep(1)
        if not _click_first(driver, REPORT_MENU_ITEMS):
            return False
        deadline = time.time() + 10
        while time.time() < deadline:
            if driver.find_elements(By.XPATH, "//div[@role='dialog']"):
                break
            time.sleep(0.5)
        if not _click_first(driver, REPORT_CONFIRM_BUTTONS, timeout=10):
            return False
        time.sleep(1)
        return True
    except LoginRequired:
        raise
    except Exception as e:
        logger.warning("Report attempt failed: %s", e)
        return False


def _qr_screenshot(driver) -> bytes:
    for c in driver.find_elements(By.TAG_NAME, "canvas"):
        if c.is_displayed():
            try:
                png = c.screenshot_as_png
                if png:
                    return png
            except Exception:
                pass
    return driver.get_screenshot_as_png()


# ================= TELEGRAM UI =================
MENU_TEXT = (
    "👋 WhatsApp Report Bot\n\n"
    "Choose an option below:\n\n"
    "🔹 Developed by Marvel"
)


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Reporting", callback_data="start_report")],
        [InlineKeyboardButton("🔗 Link via Pair Code", callback_data="pair")],
        [InlineKeyboardButton("📷 Link via QR Code", callback_data="login")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("📝 Help", callback_data="help")],
    ])


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="back")]])


def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel", callback_data="cancel")]])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, reply_markup=main_menu_keyboard())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, reply_markup=main_menu_keyboard())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step"):
        context.user_data["step"] = None
        await update.message.reply_text("✅ Cancelled.", reply_markup=main_menu_keyboard())
    elif job["running"]:
        job["cancel"] = True
        await update.message.reply_text("🛑 Cancelling after the current report…")
    else:
        await update.message.reply_text("Nothing to cancel.", reply_markup=main_menu_keyboard())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "back":
        await query.edit_message_text(MENU_TEXT, reply_markup=main_menu_keyboard())

    elif data == "pair":
        if linked_flag["value"]:
            await query.edit_message_text(
                "✅ WhatsApp is already linked — you can start reporting right away!",
                reply_markup=main_menu_keyboard(),
            )
        else:
            context.user_data["step"] = "waiting_pair_number"
            await query.edit_message_text(
                "🔗 Pair-code linking\n\n"
                "Send me YOUR WhatsApp number with country code (e.g. 2348012345678).\n"
                "I'll generate a code you type into WhatsApp on your phone.",
                reply_markup=back_kb(),
            )

    elif data == "login":
        await query.edit_message_text("📱 Opening WhatsApp Web for QR login…")
        asyncio.create_task(qr_login_flow(query.message.chat_id, context))

    elif data == "start_report":
        if job["running"]:
            await query.edit_message_text(
                f"⏳ A report job is already running ({job['done']}/{job['total']}). "
                f"Wait for it to finish or send /cancel.",
                reply_markup=back_kb(),
            )
        else:
            context.user_data["step"] = "waiting_number"
            await query.edit_message_text(
                "📲 Send the WhatsApp number to report (with country code).\n\nExample: 2349049611574",
                reply_markup=back_kb(),
            )

    elif data == "status":
        phone = f"+{job['number']}" if job["number"] else "—"
        await query.edit_message_text(
            "📊 Status\n\n"
            f"🔗 WhatsApp linked: {'yes' if linked_flag['value'] else 'no — use Link via Pair Code'}\n"
            f"🟢 Report job running: {'yes' if job['running'] else 'no'}\n"
            f"📞 Number: {phone}\n"
            f"🔄 This run: {job['success']}/{job['total']} successful\n"
            f"📈 Lifetime successful reports: {lifetime_success}\n\n"
            "🔹 Developed by Marvel",
            reply_markup=back_kb(),
        )

    elif data == "about":
        await query.edit_message_text(
            "🤖 Bot Information\n\n"
            "Function: Automated WhatsApp Reporting\n"
            f"Reports per run: {REPORTS_PER_RUN}\n"
            "Ban chance: high with multiple reports\n\n"
            "🔹 Developed by Marvel",
            reply_markup=back_kb(),
        )

    elif data == "help":
        await query.edit_message_text(
            "❓ How to use:\n\n"
            "1️⃣ LINK (one time):\n"
            "   • Tap 🔗 Link via Pair Code, send YOUR number,\n"
            "     then type the code the bot gives you into:\n"
            "     WhatsApp → Linked devices → Link a device →\n"
            "     'Link with phone number instead'\n"
            "   • Or tap 📷 Link via QR Code and scan.\n"
            "2️⃣ Tap 🚀 Start Reporting and send the target number.\n"
            "3️⃣ Send /cancel anytime to stop.\n"
            "4️⃣ Tap 📊 Status anytime.\n\n"
            "🔹 Developed by Marvel",
            reply_markup=back_kb(),
        )

    elif data == "cancel":
        job["cancel"] = True
        await query.answer("🛑 Cancelling…")


def _clean_number(text: str):
    digits = re.sub(r"\D", "", text)
    return digits if 8 <= len(digits) <= 15 else None


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if not step:
        return

    number = _clean_number(update.message.text)
    if not number:
        await update.message.reply_text(
            "❌ That doesn't look like a phone number.\n"
            "Send digits with country code, e.g. 2349049611574"
        )
        return
    context.user_data["step"] = None

    if step == "waiting_pair_number":
        await update.message.reply_text(f"🔗 Generating pair code for +{number}…")
        asyncio.create_task(pair_code_flow(update.effective_chat.id, number, context))
        return

    if step == "waiting_number":
        if job["running"]:
            await update.message.reply_text("⏳ Busy — a job is already running.", reply_markup=back_kb())
            return
        await update.message.reply_text(f"⚙️ Starting report job for +{number}…")
        context.application.bot_data["job_task"] = asyncio.create_task(
            run_report_job(update.effective_chat.id, number, context)
        )


# ================= PAIR-CODE LOGIN FLOW =================
async def pair_code_flow(chat_id, number, context):
    driver = await get_driver()
    if driver is None:
        await context.bot.send_message(chat_id, "⚠️ Could not start the browser. Check the Render logs.", reply_markup=back_kb())
        return

    try:
        code = await asyncio.to_thread(get_pair_code_sync, driver, number)
    except Exception as e:
        logger.warning("Pair-code flow crashed: %s", e)
        code = None

    if code == "ALREADY_LINKED":
        await context.bot.send_message(
            chat_id,
            "✅ WhatsApp is already linked — no need for a pair code!",
            reply_markup=main_menu_keyboard(),
        )
        return
    if not code:
        await context.bot.send_message(
            chat_id,
            "❌ Couldn't generate a pair code (WhatsApp's login screen may have changed).\n"
            "Try 📷 Link via QR Code instead, or try again in a minute.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await context.bot.send_message(
        chat_id,
        f"🔑 YOUR PAIR CODE:  {code}\n\n"
        f"On your phone NOW:\n"
        f"1. Open WhatsApp\n"
        f"2. ⚙️ Settings → Linked devices\n"
        f"3. Link a device\n"
        f"4. Tap 'Link with phone number instead'\n"
        f"5. Type:  {code}\n\n"
        f"⏱️ You have ~2 minutes!"
    )

    ok = await asyncio.to_thread(_wait_logged_in_sync, driver, PAIR_CODE_TIMEOUT)
    if ok:
        _mark_logged_in()
        await context.bot.send_message(
            chat_id,
            "✅ WhatsApp linked successfully via pair code!\nYou can now use 🚀 Start Reporting.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await context.bot.send_message(
            chat_id,
            "❌ Pair-code login timed out. Tap 🔗 Link via Pair Code to try again with a fresh code.",
            reply_markup=main_menu_keyboard(),
        )


# ================= QR LOGIN FLOW =================
async def qr_login_flow(chat_id, context):
    driver = await get_driver()
    if driver is None:
        await context.bot.send_message(chat_id, "⚠️ Could not start the browser. Check the Render logs.", reply_markup=back_kb())
        return

    await context.bot.send_message(
        chat_id,
        "📷 On your phone: WhatsApp → ⚙️ Settings → Linked devices → Link a device.\n"
        "Scan the QR code in the photo I send next — QR codes expire fast!"
    )

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(driver.get, WHATSAPP_URL)
        except Exception as e:
            logger.warning("Could not load WhatsApp Web: %s", e)
            await asyncio.sleep(3)
            continue

        await asyncio.sleep(6)

        for _ in range(20):
            if await asyncio.to_thread(_is_logged_in_sync, driver):
                _mark_logged_in()
                await context.bot.send_message(
                    chat_id,
                    "✅ WhatsApp linked successfully!\nYou can now use 🚀 Start Reporting.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            await asyncio.sleep(1)

        try:
            png = await asyncio.to_thread(_qr_screenshot, driver)
            await context.bot.send_p


================= QR LOGIN FLOW =================
async def qr_login_flow(chat_id, context):
    driver = await get_driver()
    if driver is None:
        await context.bot.send_message(chat_id, "⚠️ Could not start the browser. Check the Render logs.", reply_markup=back_kb())
        return

    await context.bot.send_message(
        chat_id,
        "📷 On your phone: WhatsApp → ⚙️ Settings → Linked devices → Link a device.\n"
        "Scan the QR code in the photo I send next — QR codes expire fast!"
    )

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(driver.get, WHATSAPP_URL)
        except Exception as e:
            logger.warning("Could not load WhatsApp Web: %s", e)
            await asyncio.sleep(3)
            continue

        await asyncio.sleep(6)

        for _ in range(20):
            if await asyncio.to_thread(_is_logged_in_sync, driver):
                _mark_logged_in()
                await context.bot.send_message(
                    chat_id,
                    "✅ WhatsApp linked successfully!\nYou can now use 🚀 Start Reporting.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            await asyncio.sleep(1)

        try:
            png = await asyncio.to_thread(_qr_screenshot, driver)
            await context.bot.send_photo(chat_id, png, caption=f"📷 QR code {attempt}/{MAX_LOGIN_ATTEMPTS} — scan it now!")
        except Exception as e:
            logger.warning("QR screenshot failed: %s", e)
            continue

        for _ in range(25):
            await asyncio.sleep(1)
            if await asyncio.to_thread(_is_logged_in_sync, driver):
                _mark_logged_in()
                await context.bot.send_message(
                    chat_id,
                    "✅ WhatsApp linked successfully!\nYou can now use 🚀 Start Reporting.",
                    reply_markup=main_menu_keyboard(),
                )
                return
        break

    await context.bot.send_message(
        chat_id,
        "❌ QR login timed out. Try 🔗 Link via Pair Code instead — it's easier.",
        reply_markup=main_menu_keyboard(),
    )


# ================= REPORT JOB =================
async def run_report_job(chat_id, number, context):
    global lifetime_success
    job.update(running=True, cancel=False, number=number, done=0, total=REPORTS_PER_RUN, success=0)

    driver = await get_driver()
    if driver is None:
        job["running"] = False
        await context.bot.send_message(chat_id, "⚠️ Could not start the browser. Check the Render logs.", reply_markup=back_kb())
        return

    status = await context.bot.send_message(chat_id, "🔎 Checking WhatsApp login…", reply_markup=cancel_kb())

    logged_in = False
    try:
        await asyncio.to_thread(driver.get, WHATSAPP_URL)
        for _ in range(20):
            await asyncio.sleep(1)
            if await asyncio.to_thread(_is_logged_in_sync, driver):
                logged_in = True
                break
    except Exception as e:
        logger.warning("Login check failed: %s", e)

    if not logged_in:
        job["running"] = False
        await status.edit_text(
            "⚠️ WhatsApp is not linked.\nTap 🔗 Link via Pair Code (easiest) or 📷 Link via QR Code, then try again.",
            reply_markup=back_kb(),
        )
        return

    _mark_logged_in()
    await status.edit_text("✅ WhatsApp linked. Reporting…", reply_markup=cancel_kb())

    for i in range(job["total"]):
        if job["cancel"]:
            break
        try:
            ok = await asyncio.to_thread(report_number_sync, driver, number)
        except LoginRequired:
            job["running"] = False
            linked_flag["value"] = False
            await status.edit_text(
                "⚠️ WhatsApp session expired mid-run.\nUse 🔗 Link via Pair Code to link again.",
                reply_markup=back_kb(),
            )
            return

        job["done"] = i + 1
        if ok:
            job["success"] += 1
            lifetime_success += 1

        if job["done"] % PROGRESS_UPDATE_INTERVAL == 0 or job["done"] == job["total"]:
            try:
                await status.edit_text(
                    f"🔄 Progress: {job['done']}/{job['total']} — ✅ {job['success']} successful",
                    reply_markup=cancel_kb(),
                )
            except Exception:
                pass

        await asyncio.sleep(DELAY_BETWEEN_REPORTS)

    await context.bot.send_message(
        chat_id,
        f"{'🛑 Cancelled!' if job['cancel'] else '✅ Process complete!'}\n"
        f"Number: +{number}\n"
        f"Reports sent: {job['success']}/{job['done']}\n"
        f"Lifetime successful reports: {lifetime_success}\n\n"
        "🔹 Developed by Marvel",
        reply_markup=back_kb(),
    )
    job["running"] = False


# ================= MAIN =================
async def on_error(update, context):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Something went wrong. Try again from /menu.")
    except Exception:
        pass


def validate_token_sync(token: str):
    """
    Talks to Telegram BEFORE the polling loop starts.
    Returns the bot's username on success, raises on failure.
    """
    bot = Bot(token)
    me = bot.get_me()  # sync call inside our startup thread; blocking is fine here
    return me.username, me.id


def main():
    if not TELEGRAM_BOT_TOKEN:
        # Super clear message so the fix is obvious from Render logs alone
        logger.error("=" * 60)
        logger.error("NO BOT TOKEN FOUND!")
        logger.error("Set TELEGRAM_BOT_TOKEN in Render → Environment, or paste")
        logger.error("it into TOKEN_IN_CODE at the top of bot.py")
        logger.error("=" * 60)
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")

    # ---- Validate the token with Telegram before doing anything else ----
    try:
        username, bot_id = validate_token_sync(TELEGRAM_BOT_TOKEN)
    except InvalidToken:
        logger.error("=" * 60)
        logger.error("TOKEN REJECTED BY TELEGRAM (401 Unauthorized)!")
        logger.error("The token in Render → Environment is old, revoked, or mistyped.")
        logger.error("Fix: open @BotFather → /mybots → your bot → API Token,")
        logger.error("copy the CURRENT token, paste it into Render Environment,")
        logger.error("then Save & Deploy.")
        logger.error("=" * 60)
        raise SystemExit("Invalid bot token")
    except Conflict:
        logger.warning("Another instance is using this token — but startup Conflict here is rare; continuing.")
    except Exception as e:
        logger.warning("Could not verify token yet (network issue?): %s — will retry during startup", e)

    logger.info("=" * 60)
    logger.info("TOKEN IS VALID ✅")
    logger.info("This bot is: @%s (ID: %s)", username, bot_id)
    logger.info("Open https://t.me/%s and send /start to use it!", username)
    logger.info("=" * 60)

    os.makedirs(DATA_DIR, exist_ok=True)
    start_health_server()
    start_keepalive_ping()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # Retry loop: Conflict (another instance) and network hiccups no longer
    # kill the bot — it waits and retries instead of "Application exited early".
    for attempt in range(1, 13):  # up to ~12 attempts ≈ 6 minutes of retrying
        try:
            logger.info("Bot is up — polling Telegram… (attempt %d)", attempt)
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            break  # clean shutdown
        except Conflict:
            logger.warning(
                "Attempt %d: another bot instance is still polling with this token. "
                "Waiting 20s for it to die… (this fixes itself after a revoke/restart)", attempt
            )
            time.sleep(20)
        except Exception as e:
            logger.warning("Attempt %d: startup failed: %s — retrying in 15s", attempt, e)
            time.sleep(15)
    else:
        raise SystemExit("Could not start polling after 12 attempts. Check Render logs above.")


if __name__ == "__main__":
    main()
