import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot import DriverBot, PassengerBot  # noqa: E402
from core import TaxiService  # noqa: E402
from store import Store  # noqa: E402


class FakeAPI:
    def __init__(self):
        self.messages = {}
        self.deleted = set()
        self._next_id = 1
        self._lock = threading.Lock()

    def send_message(self, chat_id, text, **kwargs):
        with self._lock:
            message_id = self._next_id
            self._next_id += 1
            self.messages[message_id] = {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": kwargs.get("reply_markup"),
            }
        return {"message_id": message_id}

    def edit_message_text(self, chat_id, message_id, text, **kwargs):
        with self._lock:
            message = self.messages.setdefault(
                message_id, {"chat_id": chat_id, "text": None, "reply_markup": None}
            )
            assert message["chat_id"] == chat_id
            message["text"] = text
            message["reply_markup"] = kwargs.get("reply_markup")
        return True

    def delete_message(self, chat_id, message_id):
        with self._lock:
            self.deleted.add(message_id)
        return True

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        return True

    def set_my_commands(self, commands):
        return True


def callback(user_id, message_id, data):
    return {
        "id": f"cb-{message_id}",
        "from": {"id": user_id, "first_name": "Тест"},
        "message": {
            "message_id": message_id,
            "chat": {"id": user_id, "type": "private"},
        },
        "data": data,
    }


