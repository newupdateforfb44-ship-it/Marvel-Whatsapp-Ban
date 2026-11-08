# bot.py
"""
WhatsApp Report Bot — Telegram frontend + Selenium (Chromium) backend.
Built for deployment on Render (Docker runtime).

Linking methods:
- QR code scan (photo sent in Telegram)
- Pair code (8-char code sent in Telegram)  <-- recommended for headless servers
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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.environ.get("8806455863:AAGRHE2BU2IJX7mLr288VT2ksObSB5mppDE", "").strip()

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
linked_flag = {"value": False}  # caches login state; reset on session expiry


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


async def reset_driver():
    """Kill and rebuild the browser (used when a session expires)."""
    global _driver, linked_flag
    async with _driver_lock:
        if _driver is not None:
            try:
                await asyncio.to_thread(_driver.quit)
            except Exception:
                pass
            _driver = None
    linked_flag["value"] = False


class LoginRequired(Exception):
    pass


def _is_logged_in_sync(driver) -> bool:
    """The chat search box only exists after a successful WhatsApp Web login."""
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
# 'Link with phone number' link on the QR login screen
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
    """Polls until logged in (or timeout). Pair-code users need this after typing the code."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _is_logged_in_sync(driver):
            return True
        time.sleep(2)
    return False


def get_pair_code_sync(driver, number: str):
    """
    Drives the 'Link with phone number' flow and returns the 8-char pair code.
    Returns the code string, or None on failure.
    """
    driver.get(WHATSAPP_URL)
    time.sleep(6)

    if _is_logged_in_sync(driver):
        _mark_logged_in()
        return "ALREADY_LINKED"

    # 1. Click 'Link with phone number'
    if not _click_first(driver, PAIR_LINK_XPATHS, timeout=20):
        logger.warning("Pair-code link not found on login screen")
        return None
    time.sleep(2)

    # 2. Enter the phone number into the input box
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

    # 3. Click 'Next'
    clicked = _click_first(driver, [
        "//button//div[contains(text(), 'Next')]",
        "//button[contains(., 'Next')]",
        "//div[@role='button']//*[contains(text(), 'Next')]",
    ], timeout=10)
    if not clicked:
        logger.warning("Next button not found for pair code")
        return None

    # 4. Wait for the 8-character code to appear and read it
    #    Codes look like 'AB12-CD34' or 'AB12CD34'.
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
        return  # ignore stray messages

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
       
