#!/usr/bin/env python3
"""TAXIDRIVE24/7: two Telegram bots, one shared order queue.

Passenger bot: from -> to -> preview -> send to drivers.
Driver bot: registration -> online -> first accept wins.

The whole thing runs with the Python standard library and SQLite. No webhook,
no external services, no paid APIs.
"""

import argparse
import hmac
import logging
import os
import re
import signal
import sys
import threading
import time

from admin import (
    admin_drivers_text,
    admin_keyboard,
    admin_orders_text,
    admin_panel_text,
)
from core import (
    EMPTY_KEYBOARD,
    TaxiService,
    confirm_cancel_keyboard,
    confirm_cancel_text,
    driver_accepted_text,
    driver_done_keyboard,
    esc,
    fine_how_to_text,
    fine_receipt_ok_text,
    fine_receipt_wrong_type_text,
    fine_requisites_text,
    fine_text,
    passenger_accepted_text,
    passenger_driver_keyboard,
    passenger_search_keyboard,
    passenger_search_text,
    public_id,
)
from store import Store
from tgapi import BotAPI, TelegramError

log = logging.getLogger("taxi")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------- helpers
def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                # .env is the source of truth: override inherited values so a
                # hot reload (os.execv keeps the old environment) picks up edits.
                os.environ[key] = value


def button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def inline_keyboard(rows):
    return {"inline_keyboard": rows}


def force_reply(placeholder):
    return {"force_reply": True, "input_field_placeholder": placeholder}


