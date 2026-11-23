"""Order logic and message formatting shared by both bots."""

import html
import logging
import math
import time

from tgapi import TelegramError

log = logging.getLogger("taxi.core")

EMPTY_KEYBOARD = {"inline_keyboard": []}


def esc(value):
    return html.escape(str(value if value is not None else ""))


def public_id(order):
    return f"#{int(order['id']):04d}"


def fare_text(fare):
    return f"{int(fare)} ₽"


def km_text(value):
    if value is None:
        return ""
    number = float(value)
    if abs(number - round(number)) < 0.000001:
        return f"{int(round(number))} км"
    return f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " км"


def distance_line(order):
    if order.get("distance_km") is None:
        return ""
    return f"📏 <b>Расстояние:</b> {km_text(order['distance_km'])}\n"


def price_line(order):
    note = order.get("tariff_note")
    if note:
        return (
            f"💰 <b>Стоимость:</b> от {fare_text(order['fare'])}\n"
            f"📏 <b>Тариф:</b> {esc(note)}"
        )
    return f"💰 <b>Цена:</b> {fare_text(order['fare'])}"


def passenger_label(order):
    name = esc(order.get("passenger_name") or "Пассажир")
    username = order.get("passenger_username")
    return f"{name} (@{esc(username)})" if username else name


def driver_order_text(order):
    return (
        "🚕 <b>TAXIDRIVE24/7</b>\n"
        "<b>Новый заказ</b>\n\n"
        f"📍 <b>Откуда:</b> {esc(order['from_addr'])}\n"
        f"🏁 <b>Куда:</b> {esc(order['to_addr'])}\n"
        f"{distance_line(order)}"
        f"{price_line(order)}\n"
        f"🔢 <b>Номер заказа:</b> <code>{public_id(order)}</code>\n"
        f"👤 <b>Пассажир:</b> {passenger_label(order)}"
    )


def accept_keyboard(order):
    return {
        "inline_keyboard": [
            [{"text": "🚖 Взять заказ", "callback_data": f"d:accept:{order['id']}"}]
        ]
    }


def passenger_search_text(order):
    return (
        "🔎 <b>Ищем для вас машину…</b>\n\n"
        f"Заказ <b>{public_id(order)}</b>\n"
        f"📍 <b>Откуда:</b> {esc(order['from_addr'])}\n"
        f"🏁 <b>Куда:</b> {esc(order['to_addr'])}\n"
        f"{distance_line(order)}"
        f"{price_line(order)}\n\n"
        "Я передал заказ водителям. Как только водитель примет его, "
        "сразу пришлю данные машины."
    )


def passenger_search_keyboard(order):
    return {
        "inline_keyboard": [
            [{"text": "❌ Отменить заказ", "callback_data": f"p:cancel:{order['id']}"}]
        ]
    }


def passenger_accepted_text(order):
    lines = [
        "✅ <b>Машина найдена!</b>",
        "",
        f"Заказ <b>{public_id(order)}</b>",
        f"📍 <b>Откуда:</b> {esc(order['from_addr'])}",
        f"🏁 <b>Куда:</b> {esc(order['to_addr'])}",
        f"{distance_line(order)}"
        f"{price_line(order)}",
        f"🚖 <b>Водитель:</b> {esc(order.get('driver_name') or '—')}",
        f"🚗 <b>Машина:</b> {esc(order.get('driver_car') or '—')}, "
        f"{esc(order.get('driver_color') or '—')}",
        f"🔢 <b>Номер машины:</b> {esc(order.get('driver_plate') or '—')}",
    ]
    if order.get("driver_phone"):
        lines.append(f"📞 <b>Телефон:</b> {esc(order['driver_phone'])}")
    if order.get("driver_username"):
        lines.append(f"💬 <b>Telegram:</b> @{esc(order['driver_username'])}")
    lines += [
        "",
        "Водитель уже в пути. Если нужно что-то уточнить — напишите ему.",
    ]
    return "\n".join(lines)


def contact_url(username):
    """Public t.me link is more reliable than tg://user?id= for strangers."""
    username = (username or "").strip().lstrip("@")
    return f"https://t.me/{username}" if username else None


def passenger_driver_keyboard(order):
    if not order.get("driver_id"):
        return EMPTY_KEYBOARD
    rows = []
    url = contact_url(order.get("driver_username"))
    if url:
        rows.append([{"text": "💬 Написать водителю", "url": url}])
    rows.append(
        [{"text": "❌ Отменить заказ", "callback_data": f"p:cancel:{order['id']}"}]
    )
    return {"inline_keyboard": rows}


