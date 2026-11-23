import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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
        return {"message_id": message_id, "chat": {"id": chat_id}}

    def edit_message_text(self, chat_id, message_id, text, **kwargs):
        with self._lock:
            message = self.messages[message_id]
            assert message["chat_id"] == chat_id
            message["text"] = text
            message["reply_markup"] = kwargs.get("reply_markup")
        return True

    def delete_message(self, chat_id, message_id):
        with self._lock:
            self.deleted.add(message_id)
        return True

    def send_document(self, chat_id, document, caption=None, reply_markup=None):
        with self._lock:
            message_id = self._next_id
            self._next_id += 1
            self.messages[message_id] = {
                "chat_id": chat_id,
                "text": caption or "",
                "document": document,
            }
        return {"message_id": message_id}


class FlowTest(unittest.TestCase):
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
        self.store.ensure_passenger(100, "Анна", "anna")
        self.store.create_driver(
            200, "Иван", "Kia Rio", "белый", "А123ВС77", "+79990000001", "ivan"
        )
        self.store.create_driver(
            201, "Пётр", "Lada Vesta", "чёрный", "В456ЕК99", "+79990000002", None
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def make_order(self):
        preview = self.passenger_api.send_message(100, "preview")
        order = self.service.create_order(
            self.store.get_passenger(100),
            "ул. Ленина, 1",
            "ТЦ Галерея",
            passenger_message_id=preview["message_id"],
        )
        sent = self.service.broadcast(order["id"])
        return order, sent

    def test_distance_based_fare(self):
        self.assertEqual(self.service.calculate_fare(0), 250)
        self.assertEqual(self.service.calculate_fare(2), 250)
        self.assertEqual(self.service.calculate_fare(2.01), 300)
        self.assertEqual(self.service.calculate_fare(3), 300)
        self.assertEqual(self.service.calculate_fare(5), 400)

    def test_order_stores_base_fare_and_tariff_note(self):
        order, _ = self.make_order()
        self.assertIsNone(order["distance_km"])
        self.assertEqual(order["fare"], 250)
        self.assertIn("2 км", order["tariff_note"])
        self.assertIn("50 ₽/км", order["tariff_note"])

    def test_accepted_order_disappears_for_other_drivers(self):
        order, sent = self.make_order()
        self.assertEqual(sent, 2)
        records = self.store.driver_messages(order["id"])
        self.assertEqual(len(records), 2)

        accepted = self.service.accept_order(order["id"], self.store.get_driver(200))
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["status"], "accepted")
        self.service.cleanup_after_accept(order["id"], 200)

        current = self.store.get_order(order["id"])
        self.assertEqual(current["status"], "accepted")
        self.assertEqual(current["driver_id"], 200)

        passenger_text = self.passenger_api.messages[current["passenger_message_id"]]["text"]
        self.assertIn("Машина найдена", passenger_text)
        self.assertIn("Иван", passenger_text)
        self.assertIn("Kia Rio", passenger_text)
        self.assertIn("белый", passenger_text)
        self.assertIn("А123ВС77", passenger_text)

        driver_record = self.store.get_driver_message(order["id"], 200)
        self.assertIsNotNone(driver_record)
        self.assertIn(
            "Вы взяли заказ", self.driver_api.messages[driver_record["message_id"]]["text"]
        )

        other_record = self.store.get_driver_message(order["id"], 201)
        self.assertIsNone(other_record)
        self.assertEqual(len(self.driver_api.deleted), 1)

    def test_order_cannot_be_accepted_twice(self):
        order, _ = self.make_order()
        first = self.service.accept_order(order["id"], self.store.get_driver(200))
        second = self.service.accept_order(order["id"], self.store.get_driver(201))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_waiting_order_is_offered_when_driver_comes_online(self):
        for driver_id in (200, 201):
            self.store.set_driver_status(driver_id, "offline")

        preview = self.passenger_api.send_message(100, "preview")
        order = self.service.create_order(
            self.store.get_passenger(100),
            "ул. Ленина, 1",
            "ТЦ Галерея",
            passenger_message_id=preview["message_id"],
        )
        self.assertEqual(self.service.broadcast(order["id"]), 0)

        self.store.set_driver_status(200, "active")
        sent = self.service.offer_waiting_orders(self.store.get_driver(200))
        self.assertEqual(sent, 1)
        self.assertIsNotNone(self.store.get_driver_message(order["id"], 200))

    def test_cancel_removes_order_from_drivers(self):
        order, _ = self.make_order()
        ok = self.service.cancel_order(order["id"], 100)
        self.assertTrue(ok)
        self.assertEqual(self.store.get_order(order["id"])["status"], "cancelled")
        self.assertEqual(self.store.driver_messages(order["id"]), [])
        self.assertEqual(len(self.driver_api.deleted), 2)
        self.assertIn(
            "Заказ отменён",
            self.passenger_api.messages[order["passenger_message_id"]]["text"],
        )

    def test_cancel_after_accept_fines_and_blocks(self):
        order, _ = self.make_order()
        self.service.accept_order(order["id"], self.store.get_driver(200))
        result = self.service.cancel_order(order["id"], 100)
        fine = result["fine"]
        self.assertEqual(fine["amount"], 150)
        self.assertTrue(self.store.is_blocked(100))

    def test_cancel_while_searching_has_no_fine(self):
        order, _ = self.make_order()
        self.assertIsNone(self.service.cancel_order(order["id"], 100)["fine"])
        self.assertFalse(self.store.is_blocked(100))

    def test_receipt_unblocks_automatically(self):
        self.service = TaxiService(
            self.store,
            self.passenger_api,
            self.driver_api,
            fine_amount=150,
            owner_id=999,
        )
        order, _ = self.make_order()
        self.service.accept_order(order["id"], self.store.get_driver(200))
        fine = self.service.cancel_order(order["id"], 100)["fine"]

        self.store.attach_receipt(fine["id"], "FILEID", "receipt.pdf")
        self.service.receive_receipt(
            self.store.get_fine(fine["id"]), self.store.get_passenger(100), "FILEID"
        )

        self.assertFalse(self.store.is_blocked(100))
        self.assertEqual(self.store.get_fine(fine["id"])["status"], "paid")
        receipts = [
            m for m in self.passenger_api.messages.values() if m.get("document")
        ]
        self.assertEqual(receipts[0]["chat_id"], 999)

    def test_accept_after_cancel_fails(self):
        order, _ = self.make_order()
        self.assertTrue(self.service.cancel_order(order["id"], 100))
        self.assertIsNone(
            self.service.accept_order(order["id"], self.store.get_driver(200))
        )

    def test_driver_cannot_take_two_orders(self):
        first, _ = self.make_order()
        second, _ = self.make_order()
        self.assertIsNotNone(
            self.service.accept_order(first["id"], self.store.get_driver(200))
        )
        self.assertIsNone(
            self.service.accept_order(second["id"], self.store.get_driver(200))
        )

    def test_store_accept_is_atomic(self):
        order, _ = self.make_order()
        results = []

        def accept(driver_id):
            results.append(self.store.accept_order(order["id"], driver_id))

        threads = [
            threading.Thread(target=accept, args=(200,)),
            threading.Thread(target=accept, args=(201,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for r in results if r is not None), 1)
        self.assertEqual(self.store.get_order(order["id"])["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