CONTACT_KEYBOARD = {
    "keyboard": [
        [{"text": "📱 Отправить телефон", "request_contact": True}],
        [{"text": "⏭ Пропустить"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

REMOVE_KEYBOARD = {"remove_keyboard": True}

KEEP_WORDS = {
    "-",
    "—",
    "–",
    "оставить",
    "оставить как есть",
    "не менять",
    "пропустить",
    "skip",
}


def is_keep(text):
    """True when the user asks to keep the current profile value."""
    value = " ".join((text or "").split()).strip().lower()
    if value in KEEP_WORDS:
        return True
    return "пропустить" in value or "skip" in value


def display_name(user):
    if not user:
        return None
    name = " ".join(
        p for p in (user.get("first_name"), user.get("last_name")) if p
    ).strip()
    return name or user.get("username") or str(user.get("id"))


def clean_address(text):
    value = " ".join((text or "").split())
    if len(value) < 2 or len(value) > 200:
        return None
    return value


def clean_text(text, minimum, maximum):
    value = " ".join((text or "").split())
    if len(value) < minimum or len(value) > maximum:
        return None
    return value


def normalize_phone(text):
    raw = " ".join((text or "").split())
    lowered = raw.lower()
    if lowered == "-" or "пропустить" in lowered or "skip" in lowered:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def is_private_message(msg):
    return (msg.get("chat") or {}).get("type") == "private"


class BotBase:
    name = "bot"
    commands = []

    def __init__(self, api, store, service, stop_event, admin_code=""):
        self.api = api
        self.store = store
        self.service = service
        self.stop_event = stop_event
        self.admin_code = admin_code or ""

    # ------------------------------------------------------------------- loop
    def run(self):
        me = self.api.get_me()
        log.info(
            "%s started: @%s id=%s", self.name, me.get("username"), me.get("id")
        )
        try:
            self.api.set_my_commands(self.commands)
        except TelegramError as exc:
            log.warning("%s setMyCommands: %s", self.name, exc)

        offset_key = f"offset:{self.name}"
        offset_raw = self.store.meta_get(offset_key)
        offset = int(offset_raw) if offset_raw else None

        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(
                    offset,
                    timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
            except TelegramError as exc:
                if exc.code == 409:
                    log.error("%s: another process is polling this token", self.name)
                    time.sleep(10)
                else:
                    log.warning("%s getUpdates: %s", self.name, exc)
                    time.sleep(3)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("%s update %s failed", self.name, update.get("update_id"))
            if updates:
                self.store.meta_set(offset_key, offset)

        log.info("%s stopped", self.name)

    def handle_update(self, update):
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        msg = update.get("message")
        if msg:
            self.handle_message(msg)

    # --------------------------------------------------------------- API sugar
    def send(self, chat_id, text, reply_markup=None):
        try:
            return self.api.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramError as exc:
            log.warning("%s send to %s failed: %s", self.name, chat_id, exc)
            return None

    def edit(self, chat_id, message_id, text, reply_markup=None):
        try:
            self.api.edit_message_text(
                chat_id, message_id, text, reply_markup=reply_markup
            )
            return True
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning("%s edit %s/%s failed: %s", self.name, chat_id, message_id, exc)
            return False

    def delete(self, chat_id, message_id):
        try:
            self.api.delete_message(chat_id, message_id)
        except TelegramError as exc:
            log.debug("%s delete %s/%s failed: %s", self.name, chat_id, message_id, exc)

    def answer(self, callback, text=None, alert=False):
        try:
            self.api.answer_callback_query(
                callback["id"], text=text, show_alert=alert
            )
        except TelegramError as exc:
            log.debug("%s answerCallbackQuery failed: %s", self.name, exc)

    # ----------------------------------------------------------------- admins
    def begin_admin_login(self, chat_id):
        if not self.admin_code:
            self.send(
                chat_id,
                "Админка пока не настроена. Добавьте ADMIN_INVITE_CODE в .env.",
            )
            return
        self.store.set_session(self.name, chat_id, "admin_code", {})
        self.send(
            chat_id,
            "🔐 <b>Админка TAXIDRIVE24/7</b>\n\n"
            "Введите код администратора.",
            force_reply("Код администратора"),
        )

    def admin_code_step(self, chat_id, text, user):
        code = (text or "").strip()
        if not hmac.compare_digest(
            code.encode("utf-8"), self.admin_code.encode("utf-8")
        ):
            self.send(chat_id, "Код не подошёл. Попробуйте ещё раз.")
            return
        self.store.add_admin(chat_id, display_name(user), user.get("username"))
        self.store.clear_session(self.name, chat_id)
        self.send(chat_id, "✅ Доступ к админке открыт.")
        self.show_admin_panel(chat_id)

    def show_admin_panel(self, chat_id, edit_message_id=None):
        if not self.store.is_admin(chat_id):
            self.begin_admin_login(chat_id)
            return
        text = admin_panel_text(self.store)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, admin_keyboard())
        else:
            self.send(chat_id, text, admin_keyboard())

    def show_admin_drivers(self, chat_id, edit_message_id=None):
        if not self.store.is_admin(chat_id):
            self.begin_admin_login(chat_id)
            return
        text = admin_drivers_text(self.store)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, admin_keyboard())
        else:
            self.send(chat_id, text, admin_keyboard())

    def show_admin_orders(self, chat_id, edit_message_id=None):
        if not self.store.is_admin(chat_id):
            self.begin_admin_login(chat_id)
            return
        text = admin_orders_text(self.store)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, admin_keyboard())
        else:
            self.send(chat_id, text, admin_keyboard())

    def handle_admin_callback(self, callback, data, message_id):
        chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
        if not chat_id:
            self.answer(callback)
            return
        if not self.store.is_admin(chat_id):
            self.answer(
                callback,
                "Нужен код администратора. Введите /admin.",
                alert=True,
            )
            return
        if data == "a:panel":
            self.answer(callback)
            self.show_admin_panel(chat_id, message_id)
            return
        if data == "a:drivers":
            self.answer(callback)
            self.show_admin_drivers(chat_id, message_id)
            return
        if data == "a:orders":
            self.answer(callback)
            self.show_admin_orders(chat_id, message_id)
            return
        if data == "a:back":
            self.answer(callback)
            self.send_main(chat_id, message_id)
            return
        self.answer(callback)

    # ---------------------------------------------------------------- commands
    def cmd_admin(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.store.clear_session(self.name, chat_id)
        if self.store.is_admin(chat_id):
            self.show_admin_panel(chat_id)
        else:
            self.begin_admin_login(chat_id)

    # ---------------------------------------------------------------- commands
    def maybe_command(self, msg, text):
        if not text.startswith("/"):
            return False
        head, _, args = text.partition(" ")
        name = head[1:].split("@", 1)[0].lower()
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return False
        handler(msg, args.strip())
        return True

    # --------------------------------------------------------------- abstract
    def handle_message(self, msg):
        raise NotImplementedError

    def handle_callback(self, callback):
        raise NotImplementedError


# =============================================================== passenger bot
class PassengerBot(BotBase):
    name = "passenger"
    commands = [
        ("start", "Заказать такси"),
        ("status", "Мой заказ"),
        ("cancel", "Отменить заказ"),
        ("help", "Помощь"),
        ("admin", "Админка"),
    ]

    def handle_message(self, msg):
        if not is_private_message(msg):
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        self.store.ensure_passenger(
            chat_id, display_name(user), user.get("username")
        )

        fine = self.store.active_fine(chat_id)
        if fine:
            self.blocked_message(chat_id, fine, msg)
            return

        text = msg.get("text")
        if self.maybe_command(msg, text or ""):
            return

        session = self.store.get_session(self.name, chat_id)
        state = session["state"] if session else None

        if state == "admin_code":
            self.admin_code_step(chat_id, text, user)
            return

        if state in {"from", "to"}:
            if not text:
                self.send(chat_id, "Пожалуйста, напишите ответ обычным сообщением.")
                return
            if state == "from":
                address = clean_address(text)
                if not address:
                    self.send(
                        chat_id,
                        "Не получилось разобрать адрес. Напишите его ещё раз.",
                        force_reply("Откуда"),
                    )
                    return
                data = dict(session.get("data") or {})
                data["from"] = address
                self.store.set_session(self.name, chat_id, "to", data)
                self.send(
                    chat_id,
                    "🏁 <b>Куда поедете?</b>\n\nНапишите адрес, куда вас отвезти.",
                    force_reply("Куда"),
                )
                return

            if state == "to":
                address = clean_address(text)
                if not address:
                    self.send(
                        chat_id,
                        "Не получилось разобрать адрес. Напишите его ещё раз.",
                        force_reply("Куда"),
                    )
                    return
                data = dict(session.get("data") or {})
                data["to"] = address
                self.store.set_session(self.name, chat_id, "preview", data)
                self.send_preview(chat_id, data)
                return

        if state == "distance":
            # Совместимость со старыми сессиями, где бот спрашивал километры.
            data = dict(session.get("data") or {})
            self.store.set_session(self.name, chat_id, "preview", data)
            self.send_preview(chat_id, data)
            return

        if state == "preview":
            self.send(
                chat_id,
                "Осталось отправить заказ. Нажмите «✅ Отправить водителям» "
                "или «❌ Отмена» под карточкой.",
            )
            return

        self.send_main(chat_id)

    def handle_callback(self, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id = chat["id"]
        user = callback.get("from") or {}
        if user.get("id") != chat_id:
            self.answer(callback, "Не получится: это чужой заказ.", alert=True)
            return
        self.store.ensure_passenger(
            chat_id, display_name(user), user.get("username")
        )

        data = callback.get("data") or ""
        parts = data.split(":")
        message_id = message.get("message_id")

        if data.startswith("a:"):
            self.handle_admin_callback(callback, data, message_id)
            return

        blocked = self.store.active_fine(chat_id)
        if blocked and data != "p:receipt":
            self.answer(callback, "Сначала оплатите штраф", alert=True)
            self.send_fine_screen(chat_id, blocked)
            return

        if data == "p:new":
            self.answer(callback)
            self.start_order(chat_id)
            return

        if data == "p:edit":
            self.answer(callback, "Давайте заново")
            self.store.set_session(self.name, chat_id, "from", {})
            if message_id:
                self.edit(
                    chat_id,
                    message_id,
                    "✏️ <b>Начнём заново</b>",
                    EMPTY_KEYBOARD,
                )
            self.send(
                chat_id,
                "📍 <b>Откуда поедете?</b>\n\nНапишите адрес, откуда забрать.",
                force_reply("Откуда"),
            )
            return

        if data == "p:drop":
            self.answer(callback, "Хорошо, отменил")
            self.store.clear_session(self.name, chat_id)
            if message_id:
                self.edit(
                    chat_id,
                    message_id,
                    "❌ <b>Заказ отменён</b>\n\nЕсли снова понадобится машина, я рядом.",
                    EMPTY_KEYBOARD,
                )
            return

        if data == "p:send":
            self.finish_preview(chat_id, callback, message_id)
            return

        if parts[:1] == ["p"] and len(parts) == 3 and parts[1] == "cancel":
            try:
                order_id = int(parts[2])
            except ValueError:
                self.answer(callback)
                return
            order = self.store.get_order(order_id)
            if order and order["status"] == "accepted":
                self.answer(callback)
                self.send(
                    chat_id,
                    confirm_cancel_text(order, self.service.fine_amount),
                    confirm_cancel_keyboard(order),
                )
                return
            result = self.service.cancel_order(order_id, chat_id)
            self.answer(
                callback,
                "Заказ отменён" if result else "Заказ уже приняли или отменили",
                alert=not result,
            )
            return

        if parts[:1] == ["p"] and len(parts) == 3 and parts[1] == "cancel2":
            try:
                order_id = int(parts[2])
            except ValueError:
                self.answer(callback)
                return
            result = self.service.cancel_order(order_id, chat_id)
            if not result:
                self.answer(callback, "Заказ уже отменили или закрыли", alert=True)
                return
            fine = result.get("fine")
            if fine:
                self.answer(callback, "Заказ отменён. Начислен штраф", alert=True)
                self.send(chat_id, fine_text(result["order"], fine["amount"]))
                self.send_fine_screen(chat_id, fine)
            else:
                self.answer(callback, "Заказ отменён")
            return

        if data == "p:receipt":
            fine = self.store.active_fine(chat_id)
            if not fine:
                self.answer(callback, "Штрафов нет — всё в порядке")
                return
            self.answer(callback)
            self.send(
                chat_id,
                "📎 <b>Пришлите PDF-чек</b>\n\n"
                "Файлом, как он скачался из банка. Фото и скриншоты не подойдут.",
            )
            return

        if data == "p:status":
            self.answer(callback)
            self.show_status(chat_id, message_id)
            return

        self.answer(callback)

    # --------------------------------------------------------------- passenger
    def send_fine_screen(self, chat_id, fine):
        self.send(chat_id, fine_requisites_text(fine["amount"]))
        self.send(
            chat_id,
            fine_how_to_text(fine["amount"]),
            inline_keyboard([[button("📎 Отправить чек", "p:receipt")]]),
        )

    def blocked_message(self, chat_id, fine, msg):
        """A blocked passenger can only send a PDF receipt."""
        doc = msg.get("document") or {}
        if doc:
            self.accept_receipt(chat_id, fine, doc)
            return
        self.send_fine_screen(chat_id, fine)

    def accept_receipt(self, chat_id, fine, doc):
        mime = (doc.get("mime_type") or "").lower()
        name = (doc.get("file_name") or "").lower()
        if mime != "application/pdf" and not name.endswith(".pdf"):
            self.send(chat_id, fine_receipt_wrong_type_text())
            return
        if not doc.get("file_id"):
            self.send(chat_id, "Файл не открылся, пришлите чек ещё раз.")
            return

        passenger = self.store.get_passenger(chat_id) or {"chat_id": chat_id}
        updated = self.store.attach_receipt(
            fine["id"], doc["file_id"], doc.get("file_name")
        )
        self.service.receive_receipt(updated, passenger, doc["file_id"])
        self.send(chat_id, fine_receipt_ok_text())
        log.info("fine #%s closed with receipt from %s", fine["id"], chat_id)

    def start_order(self, chat_id):
        active = self.store.active_order_for_passenger(chat_id)
        if active:
            self.send_main(chat_id)
            self.send(
                chat_id,
                "Сначала завершите текущий заказ "
                f"<b>{public_id(active)}</b>.",
            )
            return
        self.store.set_session(self.name, chat_id, "from", {})
        self.send(
            chat_id,
            "📍 <b>Откуда поедете?</b>\n\nНапишите адрес, откуда вас забрать.",
            force_reply("Откуда"),
        )

    def send_main(self, chat_id, edit_message_id=None):
        active = self.store.active_order_for_passenger(chat_id)
        if active:
            status = (
                "ищем машину"
                if active["status"] == "searching"
                else "водитель уже едет"
            )
            text = (
                "🚕 <b>Ваш заказ</b>\n\n"
                f"Заказ <b>{public_id(active)}</b>\n"
                f"📍 {esc(active['from_addr'])}\n"
                f"🏁 {esc(active['to_addr'])}\n"
                f"Сейчас: <b>{status}</b>"
            )
            rows = [[button("📋 Посмотреть заказ", "p:status")]]
        else:
            text = (
                "🚕 <b>TAXIDRIVE24/7</b>\n\n"
                "Бот для заказа такси.\n\n"
                "Нажмите «Заказать такси» — подскажу, что написать. "
                "Когда водитель примет заказ, пришлю марку, цвет и номер машины."
            )
            rows = [[button("🚕 Заказать такси", "p:new")]]
        if self.store.is_admin(chat_id):
            rows.append([button("⚙️ Админка", "a:panel")])
        keyboard = inline_keyboard(rows)

        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    def send_preview(self, chat_id, data):
        text = (
            "🚕 <b>Проверьте заказ</b>\n\n"
            f"📍 <b>Откуда:</b> {esc(data.get('from'))}\n"
            f"🏁 <b>Куда:</b> {esc(data.get('to'))}\n"
            f"💰 <b>Стоимость:</b> от {self.service.base_fare} ₽\n"
            f"📏 <b>Тариф:</b> {esc(self.service.tariff_note())}\n\n"
            "Всё верно? Отправляем водителям?"
        )
        keyboard = inline_keyboard(
            [
                [button("✅ Отправить водителям", "p:send")],
                [button("✏️ Изменить", "p:edit"), button("❌ Отмена", "p:drop")],
            ]
        )
        self.send(chat_id, text, keyboard)

    def finish_preview(self, chat_id, callback, message_id):
        session = self.store.get_session(self.name, chat_id)
        data = (session or {}).get("data") or {}
        if not session or session.get("state") != "preview" or not data.get("from"):
            self.answer(callback, "Заказ уже отправлен", alert=True)
            return

        active = self.store.active_order_for_passenger(chat_id)
        if active:
            self.answer(callback, "У вас уже есть заказ", alert=True)
            return

        passenger = self.store.get_passenger(chat_id) or {"chat_id": chat_id}
        order = self.service.create_order(
            passenger,
            data["from"],
            data["to"],
            passenger_message_id=message_id,
        )
        self.store.clear_session(self.name, chat_id)
        self.answer(callback, "Заказ отправлен водителям")

        text = passenger_search_text(order)
        keyboard = passenger_search_keyboard(order)
        if message_id:
            ok = self.edit(chat_id, message_id, text, keyboard)
            if not ok:
                sent = self.send(chat_id, text, keyboard)
                if sent:
                    self.store.set_passenger_message(order["id"], sent["message_id"])
        else:
            sent = self.send(chat_id, text, keyboard)
            if sent:
                self.store.set_passenger_message(order["id"], sent["message_id"])

        threading.Thread(
            target=self._broadcast_safely,
            args=(order["id"],),
            name=f"broadcast-{order['id']}",
            daemon=True,
        ).start()

    def _broadcast_safely(self, order_id):
        try:
            self.service.broadcast(order_id)
        except Exception:
            log.exception("broadcast order %s failed", order_id)

    def show_status(self, chat_id, edit_message_id=None):
        order = self.store.active_order_for_passenger(chat_id)
        if not order:
            if edit_message_id:
                self.edit(
                    chat_id,
                    edit_message_id,
                    "У вас пока нет заказов.",
                    inline_keyboard([[button("🚕 Заказать такси", "p:new")]]),
                )
            else:
                self.send(chat_id, "У вас пока нет заказов.")
            return

        if order["status"] == "accepted":
            full = self.store.get_order_with_driver(order["id"])
            text = passenger_accepted_text(full)
            keyboard = passenger_driver_keyboard(full)
        else:
            text = passenger_search_text(order)
            keyboard = passenger_search_keyboard(order)

        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    # ---------------------------------------------------------------- commands
    def cmd_start(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.store.clear_session(self.name, chat_id)
        self.send_main(chat_id)

    cmd_help = cmd_start

    def cmd_status(self, msg, args):
        self.show_status(msg["chat"]["id"])

    def cmd_cancel(self, msg, args):
        chat_id = msg["chat"]["id"]
        order = self.store.active_order_for_passenger(chat_id)
        session = self.store.get_session(self.name, chat_id)
        if session and not order:
            self.store.clear_session(self.name, chat_id)
            self.send(chat_id, "Хорошо, отменил.")
            return

        if not order:
            self.send(chat_id, "У вас пока нет заказов.")
            return

        self.store.clear_session(self.name, chat_id)

        if order["status"] == "accepted":
            self.send(
                chat_id,
                confirm_cancel_text(order, self.service.fine_amount),
                confirm_cancel_keyboard(order),
            )
            return

        if self.service.cancel_order(order["id"], chat_id):
            self.send(chat_id, "Заказ отменён.")
        else:
            self.send(chat_id, "Не получилось отменить заказ. Попробуйте ещё раз.")


# ================================================================== driver bot
class DriverBot(BotBase):
    name = "driver"
    commands = [
        ("start", "Главное меню"),
        ("online", "Начать работу"),
        ("offline", "Закончить работу"),
        ("profile", "Мой профиль"),
        ("cancel", "Отменить"),
        ("admin", "Админка"),
    ]

    def __init__(
        self, api, store, service, stop_event, invite_code="", admin_code=""
    ):
        super().__init__(api, store, service, stop_event, admin_code=admin_code)
        self.invite_code = invite_code or ""

    def handle_message(self, msg):
        if not is_private_message(msg):
            return
        chat_id = msg["chat"]["id"]
        text = msg.get("text")
        if self.maybe_command(msg, text or ""):
            return

        session = self.store.get_session(self.name, chat_id)
        state = session["state"] if session else None
        if state == "admin_code":
            self.admin_code_step(chat_id, text, msg.get("from") or {})
            return
        if state and (state == "code" or state.startswith("reg_")):
            editing = bool((session.get("data") or {}).get("edit"))
            if not editing and self.store.get_driver(chat_id):
                # A registered driver must never be pushed through registration again.
                log.warning(
                    "registered driver %s hit registration state %s -> main menu",
                    chat_id,
                    state,
                )
                self.store.clear_session(self.name, chat_id)
                self.send(chat_id, "Вы уже зарегистрированы — открываю главное меню.")
                self.send_main(chat_id)
                return
            self.registration_step(chat_id, msg, session, msg.get("from") or {})
            return

        if msg.get("contact"):
            self.send(chat_id, "Телефон здесь не нужен.")
            return
        self.send_main(chat_id)

    def handle_callback(self, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id = chat["id"]
        user = callback.get("from") or {}
        if user.get("id") != chat_id:
            self.answer(callback, "Это чужой профиль.", alert=True)
            return

        data = callback.get("data") or ""
        parts = data.split(":")
        message_id = message.get("message_id")

        if data.startswith("a:"):
            self.handle_admin_callback(callback, data, message_id)
            return

        if data == "d:reg":
            self.answer(callback)
            if self.store.get_driver(chat_id):
                self.send_main(chat_id, message_id)
                return
            session = self.store.get_session(self.name, chat_id)
            state = session["state"] if session else None
            if state and (state == "code" or state.startswith("reg_")):
                # Do not reset progress when the old button is tapped again.
                self.send(
                    chat_id,
                    "Регистрация уже начата — продолжите с последнего вопроса. "
                    "Если хотите начать заново, отправьте /start.",
                )
                return
            if message_id:
                self.edit(
                    chat_id,
                    message_id,
                    "📝 <b>Регистрация</b>\n\nЗаполним профиль один раз.",
                    EMPTY_KEYBOARD,
                )
            self.begin_registration(chat_id)
            return

        if data in {"d:on", "d:off"}:
            self.toggle_status(chat_id, data == "d:on", callback, message_id)
            return

        if data == "d:profile":
            self.answer(callback)
            self.show_profile(chat_id, message_id)
            return

        if data == "d:edit":
            self.answer(callback)
            self.begin_edit(chat_id)
            return

        if data == "d:current":
            self.answer(callback)
            self.show_current_order(chat_id, message_id)
            return

        if parts[:1] == ["d"] and len(parts) == 3 and parts[1] == "accept":
            try:
                order_id = int(parts[2])
            except ValueError:
                self.answer(callback)
                return
            self.accept_order(chat_id, order_id, callback, message_id)
            return

        if parts[:1] == ["d"] and len(parts) == 3 and parts[1] == "done":
            try:
                order_id = int(parts[2])
            except ValueError:
                self.answer(callback)
                return
            self.finish_order(chat_id, order_id, callback)
            return

        self.answer(callback)

    # -------------------------------------------------------------- registration
    def begin_registration(self, chat_id):
        log.info("driver registration started: chat_id=%s", chat_id)
        if self.invite_code:
            self.store.set_session(self.name, chat_id, "code", {})
            self.send(
                chat_id,
                "🔐 <b>TAXIDRIVE24/7</b>\n\n"
                "Введите код, который вам дали для регистрации.",
                force_reply("Код"),
            )
            return
        self.ask_name(chat_id, {})

    def begin_edit(self, chat_id):
        driver = self.store.get_driver(chat_id)
        if not driver:
            self.begin_registration(chat_id)
            return
        data = {
            "edit": True,
            "name": driver["name"],
            "car": driver["car"],
            "color": driver["color"],
            "plate": driver["plate"],
            "phone": driver.get("phone") or "",
        }
        self.store.set_session(self.name, chat_id, "reg_name", data)
        self.send(
            chat_id,
            "✏️ <b>Изменение профиля</b>\n\n"
            f"Сейчас имя: <b>{esc(driver['name'])}</b>.\n"
            "Напишите новое имя или отправьте «-», чтобы оставить как есть.",
            force_reply("Имя"),
        )

    def ask_name(self, chat_id, data):
        self.store.set_session(self.name, chat_id, "reg_name", data)
        self.send(
            chat_id,
            "📝 <b>Давайте познакомимся</b>\n\nКак вас зовут?",
            force_reply("Имя"),
        )

    def registration_step(self, chat_id, msg, session, user):
        state = session.get("state")
        data = dict(session.get("data") or {})
        text = msg.get("text") or ""
        log.info("driver registration step=%s chat_id=%s", state, chat_id)
        edit = bool(data.get("edit"))
        keep_hint = (
            "\n\nОтправьте «-», чтобы оставить текущее значение." if edit else ""
        )

        if state == "code":
            given = " ".join((text or "").split()).casefold()
            expected = self.invite_code.strip().casefold()
            if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
                log.info(
                    "driver invite code rejected: chat_id=%s", chat_id
                )
                self.send(chat_id, "Код не подошёл. Попробуйте ещё раз.")
                return
            data["code_ok"] = True
            self.ask_name(chat_id, data)
            return

        if state == "reg_name":
            name = (
                data.get("name")
                if edit and is_keep(text)
                else clean_text(text, 2, 60)
            )
            if not name:
                self.send(chat_id, "Пожалуйста, напишите имя.")
                return
            data["name"] = name
            self.store.set_session(self.name, chat_id, "reg_car", data)
            self.send(
                chat_id,
                "🚗 <b>На какой машине работаете?</b>\n\n"
                "Напишите марку и модель, например: Kia Rio."
                + keep_hint,
                force_reply("Марка и модель"),
            )
            return

        if state == "reg_car":
            car = (
                data.get("car")
                if edit and is_keep(text)
                else clean_text(text, 2, 60)
            )
            if not car:
                self.send(chat_id, "Пожалуйста, напишите марку и модель машины.")
                return
            data["car"] = car
            self.store.set_session(self.name, chat_id, "reg_color", data)
            self.send(
                chat_id,
                "🎨 <b>Какого цвета машина?</b>\n\nНапример: белая." + keep_hint,
                force_reply("Цвет"),
            )
            return

        if state == "reg_color":
            color = (
                data.get("color")
                if edit and is_keep(text)
                else clean_text(text, 2, 30)
            )
            if not color:
                self.send(chat_id, "Пожалуйста, напишите цвет машины.")
                return
            data["color"] = color
            self.store.set_session(self.name, chat_id, "reg_plate", data)
            self.send(
                chat_id,
                "🔢 <b>Какой номер у машины?</b>\n\nНапример: А123ВС77."
                + keep_hint,
                force_reply("Номер машины"),
            )
            return

        if state == "reg_plate":
            if edit and is_keep(text):
                plate = data.get("plate") or ""
            else:
                plate = " ".join(text.upper().split())
            if not 2 <= len(plate) <= 15:
                self.send(chat_id, "Пожалуйста, напишите номер машины.")
                return
            data["plate"] = plate
            self.store.set_session(self.name, chat_id, "reg_phone", data)
            phone_note = "Оставьте номер, чтобы пассажир мог связаться с вами. "
            if not edit:
                phone_note += "Можно пропустить."
            self.send(chat_id, "📞 <b>Телефон</b>\n\n" + phone_note, CONTACT_KEYBOARD)
            return

        if state == "reg_phone":
            contact = msg.get("contact") or {}
            if contact:
                if contact.get("user_id") not in (None, chat_id):
                    self.send(chat_id, "Пожалуйста, отправьте свой номер.")
                    return
                phone = normalize_phone(contact.get("phone_number"))
            elif edit and is_keep(text):
                phone = data.get("phone") or ""
            else:
                phone = normalize_phone(text)
            if phone is None:
                self.send(
                    chat_id,
                    "Кажется, в номере ошибка. Напишите ещё раз "
                    "или нажмите «Пропустить».",
                    CONTACT_KEYBOARD,
                )
                return
            data["phone"] = phone
            self.finish_registration(chat_id, data, user)
            return

        self.store.clear_session(self.name, chat_id)
        self.send_main(chat_id)

    def finish_registration(self, chat_id, data, user):
        editing = bool(data.get("edit"))
        try:
            self.store.create_driver(
                chat_id=chat_id,
                name=data["name"],
                car=data["car"],
                color=data["color"],
                plate=data["plate"],
                phone=data.get("phone") or None,
                username=user.get("username"),
            )
        except Exception:
            log.exception("saving driver %s failed", chat_id)
            self.send(
                chat_id,
                "Не получилось сохранить профиль. Попробуйте ещё раз через минуту.",
            )
            return
        self.store.clear_session(self.name, chat_id)
        log.info(
            "driver %s: chat_id=%s username=%s",
            "profile updated" if editing else "registered",
            chat_id,
            user.get("username"),
        )

        if editing:
            self.send(
                chat_id,
                "✅ <b>Профиль обновлён</b>",
                REMOVE_KEYBOARD,
            )
            self.show_profile(chat_id)
            return

        self.send(
            chat_id,
            "✅ <b>Готово!</b>\n\n"
            "Теперь вы можете принимать заказы. Они будут приходить сюда.",
            REMOVE_KEYBOARD,
        )
        self.send_main(chat_id)
        self._offer_waiting(chat_id)

    # -------------------------------------------------------------- driver menus
    def send_main(self, chat_id, edit_message_id=None):
        driver = self.store.get_driver(chat_id)
        if not driver:
            text = (
                "🚖 <b>TAXIDRIVE24/7</b>\n\n"
                "Здесь водители получают заказы. Смотрите адреса и цену, "
                "выбирайте подходящий заказ и нажимайте «Взять заказ».\n\n"
                "Чтобы начать, заполните профиль."
            )
            rows = [[button("📝 Зарегистрироваться", "d:reg")]]
            if self.store.is_admin(chat_id):
                rows.append([button("⚙️ Админка", "a:panel")])
            keyboard = inline_keyboard(rows)
            if edit_message_id:
                self.edit(chat_id, edit_message_id, text, keyboard)
            else:
                self.send(chat_id, text, keyboard)
            return

        active = self.store.active_order_for_driver(chat_id)
        status_line = (
            "🟢 <b>Приём заказов включён</b> — новые заказы приходят сюда."
            if driver["status"] == "active"
            else "🔴 <b>Приём заказов выключен</b> — сейчас заказы не приходят."
        )
        lines = [
            "🚕 <b>TAXIDRIVE24/7</b>",
            "",
            f"👤 {esc(driver['name'])}",
            f"🚗 {esc(driver['car'])}, {esc(driver['color'])}",
            f"🔢 {esc(driver['plate'])}",
        ]
        if driver.get("phone"):
            lines.append(f"📞 {esc(driver['phone'])}")
        lines += ["", status_line]
        if active:
            lines += ["", f"🔔 У вас заказ: <b>{public_id(active)}</b>"]

        rows = []
        if active:
            rows.append([button("📋 Мой заказ", "d:current")])
        if driver["status"] == "active":
            rows.append([button("🔴 Закончить работу", "d:off")])
        else:
            rows.append([button("🟢 Начать работу", "d:on")])
        rows.append([button("👤 Профиль", "d:profile")])
        if self.store.is_admin(chat_id):
            rows.append([button("⚙️ Админка", "a:panel")])
        keyboard = inline_keyboard(rows)

        if edit_message_id:
            self.edit(chat_id, edit_message_id, "\n".join(lines), keyboard)
        else:
            self.send(chat_id, "\n".join(lines), keyboard)

    def show_profile(self, chat_id, edit_message_id=None):
        driver = self.store.get_driver(chat_id)
        if not driver:
            self.send_main(chat_id, edit_message_id)
            return
        lines = [
            "👤 <b>Мой профиль</b>",
            "",
            f"Имя: <b>{esc(driver['name'])}</b>",
            f"Машина: <b>{esc(driver['car'])}, {esc(driver['color'])}</b>",
            f"Номер машины: <b>{esc(driver['plate'])}</b>",
        ]
        if driver.get("phone"):
            lines.append(f"Телефон: <b>{esc(driver['phone'])}</b>")
        lines.append(
            f"Приём заказов: <b>{'включён' if driver['status'] == 'active' else 'выключен'}</b>"
        )
        keyboard = inline_keyboard(
            [
                [button("✏️ Изменить профиль", "d:edit")],
                [
                    button(
                        "🔴 Закончить работу" if driver["status"] == "active" else "🟢 Начать работу",
                        "d:off" if driver["status"] == "active" else "d:on",
                    )
                ],
                [button("⬅️ Назад", "d:reg")],
            ]
        )
        if edit_message_id:
            self.edit(chat_id, edit_message_id, "\n".join(lines), keyboard)
        else:
            self.send(chat_id, "\n".join(lines), keyboard)

    def show_current_order(self, chat_id, edit_message_id=None):
        order = self.store.active_order_for_driver(chat_id)
        if not order:
            self.send(chat_id, "Сейчас у вас нет заказа.")
            return
        full = self.store.get_order_with_driver(order["id"])
        text = driver_accepted_text(full)
        keyboard = driver_done_keyboard(full)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    def toggle_status(self, chat_id, online, callback=None, edit_message_id=None):
        driver = self.store.get_driver(chat_id)
        if not driver:
            if callback:
                self.answer(callback, "Сначала заполните профиль", alert=True)
            else:
                self.send_main(chat_id)
            return
        if not online and self.store.active_order_for_driver(chat_id):
            if callback:
                self.answer(callback, "Сначала завершите текущий заказ", alert=True)
            else:
                self.send(chat_id, "Сначала завершите текущий заказ.")
            return
        self.store.set_driver_status(chat_id, "active" if online else "offline")
        if callback:
            self.answer(
                callback,
                "Приём заказов включён" if online else "Приём заказов выключен",
            )
        self.send_main(chat_id, edit_message_id)
        if online:
            self._offer_waiting(chat_id)

    def _offer_waiting(self, chat_id):
        driver = self.store.get_driver(chat_id)
        if not driver:
            return
        threading.Thread(
            target=self._offer_waiting_safely,
            args=(driver,),
            name=f"waiting-{chat_id}",
            daemon=True,
        ).start()

    def _offer_waiting_safely(self, driver):
        try:
            self.service.offer_waiting_orders(driver)
        except Exception:
            log.exception("waiting orders for %s failed", driver.get("chat_id"))

    # ------------------------------------------------------------------- orders
    def accept_order(self, chat_id, order_id, callback, message_id):
        driver = self.store.get_driver(chat_id)
        if not driver:
            self.answer(callback, "Сначала заполните профиль", alert=True)
            return
        if driver["status"] != "active":
            self.answer(callback, "Сейчас вы не принимаете заказы", alert=True)
            return
        if self.store.active_order_for_driver(chat_id):
            self.answer(callback, "Сначала завершите текущий заказ", alert=True)
            return

        order = self.service.accept_order(order_id, driver)
        if not order:
            self.answer(callback, "Этот заказ уже взяли", alert=True)
            if message_id:
                self.edit(
                    chat_id,
                    message_id,
                    "❌ <b>Этот заказ уже взяли</b>",
                    EMPTY_KEYBOARD,
                )
            return

        self.answer(callback, "✅ Заказ ваш")
        threading.Thread(
            target=self._cleanup_safely,
            args=(order_id, chat_id),
            name=f"cleanup-{order_id}",
            daemon=True,
        ).start()

    def _cleanup_safely(self, order_id, driver_id):
        try:
            self.service.cleanup_after_accept(order_id, driver_id)
        except Exception:
            log.exception("cleanup order %s failed", order_id)

    def finish_order(self, chat_id, order_id, callback):
        if not self.service.complete_order(order_id, chat_id):
            self.answer(callback, "Этот заказ уже завершён", alert=True)
            return
        self.answer(callback, "🏁 Заказ завершён")
        self.send_main(chat_id)

    # ---------------------------------------------------------------- commands
    def cmd_start(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.store.clear_session(self.name, chat_id)
        self.send_main(chat_id)

    def cmd_help(self, msg, args):
        self.send_main(msg["chat"]["id"])

    def cmd_online(self, msg, args):
        self.toggle_status(msg["chat"]["id"], True)

    def cmd_offline(self, msg, args):
        self.toggle_status(msg["chat"]["id"], False)

    def cmd_profile(self, msg, args):
        self.show_profile(msg["chat"]["id"])

    def cmd_cancel(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.store.clear_session(self.name, chat_id)
        self.send(chat_id, "Хорошо, отменил.")
        self.send_main(chat_id)


# ======================================================================== main
def main():
    load_env_file(os.path.join(BASE_DIR, ".env"))

    parser = argparse.ArgumentParser(description="TAXIDRIVE24/7 Telegram bots")
    parser.add_argument(
        "--passenger-token",
        default=os.environ.get("PASSENGER_BOT_TOKEN"),
    )
    parser.add_argument(
        "--driver-token",
        default=os.environ.get("DRIVER_BOT_TOKEN"),
    )
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data")))
    parser.add_argument("--api-base", default=os.environ.get("API_BASE", "https://api.telegram.org"))
    parser.add_argument(
        "--base-fare",
        "--fare",
        dest="base_fare",
        type=int,
        default=int(os.environ.get("BASE_FARE", "300")),
    )
    parser.add_argument(
        "--included-km",
        type=float,
        default=float(os.environ.get("INCLUDED_KM", "2")),
    )
    parser.add_argument(
        "--price-per-km",
        type=int,
        default=int(os.environ.get("PRICE_PER_KM", "50")),
    )
    parser.add_argument(
        "--fine-amount",
        type=int,
        default=int(os.environ.get("FINE_AMOUNT", "150")),
        help="Fine for cancelling after the driver accepted",
    )
    parser.add_argument(
        "--owner-id",
        type=int,
        default=int(os.environ.get("OWNER_ID", "0") or 0),
        help="Telegram ID that receives fine receipts",
    )
    args = parser.parse_args()
    if not os.path.isabs(args.data_dir):
        args.data_dir = os.path.join(BASE_DIR, args.data_dir)

    if not args.passenger_token or not args.driver_token:
        parser.error(
            "PASSENGER_BOT_TOKEN and DRIVER_BOT_TOKEN are required "
            "(set them in .env or pass --passenger-token/--driver-token)"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Keep a log file next to the code: the panel console does not keep history,
    # so this is the only way to check later why a driver was asked to register.
    try:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            os.path.join(BASE_DIR, "bot.log"),
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:
        log.warning("could not open bot.log: %s", exc)

    # A stale flag from a previous forced kill must not stop the watchdog.
    try:
        os.remove(os.path.join(BASE_DIR, ".stop-requested"))
    except OSError:
        pass
    # A stale reload flag must not restart the process in a loop.
    try:
        os.remove(os.path.join(BASE_DIR, ".reload-requested"))
    except OSError:
        pass

    tz_name = os.environ.get("TZ")
    if tz_name:
        os.environ["TZ"] = tz_name
        if hasattr(time, "tzset"):
            try:
                time.tzset()
            except Exception:
                log.warning("could not apply TZ=%s", tz_name)

    os.makedirs(args.data_dir, exist_ok=True)
    db_path = os.path.join(args.data_dir, "taxi.db")
    store = Store(db_path)
    passenger_api = BotAPI(args.passenger_token, args.api_base)
    driver_api = BotAPI(args.driver_token, args.api_base)
    service = TaxiService(
        store,
        passenger_api,
        driver_api,
        base_fare=args.base_fare,
        included_km=args.included_km,
        price_per_km=args.price_per_km,
        fine_amount=args.fine_amount,
        owner_id=args.owner_id,
    )

    stop = threading.Event()
    invite_code = os.environ.get("DRIVER_INVITE_CODE", "")
    admin_code = os.environ.get("ADMIN_INVITE_CODE", "")

    passenger_bot = PassengerBot(
        passenger_api, store, service, stop, admin_code=admin_code
    )
    driver_bot = DriverBot(
        driver_api,
        store,
        service,
        stop,
        invite_code=invite_code,
        admin_code=admin_code,
    )

    def run_bot(bot):
        try:
            bot.run()
        except Exception:
            log.exception("%s crashed", bot.name)
            stop.set()

    threads = [
        threading.Thread(target=run_bot, args=(passenger_bot,), name="passenger", daemon=True),
        threading.Thread(target=run_bot, args=(driver_bot,), name="driver", daemon=True),
    ]

    def shutdown(*_):
        log.info("stopping…")
        # Tell the watchdog this is a deliberate stop, not a crash.
        try:
            with open(os.path.join(BASE_DIR, ".stop-requested"), "w"):
                pass
        except OSError:
            pass
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    search_timeout = int(float(os.environ.get("SEARCH_TIMEOUT_MINUTES", "15")) * 60)

    def maintenance():
        while not stop.wait(60):
            try:
                service.reoffer_waiting_orders()
                expired = service.expire_stale_orders(search_timeout)
                if expired:
                    log.info("expired %s stale orders", expired)
            except Exception:
                log.exception("maintenance failed")

    def reload_watcher():
        """Hot deploy: re-exec the new code without stopping the container."""
        flag = os.path.join(BASE_DIR, ".reload-requested")
        while not stop.wait(1):
            if not os.path.exists(flag):
                continue
            try:
                os.remove(flag)
            except OSError:
                pass
            log.info("reload requested, restarting process with the new code")
            stop.set()
            time.sleep(0.5)
            try:
                os.execv(
                    sys.executable,
                    [sys.executable, os.path.join(BASE_DIR, "main.py")],
                )
            except Exception:
                log.exception("hot reload failed, staying on the old code")
                return

    log.info(
        "starting: db=%s, base_fare=%s ₽, included_km=%s, price_per_km=%s ₽, "
        "drivers=%s, invite_code=%s, admin_code=%s, search_timeout=%s",
        db_path,
        args.base_fare,
        args.included_km,
        args.price_per_km,
        store.driver_count(),
        "on" if invite_code else "off",
        "on" if admin_code else "off",
        f"{search_timeout // 60} min" if search_timeout > 0 else "off",
    )
    threading.Thread(target=maintenance, name="maintenance", daemon=True).start()
    threading.Thread(target=reload_watcher, name="reload", daemon=True).start()
    for thread in threads:
        thread.start()
    while any(t.is_alive() for t in threads):
        time.sleep(0.5)
    for thread in threads:
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