def driver_accepted_text(order):
    lines = [
        f"✅ <b>Вы взяли заказ {public_id(order)}</b>",
        "",
        f"📍 <b>Откуда:</b> {esc(order['from_addr'])}",
        f"🏁 <b>Куда:</b> {esc(order['to_addr'])}",
        f"{distance_line(order)}"
        f"{price_line(order)}",
        f"👤 <b>Пассажир:</b> {passenger_label(order)}",
        "",
        "Написать пассажиру можно кнопкой ниже.",
    ]
    return "\n".join(lines)


def driver_done_keyboard(order):
    rows = [
        [{"text": "🏁 Завершить заказ", "callback_data": f"d:done:{order['id']}"}]
    ]
    url = contact_url(order.get("passenger_username"))
    if url:
        rows.append([{"text": "💬 Написать пассажиру", "url": url}])
    return {"inline_keyboard": rows}


def passenger_finished_text(order):
    return (
        "🏁 <b>Поездка завершена</b>\n\n"
        f"Заказ {public_id(order)} закрыт.\n"
        "Спасибо, что были с TAXIDRIVE24/7!"
    )


def passenger_cancelled_text(order):
    return (
        "❌ <b>Заказ отменён</b>\n\n"
        "Если снова понадобится машина, я рядом."
    )


def confirm_cancel_keyboard(order):
    return {
        "inline_keyboard": [
            [{"text": "❌ Да, отменить", "callback_data": f"p:cancel2:{order['id']}"}],
            [{"text": "⬅️ Оставить заказ", "callback_data": "p:status"}],
        ]
    }


def confirm_cancel_text(order, fine_amount):
    return (
        "⚠️ <b>Водитель уже принял заказ и едет к вам</b>\n\n"
        f"Если отмените сейчас — штраф {fine_amount} ₽ и новые заказы "
        "будут недоступны до оплаты.\n\n"
        "Точно отменяем?"
    )


def fine_text(order, amount):
    return (
        f"⛔ <b>Заказ {public_id(order)} отменён после принятия</b>\n\n"
        "Водитель уже ехал к вам. Отмена на этом этапе — "
        f"штраф <b>{amount} ₽</b>.\n\n"
        "Пока штраф не оплачен, новые заказы недоступны."
    )


def fine_requisites_text(amount):
    return (
        "💳 <b>Оплата штрафа</b>\n\n"
        f"Сумма: <b>{amount} ₽</b>\n\n"
        "Получатель:\n"
        "Микитюк Валентин Юрьевич\n\n"
        "Счет:\n"
        "<code>40817810500154994453</code>\n\n"
        "Банк: АО \"ТБанк\"\n"
        "БИК: <code>044525974</code>"
    )


def fine_how_to_text(amount):
    return (
        "📎 <b>Остался один шаг</b>\n\n"
        f"Переведите {amount} ₽ по реквизитам и пришлите сюда "
        "<b>PDF-чек из банка</b> — обычным файлом, не фото.\n\n"
        "Как только чек придёт, доступ откроется автоматически."
    )


def fine_receipt_ok_text():
    return (
        "✅ <b>Чек получен — доступ открыт</b>\n\n"
        "Штраф закрыт, можно снова заказывать машину."
    )


def fine_receipt_wrong_type_text():
    return (
        "📄 <b>Нужен PDF-чек</b>\n\n"
        "Пришлите чек из банка именно файлом <b>PDF</b> — не фото "
        "и не скриншот."
    )


def fine_owner_card_text(fine, passenger):
    name = passenger.get("name") or str(fine["passenger_id"])
    username = passenger.get("username")
    lines = [
        "💸 <b>Чек по штрафу</b>",
        "",
        f"Штраф: <b>#{fine['id']}</b> · {fine['amount']} ₽",
        f"Пассажир: <b>{esc(name)}</b>"
        + (f" (@{esc(username)})" if username else ""),
        f"ID: <code>{fine['passenger_id']}</code>",
    ]
    if fine.get("order_id"):
        lines.append(f"Заказ: <b>#{int(fine['order_id']):04d}</b>")
    lines += ["", "Доступ пассажиру открыт автоматически."]
    return "\n".join(lines)


def driver_finished_text(order):
    return (
        f"🏁 <b>Заказ {public_id(order)} закрыт</b>\n\n"
        "Можно брать следующий."
    )


def driver_cancelled_text(order):
    return (
        f"❌ <b>Заказ {public_id(order)} отменён пассажиром</b>\n\n"
        "Вы снова свободны и можете брать другие заказы."
    )


