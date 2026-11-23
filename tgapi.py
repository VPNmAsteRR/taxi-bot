"""Small dependency-free Telegram Bot API client.

Only the methods this project needs: long polling, text messages with inline
keyboards, editing, deleting and callback answers.
"""

import json
import time
import urllib.error
import urllib.request


class TelegramError(Exception):
    def __init__(self, method, code, description, parameters=None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.parameters = parameters or {}


class BotAPI:
    def __init__(self, token, api_base="https://api.telegram.org", request_timeout=60):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.request_timeout = request_timeout
        self._method_url = f"{self.api_base}/bot{token}/"

    # ------------------------------------------------------------------ core
    def call(self, method, params=None, timeout=None):
        delay = 1
        last = None
        for _ in range(5):
            try:
                return self._call_once(method, params, timeout)
            except TelegramError as exc:
                last = exc
                if exc.code == 429:
                    wait = int(exc.parameters.get("retry_after", 3))
                    time.sleep(max(1, min(wait, 30)))
                    continue
                if exc.code == 0 or 500 <= exc.code < 600:
                    time.sleep(delay)
                    delay = min(delay * 2, 15)
                    continue
                raise
        raise last

    def _call_once(self, method, params=None, timeout=None):
        body = json.dumps(params or {}).encode()
        req = urllib.request.Request(
            self._method_url + method,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.request_timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                raise TelegramError(method, exc.code, exc.reason) from exc
        except urllib.error.URLError as exc:
            raise TelegramError(method, 0, f"network error: {exc.reason}") from exc

        if not payload.get("ok"):
            raise TelegramError(
                method,
                payload.get("error_code", 0),
                payload.get("description", "unknown error"),
                payload.get("parameters"),
            )
        return payload["result"]

    # ------------------------------------------------------------- updating
    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=None, timeout=25, allowed_updates=None):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        return self.call("getUpdates", params, timeout=timeout + 30)

    def set_my_commands(self, commands):
        return self.call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
        )

    # ------------------------------------------------------------- messaging
    def send_message(self, chat_id, text, **kwargs):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id, message_id, text, **kwargs):
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return self.call("editMessageText", params)

    def delete_message(self, chat_id, message_id):
        return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def send_document(self, chat_id, document, caption=None, reply_markup=None):
        """Send a document by file_id, without re-uploading it."""
        params = {"chat_id": chat_id, "document": document}
        if caption:
            params["caption"] = caption
            params["parse_mode"] = "HTML"
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendDocument", params)

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        params = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            params["text"] = text[:200]
        return self.call("answerCallbackQuery", params)
