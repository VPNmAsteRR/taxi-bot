"""Admin panel texts and keyboards shared by both bots."""

from core import esc, fare_text, km_text, public_id

STATUS_LABELS = {
    "searching": "ищет машину",
    "accepted": "в поездке",
    "completed": "завершён",
    "cancelled": "отменён",
}


def _money(value):
    return f"{int(value or 0):,}".replace(",", " ") + " ₽"


def admin_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔄 Обновить", "callback_data": "a:panel"}],
            [{"text": "🚕 Водители на смене", "callback_data": "a:drivers"}],
            [{"text": "📦 Последние заказы", "callback_data": "a:orders"}],
            [{"text": "⬅️ Назад", "callback_data": "a:back"}],
        ]
    }


def admin_panel_text(store):
    stats = store.admin_stats()
    free_drivers = max(0, stats["drivers_active"] - stats["orders_accepted"])
    fines = store.fines_stats()
    return "\n".join(
        [
            "⚙️ <b>Админка TAXIDRIVE24/7</b>",
            "",
            "🚕 <b>Водители</b>",
            f"• на смене: <b>{stats['drivers_active']}</b> из {stats['drivers_total']}",
            f"• на заказе: <b>{stats['orders_accepted']}</b>",
            f"• свободны: <b>{free_drivers}</b>",
            "",
            "📦 <b>Заказы</b>",
            f"• ищут машину: <b>{stats['orders_searching']}</b>",
            f"• в поездке: <b>{stats['orders_accepted']}</b>",
            f"• за сегодня: <b>{stats['orders_today']}</b>",
            f"• завершено сегодня: <b>{stats['completed_today']}</b>",
            f"• отменено сегодня: <b>{stats['cancelled_today']}</b>",
            "",
            f"💰 <b>Выручка по посадкам за сегодня:</b> {_money(stats['revenue_today'])}",
            "",
            f"💸 <b>Штрафы</b>: ждут оплаты — {fines['open']}, "
            f"получено — {_money(fines['paid_sum'])}",
            "",
            f"👥 Пассажиров в базе: {stats['passengers_total']}",
            f"🛡 Админов: {stats['admins_total']}",
        ]
    )


def admin_drivers_text(store):
    drivers = store.drivers_on_shift()
    if not drivers:
        return (
            "🚕 <b>Водители на смене</b>\n\n"
            "Сейчас на смене никого нет."
        )

    lines = [
        "🚕 <b>Водители на смене</b>",
        f"Всего: <b>{len(drivers)}</b>",
        "",
    ]
    for driver in drivers[:25]:
        lines.append(
            f"• <b>{esc(driver['name'])}</b> — "
            f"{esc(driver['car'])}, {esc(driver['color'])}, "
            f"{esc(driver['plate'])}"
        )
        if driver.get("phone"):
            lines.append(f"  📞 {esc(driver['phone'])}")
        if driver.get("order_id"):
            lines.append(
                f"  🚖 В заказе <b>#{int(driver['order_id']):04d}</b>: "
                f"{esc(driver['order_from'])} → {esc(driver['order_to'])}"
            )
        else:
            lines.append("  🟢 Свободен")
        lines.append("")

    if len(drivers) > 25:
        lines.append(f"Показаны первые 25 из {len(drivers)}.")
    return "\n".join(lines).rstrip()


def admin_orders_text(store):
    orders = store.recent_orders(10)
    if not orders:
        return (
            "📦 <b>Последние заказы</b>\n\n"
            "Заказов пока нет."
        )

    lines = ["📦 <b>Последние заказы</b>", ""]
    for order in orders:
        status = STATUS_LABELS.get(order["status"], order["status"])
        distance = (
            f" · {km_text(order['distance_km'])}"
            if order.get("distance_km") is not None
            else ""
        )
        lines.append(
            f"<b>{public_id(order)}</b> · {status} · "
            f"{fare_text(order['fare'])}{distance}"
        )
        lines.append(
            f"  {esc(order['from_addr'])} → {esc(order['to_addr'])}"
        )
        if order.get("driver_name"):
            lines.append(f"  🚖 {esc(order['driver_name'])}")
        lines.append("")
    return "\n".join(lines).rstrip()