def passenger_expired_text(order):
    return (
        "😔 <b>Машину найти не удалось</b>\n\n"
        f"Заказ <b>{public_id(order)}</b> снят: никто из водителей не принял его вовремя.\n"
        "Попробуйте отправить заказ ещё раз чуть позже."
    )


class TaxiService:
    def __init__(
        self,
        store,
        passenger_api,
        driver_api,
        base_fare=300,
        included_km=2,
        price_per_km=50,
        fine_amount=150,
        owner_id=0,
    ):
        self.store = store
        self.passenger_api = passenger_api
        self.driver_api = driver_api
        self.base_fare = max(0, int(base_fare))
        self.included_km = max(0.0, float(included_km))
        self.price_per_km = max(0, int(price_per_km))
        self.fine_amount = max(0, int(fine_amount))
        self.owner_id = int(owner_id or 0)

    def receive_receipt(self, fine, passenger, file_id):
        """Receipt unlocks the passenger right away; the owner gets a copy."""
        self.store.paid_fine(fine["id"])
        if not self.owner_id:
            log.warning("fine #%s: OWNER_ID is empty, receipt not forwarded", fine["id"])
            return False
        try:
            self.passenger_api.send_document(
                self.owner_id, file_id, caption=fine_owner_card_text(fine, passenger)
            )
            return True
        except TelegramError as exc:
            log.error("fine #%s receipt not sent to owner: %s", fine["id"], exc)
            return False

    def tariff_note(self):
        return (
            f"{self.base_fare} ₽ посадка, "
            f"{km_text(self.included_km)} включены, "
            f"далее {self.price_per_km} ₽/км"
        )

    def calculate_fare(self, distance_km):
        distance = max(0.0, float(distance_km or 0))
        extra = max(0.0, distance - self.included_km)
        extra_km = math.ceil(extra - 0.000001) if extra > 0 else 0
        return self.base_fare + extra_km * self.price_per_km

    def fare_details(self, distance_km):
        distance = max(0.0, float(distance_km or 0))
        total = self.calculate_fare(distance)
        extra = max(0.0, distance - self.included_km)
        extra_km = math.ceil(extra - 0.000001) if extra > 0 else 0
        return {
            "base_fare": self.base_fare,
            "included_km": self.included_km,
            "price_per_km": self.price_per_km,
            "distance_km": distance,
            "extra_km": extra_km,
            "total": total,
        }

    def fare_breakdown(self, distance_km):
        details = self.fare_details(distance_km)
        if details["extra_km"] <= 0:
            return (
                f"{details['base_fare']} ₽ — посадка, "
                f"первые {km_text(details['included_km'])} включены"
            )
        return (
            f"{details['base_fare']} ₽ посадка + "
            f"{details['extra_km']} км × {details['price_per_km']} ₽"
        )

    # ------------------------------------------------------------------ orders
    def create_order(
        self,
        passenger,
        from_addr,
        to_addr,
        distance_km=None,
        passenger_message_id=None,
    ):
        return self.store.create_order(
            passenger_id=passenger["chat_id"],
            passenger_name=passenger.get("name"),
            passenger_username=passenger.get("username"),
            from_addr=from_addr,
            to_addr=to_addr,
            fare=self.base_fare,
            distance_km=distance_km,
            tariff_note=self.tariff_note(),
            passenger_message_id=passenger_message_id,
        )

    def broadcast(self, order_id):
        """Send a new order to every online driver, newest state first."""
        order = self.store.get_order(order_id)
        if not order or order["status"] != "searching":
            return 0

        drivers = self.store.active_drivers()
        if not drivers:
            self._edit_or_send(
                self.passenger_api,
                order["passenger_id"],
                order.get("passenger_message_id"),
                passenger_search_text(order)
                + "\n\n⚠️ <b>Сейчас все машины заняты.</b> "
                "Я передам заказ, как только освободится водитель. "
                "Можно отменить заказ кнопкой ниже.",
                passenger_search_keyboard(order),
                order_id=order["id"],
            )
            return 0

        sent = 0
        for driver in drivers:
            current = self.store.get_order(order_id)
            if not current or current["status"] != "searching":
                break
            if self.store.get_driver_message(order_id, driver["chat_id"]):
                continue
            try:
                message = self.driver_api.send_message(
                    driver["chat_id"],
                    driver_order_text(current),
                    reply_markup=accept_keyboard(current),
                )
            except TelegramError as exc:
                log.warning("broadcast to %s failed: %s", driver["chat_id"], exc)
                continue

            self.store.add_driver_message(order_id, driver["chat_id"], message["message_id"])
            sent += 1

            # The order may have been accepted while this message was in flight.
            current = self.store.get_order(order_id)
            if not current or current["status"] != "searching":
                self._delete_or_edit_driver_card(
                    driver["chat_id"], message["message_id"], "taken"
                )
                self.store.remove_driver_message(order_id, driver["chat_id"])
                break
            time.sleep(0.05)

        current = self.store.get_order(order_id)
        if sent == 0 and current and current["status"] == "searching":
            self._edit_or_send(
                self.passenger_api,
                current["passenger_id"],
                current.get("passenger_message_id"),
                passenger_search_text(current)
                + "\n\n⚠️ <b>Что-то пошло не так.</b> "
                "Попробуйте отправить заказ ещё раз.",
                passenger_search_keyboard(current),
                order_id=current["id"],
            )
        return sent

    def offer_waiting_orders(self, driver):
        """Show orders that were created while this driver was offline."""
        if driver.get("status") != "active":
            return 0
        if self.store.active_order_for_driver(driver["chat_id"]):
            return 0
        sent = 0
        for order in self.store.searching_orders():
            if self.store.get_driver_message(order["id"], driver["chat_id"]):
                continue
            try:
                message = self.driver_api.send_message(
                    driver["chat_id"],
                    driver_order_text(order),
                    reply_markup=accept_keyboard(order),
                )
            except TelegramError as exc:
                log.warning("waiting order to %s failed: %s", driver["chat_id"], exc)
                continue
            self.store.add_driver_message(
                order["id"], driver["chat_id"], message["message_id"]
            )
            current = self.store.get_order(order["id"])
            if not current or current["status"] != "searching":
                self._delete_or_edit_driver_card(
                    driver["chat_id"], message["message_id"], "taken"
                )
                self.store.remove_driver_message(order["id"], driver["chat_id"])
                continue
            sent += 1
        return sent

    def accept_order(self, order_id, driver):
        """Accept an order and notify the passenger immediately."""
        order = self.store.get_order(order_id)
        if not order or order["status"] != "searching":
            return None
        if self.store.active_order_for_driver(driver["chat_id"]):
            return None

        accepted = self.store.accept_order(order_id, driver["chat_id"])
        if not accepted:
            return None

        full = self.store.get_order_with_driver(order_id)
        self._notify_passenger_accepted(full)
        self._notify_driver_accepted(full, driver)
        return full

    def cleanup_after_accept(self, order_id, driver_id):
        """Delete the order from every other driver's chat."""
        for record in self.store.driver_messages(order_id):
            if record["driver_id"] == driver_id:
                continue
            self._delete_or_edit_driver_card(
                record["driver_id"], record["message_id"], "taken"
            )
            self.store.remove_driver_message(order_id, record["driver_id"])

    def complete_order(self, order_id, driver_id):
        if not self.store.complete_order(order_id, driver_id):
            return False

        order = self.store.get_order_with_driver(order_id)
        self._edit_or_send(
            self.passenger_api,
            order["passenger_id"],
            order.get("passenger_message_id"),
            passenger_finished_text(order),
            EMPTY_KEYBOARD,
            order_id=order["id"],
        )
        record = self.store.get_driver_message(order_id, driver_id)
        if record:
            try:
                self.driver_api.edit_message_text(
                    driver_id,
                    record["message_id"],
                    driver_finished_text(order),
                    reply_markup=EMPTY_KEYBOARD,
                )
            except TelegramError as exc:
                log.warning("finish card edit failed: %s", exc)
        self.store.remove_driver_message(order_id, driver_id)
        return True

    def cancel_order(self, order_id, passenger_id):
        order = self.store.get_order(order_id)
        if not order or order["passenger_id"] != passenger_id:
            return False
        fined = order["status"] == "accepted"
        if not self.store.cancel_order(order_id, passenger_id):
            return False

        order = self.store.get_order_with_driver(order_id)
        self._edit_or_send(
            self.passenger_api,
            passenger_id,
            order.get("passenger_message_id"),
            passenger_cancelled_text(order),
            EMPTY_KEYBOARD,
            order_id=order["id"],
        )
        for record in self.store.driver_messages(order_id):
            if record["driver_id"] == order.get("driver_id"):
                try:
                    self.driver_api.edit_message_text(
                        record["driver_id"],
                        record["message_id"],
                        driver_cancelled_text(order),
                        reply_markup=EMPTY_KEYBOARD,
                    )
                except TelegramError as exc:
                    log.warning("driver cancel notice failed: %s", exc)
            else:
                self._delete_or_edit_driver_card(
                    record["driver_id"], record["message_id"], "cancelled"
                )
            self.store.remove_driver_message(order_id, record["driver_id"])

        fine = None
        if fined:
            fine = self.store.create_fine(passenger_id, order_id, self.fine_amount)
            log.info(
                "fine #%s: passenger %s cancelled accepted order #%s",
                fine["id"], passenger_id, order_id,
            )
        return {"fine": fine, "order": order}

    # ------------------------------------------------------------- maintenance
    def expire_stale_orders(self, timeout_seconds):
        """Close orders nobody accepted in time and tell everyone involved."""
        timeout_seconds = int(timeout_seconds or 0)
        if timeout_seconds <= 0:
            return 0
        cutoff = int(time.time()) - timeout_seconds
        expired = 0
        for order in self.store.searching_orders():
            if int(order["created_at"]) > cutoff:
                continue
            if not self.store.expire_searching_order(order["id"], cutoff):
                continue
            expired += 1
            self._edit_or_send(
                self.passenger_api,
                order["passenger_id"],
                order.get("passenger_message_id"),
                passenger_expired_text(order),
                EMPTY_KEYBOARD,
                order_id=order["id"],
            )
            for record in self.store.driver_messages(order["id"]):
                self._delete_or_edit_driver_card(
                    record["driver_id"], record["message_id"], "expired"
                )
                self.store.remove_driver_message(order["id"], record["driver_id"])
        return expired

    def reoffer_waiting_orders(self):
        """Remind free online drivers about orders they have not seen yet."""
        sent = 0
        for driver in self.store.active_drivers():
            try:
                sent += self.offer_waiting_orders(driver)
            except TelegramError as exc:
                log.warning("reoffer to %s failed: %s", driver["chat_id"], exc)
        return sent

    # ------------------------------------------------------------- passenger UX
    def _notify_passenger_accepted(self, order):
        self._edit_or_send(
            self.passenger_api,
            order["passenger_id"],
            order.get("passenger_message_id"),
            passenger_accepted_text(order),
            passenger_driver_keyboard(order),
            order_id=order["id"],
        )

    def _notify_driver_accepted(self, order, driver):
        record = self.store.get_driver_message(order["id"], driver["chat_id"])
        if record:
            try:
                self.driver_api.edit_message_text(
                    driver["chat_id"],
                    record["message_id"],
                    driver_accepted_text(order),
                    reply_markup=driver_done_keyboard(order),
                )
                return
            except TelegramError as exc:
                log.warning("accepted card edit failed: %s", exc)
        sent = self._safe_send(
            self.driver_api,
            driver["chat_id"],
            driver_accepted_text(order),
            driver_done_keyboard(order),
        )
        if sent:
            self.store.add_driver_message(
                order["id"], driver["chat_id"], sent["message_id"]
            )

    # ------------------------------------------------------------------ utils
    def _edit_or_send(self, api, chat_id, message_id, text, reply_markup, order_id=None):
        if message_id:
            try:
                api.edit_message_text(
                    chat_id, message_id, text, reply_markup=reply_markup
                )
                return message_id
            except TelegramError as exc:
                if "message is not modified" in str(exc).lower():
                    return message_id
                log.warning("edit %s/%s failed: %s", chat_id, message_id, exc)

        message = self._safe_send(api, chat_id, text, reply_markup)
        if message and order_id is not None:
            self.store.set_passenger_message(order_id, message["message_id"])
        return message["message_id"] if message else None

    def _safe_send(self, api, chat_id, text, reply_markup=None):
        try:
            return api.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramError as exc:
            log.warning("send to %s failed: %s", chat_id, exc)
            return None

    def _delete_or_edit_driver_card(self, chat_id, message_id, reason):
        try:
            self.driver_api.delete_message(chat_id, message_id)
            return
        except TelegramError:
            pass
        text = (
            "❌ <b>Этот заказ уже взят</b>"
            if reason == "taken"
            else "❌ <b>Пассажир отменил заказ</b>"
            if reason == "cancelled"
            else "⌛ <b>Заказ снят: машину не нашли</b>"
        )
        try:
            self.driver_api.edit_message_text(
                chat_id, message_id, text, reply_markup=EMPTY_KEYBOARD
            )
        except TelegramError as exc:
            log.debug("cleanup %s/%s failed: %s", chat_id, message_id, exc)