def message(user_id, text=None, contact=None):
    payload = {
        "message_id": 1,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "first_name": "Тест", "username": "test_user"},
    }
    if text is not None:
        payload["text"] = text
    if contact is not None:
        payload["contact"] = contact
    return payload


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "taxi.db"))
        self.passenger_api = FakeAPI()
        self.driver_api = FakeAPI()
        self.service = TaxiService(
            self.store,
            self.passenger_api,
            self.driver_api,
            base_fare=250,
            included_km=2,
            price_per_km=50,
        )
        self.stop = threading.Event()
        self.passenger_bot = PassengerBot(
            self.passenger_api, self.store, self.service, self.stop
        )
        self.driver_bot = DriverBot(
            self.driver_api, self.store, self.service, self.stop
        )

    def tearDown(self):
        self.stop.set()
        time.sleep(0.2)
        self.store.db.close()
        self.tmp.cleanup()

    def test_driver_registration(self):
        self.driver_bot.handle_callback(callback(200, 1, "d:reg"))
        self.driver_bot.handle_message(message(200, "Иван"))
        self.driver_bot.handle_message(message(200, "Kia Rio"))
        self.driver_bot.handle_message(message(200, "белый"))
        self.driver_bot.handle_message(message(200, "А123ВС77"))
        self.driver_bot.handle_message(
            message(200, contact={"phone_number": "+79990000001"})
        )

        driver = self.store.get_driver(200)
        self.assertIsNotNone(driver)
        self.assertEqual(driver["name"], "Иван")
        self.assertEqual(driver["car"], "Kia Rio")
        self.assertEqual(driver["color"], "белый")
        self.assertEqual(driver["plate"], "А123ВС77")
        self.assertEqual(driver["status"], "active")
        self.assertEqual(driver["username"], "test_user")

    def test_driver_registration_can_skip_phone(self):
        self.driver_bot.handle_callback(callback(201, 1, "d:reg"))
        for value in ("Пётр", "Lada Vesta", "чёрный", "В456ЕК99"):
            self.driver_bot.handle_message(message(201, value))
        self.driver_bot.handle_message(message(201, "⏭ Пропустить"))

        driver = self.store.get_driver(201)
        self.assertIsNotNone(driver)
        self.assertIsNone(driver["phone"])

    def test_driver_invite_code_ignores_case_but_not_typos(self):
        bot = DriverBot(
            self.driver_api,
            self.store,
            self.service,
            self.stop,
            invite_code="TAXiDrivEE",
        )
        bot.handle_callback(callback(202, 1, "d:reg"))
        bot.handle_message(message(202, "taxidriv"))
        self.assertIsNone(self.store.get_driver(202))
        self.assertEqual(self.store.get_session(bot.name, 202)["state"], "code")

        bot.handle_message(message(202, "taxidrivee"))
        self.assertIsNone(self.store.get_driver(202))
        self.assertEqual(self.store.get_session(bot.name, 202)["state"], "reg_name")

    def test_passenger_order_and_driver_accept(self):
        self.store.create_driver(
            200, "Иван", "Kia Rio", "белый", "А123ВС77", "+79990000001", "ivan"
        )

        self.passenger_bot.handle_callback(callback(100, 1, "p:new"))
        self.passenger_bot.handle_message(message(100, "ул. Ленина, 1"))
        self.passenger_bot.handle_message(message(100, "ТЦ Галерея"))

        preview_id = max(self.passenger_api.messages)
        preview = self.passenger_api.messages[preview_id]
        self.assertIn("Отправить водителям", str(preview["reply_markup"]))
        self.assertIn("Тариф", preview["text"])
        self.assertIn("250", preview["text"])
        self.assertNotIn("Расстояние", preview["text"])

        self.passenger_bot.handle_callback(callback(100, preview_id, "p:send"))
        time.sleep(0.25)

        order = self.store.active_order_for_passenger(100)
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "searching")
        self.assertEqual(order["fare"], 250)
        self.assertIn("2 км", order["tariff_note"])
        self.assertIn("50 ₽/км", order["tariff_note"])

        driver_messages = [
            (mid, m)
            for mid, m in self.driver_api.messages.items()
            if m["chat_id"] == 200
        ]
        self.assertEqual(len(driver_messages), 1)
        driver_card_id, driver_card = driver_messages[0]
        self.assertIn("Новый заказ", driver_card["text"])
        self.assertIn("Взять заказ", str(driver_card["reply_markup"]))

        self.driver_bot.handle_callback(
            callback(200, driver_card_id, f"d:accept:{order['id']}")
        )
        time.sleep(0.25)

        current = self.store.get_order(order["id"])
        self.assertEqual(current["status"], "accepted")
        self.assertEqual(current["driver_id"], 200)
        passenger_status = self.passenger_api.messages[current["passenger_message_id"]]["text"]
        self.assertIn("Машина найдена", passenger_status)
        self.assertIn("Kia Rio", passenger_status)
        self.assertIn("белый", passenger_status)

    def test_admin_login_in_both_bots(self):
        passenger_bot = PassengerBot(
            self.passenger_api,
            self.store,
            self.service,
            self.stop,
            admin_code="TAXI-ADMIN-TEST",
        )
        passenger_bot.handle_message(message(300, "/admin"))
        passenger_bot.handle_message(message(300, "wrong"))
        self.assertFalse(self.store.is_admin(300))
        passenger_bot.handle_message(message(300, "TAXI-ADMIN-TEST"))
        self.assertTrue(self.store.is_admin(300))

        driver_bot = DriverBot(
            self.driver_api,
            self.store,
            self.service,
            self.stop,
            invite_code="TAXiDrivEE",
            admin_code="TAXI-ADMIN-TEST",
        )
        driver_bot.handle_message(message(301, "/admin"))
        driver_bot.handle_message(message(301, "TAXI-ADMIN-TEST"))
        self.assertTrue(self.store.is_admin(301))

        texts = [m["text"] for m in self.passenger_api.messages.values()]
        self.assertTrue(any("Админка TAXIDRIVE24/7" in text for text in texts))
        self.assertTrue(any("Выручка по посадкам" in text for text in texts))

        panel_id = max(self.passenger_api.messages)
        passenger_bot.handle_callback(callback(300, panel_id, "a:drivers"))
        self.assertIn(
            "Водители на смене",
            self.passenger_api.messages[panel_id]["text"],
        )
        passenger_bot.handle_callback(callback(300, panel_id, "a:orders"))
        self.assertIn(
            "Последние заказы",
            self.passenger_api.messages[panel_id]["text"],
        )


if __name__ == "__main__":
    unittest.main()
