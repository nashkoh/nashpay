"""Nash Pay — выплаты и приём оплаты в вашем Telegram-боте через @Nashkoh_bot.

Установка:  pip install nashpay   (или положите этот файл рядом со своим ботом)

    from nashpay import NashPay, NashPayError

    pay = NashPay("ВАШ_ТОКЕН")                      # токен: @Nashkoh_bot → Помощь → Nash Pay → приложение

    # выплата пользователю (он должен хоть раз запустить @Nashkoh_bot)
    await pay.payout(user_id, "USDT", "2.5", spend_id=f"withdraw-{withdraw_id}")

    # счёт на оплату (пополнение)
    inv = await pay.create_invoice("USDT", 5, description="VIP на месяц", payload=str(user_id))
    await message.answer(f"Оплатите: {inv.pay_url}")

    # учёт в рублях: счёт на 500 ₽ — сумма в USDT посчитается по текущему курсу
    inv = await pay.create_invoice("USDT", 500, fiat="RUB", payload=str(user_id))
    rate = await pay.get_rate("USDT", "RUB")          # сколько ₽ стоит 1 USDT

    # чек 🧾 (вывод): человек открывает ссылку и забирает деньги в @Nashkoh_bot
    chk = await pay.create_check("USDT", "2.5", pin_to_user_id=user_id, spend_id=f"withdraw-{withdraw_id}")
    await message.answer(f"Ваш вывод: {chk.url}")

Для синхронных ботов (pyTelegramBotAPI и т.п.) — NashPaySync с теми же методами, без await.

Асинхронный клиент использует aiohttp (он уже стоит вместе с aiogram).
Синхронный — только стандартную библиотеку Python.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable

__version__ = "1.1.0"

DEFAULT_API_URL = "https://pay.nashkoh.workers.dev"  # постоянный адрес Nash Pay
API_URL = "__NASH_PAY_URL__"   # подставляется ботом при скачивании; можно передать api_url= в конструктор
BOT_USERNAME = "Nashkoh_bot"

ASSETS = ("GRAM", "USDT")

# Понятные тексты ошибок. e.name — код от сервера, e.text — по-русски для вас,
# e.user_text — можно показать пользователю вашего бота.
ERRORS: dict[str, tuple[str, str | None]] = {
    "UNAUTHORIZED": ("Неверный токен приложения", None),
    "SERVICE_DISABLED": ("Nash Pay временно отключён", "Выплаты временно недоступны, попробуйте позже."),
    "MAINTENANCE": ("Технические работы в Nash Koh", "Выплаты временно недоступны, попробуйте позже."),
    "APP_BLOCKED": ("Приложение заблокировано администрацией Nash Koh", "Выплаты временно недоступны."),
    "TOO_MANY_REQUESTS": ("Слишком много запросов (лимит 30 в секунду)", None),
    "TRANSFERS_DISABLED": ("Выплаты и чеки выключены: включите их в приложении → «🛡 Выплаты»",
                           "Выплаты временно недоступны, попробуйте позже."),
    "INSUFFICIENT_FUNDS": ("На балансе приложения не хватает средств — пополните его в @Nashkoh_bot",
                           "Выплаты временно недоступны, попробуйте позже."),
    "USER_NOT_FOUND": ("Пользователь ни разу не запускал @Nashkoh_bot",
                       f"Чтобы получить выплату, откройте @{BOT_USERNAME}, нажмите «Старт» и повторите."),
    "USER_BLOCKED": ("Пользователь заблокирован в Nash Koh", "Выплата на ваш кошелёк Nash Koh невозможна."),
    "SPEND_ID_USED": ("Этот spend_id уже использован для другой выплаты", None),
    "SPEND_ID_INVALID": ("spend_id пустой или длиннее 64 символов", None),
    "CHECK_NOT_FOUND": ("Чек не найден или уже активирован", None),
    "FIAT_INVALID": ("Неизвестная фиатная валюта — USD, EUR, RUB, UAH или KZT", None),
    "RATE_UNAVAILABLE": ("Курс временно недоступен, повторите через минуту", "Сервис временно недоступен, попробуйте позже."),
    "AMOUNT_TOO_SMALL": ("Сумма слишком мала: по курсу выходит меньше минимальной суммы в монете", None),
    "PIN_TO_USER_ID_INVALID": ("pin_to_user_id — числовой Telegram ID", None),
    "PIN_ONLY_ONE": ("Укажите или pin_to_user_id, или pin_to_username — не оба", None),
    "ASSET_INVALID": ("Неизвестная валюта — используйте GRAM или USDT", None),
    "AMOUNT_INVALID": ("Неверная сумма: больше нуля, не больше 6 знаков после точки для USDT и 9 для GRAM", None),
    "COMMENT_TOO_LONG": ("Комментарий длиннее 1024 символов", None),
    "DESCRIPTION_TOO_LONG": ("Описание счёта слишком длинное", None),
    "PAYLOAD_TOO_LONG": ("payload слишком длинный", None),
    "PAID_BTN_URL_INVALID": ("paid_btn_url должен начинаться с https://, http:// или tg://", None),
    "EXPIRES_IN_INVALID": ("expires_in — от 1 секунды до 31 дня", None),
    "INVOICE_NOT_FOUND": ("Счёт не найден", None),
    "INVOICE_NOT_ACTIVE": ("Счёт уже оплачен, отменён или истёк", None),
    "PARAM_INVALID": ("Неверный параметр запроса", None),
    "METHOD_NOT_FOUND": ("Такого метода нет — обновите nashpay.py", None),
    "NETWORK_ERROR": ("Нет связи с Nash Pay", "Сервис временно недоступен, попробуйте позже."),
}


class NashPayError(Exception):
    """Ошибка Nash Pay. name — код (например INSUFFICIENT_FUNDS), text — описание, user_text — для пользователя."""

    def __init__(self, name: str, code: int = 400, detail: str = ""):
        self.name, self.code, self.detail = name, code, detail
        self.text, user = ERRORS.get(name, (name, None))
        if detail:  # для «Нет связи»: что именно случилось и куда стучались — видно в логах и у админа
            self.text = f"{self.text} ({detail})"
        self.user_text = user or "Не получилось выполнить операцию, попробуйте позже."
        # True — связь оборвалась во время выплаты: деньги МОГЛИ уйти. Не возвращайте баланс сразу,
        # а повторите payout с тем же spend_id (второй раз не заплатит) или проверьте get_transfer(spend_id).
        self.uncertain = False
        super().__init__(f"{name}: {self.text}")


# ---------- Объекты ответа ----------

def _dec(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


@dataclass
class Invoice:
    invoice_id: int
    hash: str
    status: str               # active | paid | expired | cancelled
    asset: str
    amount: Decimal
    pay_url: str              # ссылка «оплатить» — отправьте её покупателю
    description: str | None = None
    payload: str | None = None
    paid_at: str | None = None
    fee_amount: Decimal = Decimal(0)
    fiat: str | None = None              # счёт в фиате: RUB, UAH…
    fiat_amount: Decimal | None = None   # сумма в фиате (её и зачисляйте, если учёт в рублях)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"

    @classmethod
    def from_json(cls, d: dict) -> "Invoice":
        return cls(invoice_id=d["invoice_id"], hash=d.get("hash", ""), status=d["status"], asset=d["asset"],
                   amount=_dec(d["amount"]), pay_url=d.get("pay_url") or d.get("bot_invoice_url", ""),
                   description=d.get("description"), payload=d.get("payload"), paid_at=d.get("paid_at"),
                   fee_amount=_dec(d.get("fee_amount")), fiat=d.get("fiat"),
                   fiat_amount=_dec(d["fiat_amount"]) if d.get("fiat_amount") else None, raw=d)


@dataclass
class Transfer:
    transfer_id: int
    spend_id: str
    user_id: int
    asset: str
    amount: Decimal
    status: str
    completed_at: str
    comment: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict) -> "Transfer":
        return cls(transfer_id=d["transfer_id"], spend_id=d["spend_id"], user_id=d["user_id"], asset=d["asset"],
                   amount=_dec(d["amount"]), status=d["status"], completed_at=d["completed_at"],
                   comment=d.get("comment"), raw=d)


@dataclass
class Check:
    check_id: int
    hash: str
    asset: str
    amount: Decimal
    url: str                  # ссылка на чек — отправьте её человеку
    status: str               # active | activated | deleted
    pin_to_user_id: int | None = None
    pin_to_username: str | None = None
    activated_by: int | None = None
    fiat: str | None = None
    fiat_amount: Decimal | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_activated(self) -> bool:
        return self.status == "activated"

    @classmethod
    def from_json(cls, d: dict) -> "Check":
        return cls(check_id=d["check_id"], hash=d["hash"], asset=d["asset"], amount=_dec(d["amount"]),
                   url=d["bot_check_url"], status=d["status"], pin_to_user_id=d.get("pin_to_user_id"),
                   pin_to_username=d.get("pin_to_username"), activated_by=d.get("activated_by"),
                   fiat=d.get("fiat"), fiat_amount=_dec(d["fiat_amount"]) if d.get("fiat_amount") else None, raw=d)


@dataclass
class Rate:
    asset: str
    fiat: str
    rate: Decimal            # сколько фиата стоит 1 монета
    updated_at: str

    def to_fiat(self, amount) -> Decimal:
        return (Decimal(str(amount)) * self.rate).quantize(Decimal("0.01"))

    def to_crypto(self, fiat_amount) -> Decimal:
        return (Decimal(str(fiat_amount)) / self.rate).quantize(Decimal("0.000001"))


# ---------- Общая часть ----------

def _amount(v) -> str:
    if isinstance(v, float):
        v = Decimal(repr(v))          # 0.1 → "0.1", а не 0.1000000000000000055
    d = Decimal(str(v))
    return f"{d.normalize():f}"


def _asset(a: str) -> str:
    a = str(a).upper().strip()
    return "GRAM" if a == "TON" else a


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


def _payout_params(user_id, asset, amount, spend_id, comment) -> dict:
    if not spend_id:
        raise ValueError("spend_id обязателен: уникальный номер выплаты в вашей базе, "
                         "например f'withdraw-{id}'. Он защищает от двойной выплаты.")
    return _clean({"user_id": int(user_id), "asset": _asset(asset), "amount": _amount(amount),
                   "spend_id": str(spend_id), "comment": comment})


def _invoice_params(asset, amount, description, payload, paid_btn_url, expires_in, fiat=None) -> dict:
    return _clean({"asset": _asset(asset), "amount": _amount(amount), "description": description,
                   "fiat": fiat.upper() if fiat else None,
                   "payload": None if payload is None else str(payload), "paid_btn_url": paid_btn_url,
                   "expires_in": expires_in})


def _check_params(asset, amount, pin_to_user_id, pin_to_username, spend_id, comment, fiat=None) -> dict:
    return _clean({"asset": _asset(asset), "amount": _amount(amount), "fiat": fiat.upper() if fiat else None,
                   "pin_to_user_id": int(pin_to_user_id) if pin_to_user_id else None,
                   "pin_to_username": pin_to_username, "spend_id": spend_id, "comment": comment})


def _ids(ids) -> str | None:
    if ids is None:
        return None
    if isinstance(ids, (int, str)):
        ids = [ids]
    return ",".join(str(int(i)) for i in ids)


def _url(api_url: str | None) -> str:
    u = (api_url or (API_URL if API_URL.startswith("http") else DEFAULT_API_URL)).rstrip("/")
    if not u.startswith("http"):
        raise ValueError("Не задан адрес API: передайте api_url='https://…' или скачайте nashpay.py заново в боте")
    return u + ("" if u.endswith("/api") else "/api")


def _root(api: str) -> str:
    return api[:-4] if api.endswith("/api") else api


def _api_from_location(loc: str, fallback: str) -> str:
    """Постоянный адрес отвечает переадресацией на текущий сервер — берём из неё адрес сервера."""
    from urllib.parse import urlsplit
    p = urlsplit(loc)
    return f"{p.scheme}://{p.netloc}/api" if p.scheme in ("http", "https") and p.netloc else fallback


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _unpack(status: int, body: bytes, url: str = ""):
    try:
        data = json.loads(body)
    except ValueError:  # ответил не Nash Pay: неверный адрес, порт или сервер не запущен
        raise NashPayError("NETWORK_ERROR", status, f"HTTP {status}, ответ не от Nash Pay — проверьте адрес {url}")
    if not data.get("ok"):
        err = data.get("error") or {}
        raise NashPayError(err.get("name", "UNKNOWN"), err.get("code", status))
    return data["result"]


_RETRY_SAFE = {"getMe", "getBalance", "getCurrencies", "getInvoices", "getTransfers", "getChecks", "transfer",
               "getRate", "getExchangeRates"}
# transfer повторять безопасно: тот же spend_id вернёт уже сделанную выплату, второй раз деньги не уйдут


# ---------- Асинхронный клиент (aiogram и т.п.) ----------

class NashPay:
    """Асинхронный клиент. Создайте один раз при старте бота и используйте везде."""

    def __init__(self, token: str, api_url: str | None = None, timeout: float = 15, retries: int = 3):
        self.token, self.api = token.strip(), _url(api_url)
        self.timeout, self.retries = timeout, retries
        self._session = None
        self._real: str | None = None  # адрес сервера, куда ведёт постоянный адрес (узнаём один раз)

    async def _server(self, s) -> str:
        """Постоянный адрес (например pay.nashkoh.workers.dev) переадресует на текущий сервер.
        Узнаём сервер один раз и дальше ходим напрямую; при сбое связи — узнаём заново."""
        if self._real is None:
            try:
                async with s.get(_root(self.api) + "/", allow_redirects=False) as r:
                    loc = r.headers.get("Location") if r.status in (301, 302, 303, 307, 308) else None
                self._real = _api_from_location(loc, self.api) if loc else self.api
            except Exception:
                return self.api
        return self._real

    async def _http(self):
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def call(self, method: str, **params) -> Any:
        """Прямой вызов любого метода API."""
        import aiohttp
        attempts = self.retries if method in _RETRY_SAFE else 1
        n, moved, base = 0, False, self.api
        while True:
            try:
                s = await self._http()
                base = await self._server(s)
                async with s.post(f"{base}/{method}", json=_clean(params), allow_redirects=False,
                                  headers={"Nash-Pay-API-Token": self.token}) as r:
                    body = await r.read()
                    if r.status in (301, 302, 303, 307, 308) and not moved:
                        # сервер переехал: запрос НЕ выполнен — узнаём новый адрес и сразу повторяем
                        self._real, moved = None, True
                        continue
                    if r.status == 429 or r.status >= 500:
                        raise NashPayError("TOO_MANY_REQUESTS" if r.status == 429 else "NETWORK_ERROR", r.status,
                                           "" if r.status == 429 else f"HTTP {r.status} от {base}")
                    return _unpack(r.status, body, base)
            except (aiohttp.ClientError, asyncio.TimeoutError, NashPayError) as e:
                retryable = not isinstance(e, NashPayError) or e.name in ("TOO_MANY_REQUESTS", "NETWORK_ERROR")
                if not isinstance(e, NashPayError) or e.name == "NETWORK_ERROR":
                    self._real = None  # при следующей попытке заново узнаём, где сервер
                n += 1
                if not retryable or n >= attempts:
                    if isinstance(e, NashPayError):
                        raise e
                    why = "таймаут" if isinstance(e, asyncio.TimeoutError) else (str(e) or type(e).__name__)
                    raise NashPayError("NETWORK_ERROR", 0, f"{why} — адрес {base}") from e
                await asyncio.sleep(0.5 * 2 ** (n - 1))

    # --- Приложение ---
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_balance(self) -> dict[str, Decimal]:
        """{'GRAM': Decimal('1.5'), 'USDT': Decimal('20')}"""
        return {b["currency_code"]: _dec(b["available"]) for b in await self.call("getBalance")}

    async def get_currencies(self) -> list[dict]:
        return await self.call("getCurrencies")

    # --- Выплаты ---
    async def payout(self, user_id: int, asset: str, amount, spend_id: str, comment: str | None = None) -> Transfer:
        """Перевести деньги пользователю на его кошелёк в @Nashkoh_bot.

        user_id  — Telegram ID получателя;
        spend_id — ваш уникальный номер выплаты (до 64 символов). Повтор с тем же spend_id
                   не заплатит второй раз, а вернёт уже сделанную выплату.
        """
        params = _payout_params(user_id, asset, amount, spend_id, comment)
        try:
            return Transfer.from_json(await self.call("transfer", **params))
        except NashPayError as e:
            e.uncertain = e.name == "NETWORK_ERROR"
            raise

    transfer = payout

    async def get_transfers(self, transfer_ids=None, spend_id: str | None = None,
                            offset: int = 0, count: int = 100) -> list[Transfer]:
        r = await self.call("getTransfers", transfer_ids=_ids(transfer_ids), spend_id=spend_id, offset=offset, count=count)
        return [Transfer.from_json(t) for t in r["items"]]

    async def get_transfer(self, spend_id: str) -> Transfer | None:
        items = await self.get_transfers(spend_id=spend_id, count=1)
        return items[0] if items else None

    # --- Счета ---
    async def create_invoice(self, asset: str, amount, description: str | None = None, payload: str | None = None,
                             paid_btn_url: str | None = None, expires_in: int | None = None,
                             fiat: str | None = None) -> Invoice:
        """Счёт на оплату. Отправьте покупателю invoice.pay_url. payload вернётся вам при оплате.
        fiat="RUB" — amount в рублях, сумма в asset посчитается по текущему курсу (invoice.amount)."""
        return Invoice.from_json(await self.call(
            "createInvoice", **_invoice_params(asset, amount, description, payload, paid_btn_url, expires_in, fiat)))

    # --- Курсы ---
    async def get_rate(self, asset: str, fiat: str) -> Rate:
        """Сколько фиата стоит 1 монета: await pay.get_rate("USDT", "RUB") → Rate(rate=91.35, ...)."""
        r = await self.call("getRate", asset=_asset(asset), fiat=fiat.upper())
        return Rate(r["source"], r["target"], _dec(r["rate"]), r["updated_at"])

    async def get_exchange_rates(self) -> list[Rate]:
        return [Rate(r["source"], r["target"], _dec(r["rate"]), r.get("updated_at", ""))
                for r in await self.call("getExchangeRates") if r.get("is_valid")]

    async def get_invoices(self, invoice_ids=None, status: str | None = None, asset: str | None = None,
                           offset: int = 0, count: int = 100) -> list[Invoice]:
        r = await self.call("getInvoices", invoice_ids=_ids(invoice_ids), status=status,
                            asset=_asset(asset) if asset else None, offset=offset, count=count)
        return [Invoice.from_json(i) for i in r["items"]]

    async def get_invoice(self, invoice_id: int) -> Invoice | None:
        items = await self.get_invoices(invoice_ids=[invoice_id])
        return items[0] if items else None

    async def delete_invoice(self, invoice_id: int) -> bool:
        return bool(await self.call("deleteInvoice", invoice_id=int(invoice_id)))

    # --- Чеки ---
    async def create_check(self, asset: str, amount, pin_to_user_id: int | None = None,
                           pin_to_username: str | None = None, spend_id: str | None = None,
                           comment: str | None = None, fiat: str | None = None) -> Check:
        """Чек 🧾: деньги списываются с баланса приложения, человек забирает их по ссылке check.url в @Nashkoh_bot.

        pin_to_user_id / pin_to_username — забрать сможет только этот человек (рекомендуем для выводов);
        spend_id — ваш номер заявки: повтор с тем же spend_id вернёт уже созданный чек, а не второй;
        comment — текст, который человек увидит при активации (до 200 символов);
        fiat="RUB" — amount в рублях, сумма в asset посчитается по текущему курсу.
        """
        params = _check_params(asset, amount, pin_to_user_id, pin_to_username, spend_id, comment, fiat)
        try:
            return Check.from_json(await self.call("createCheck", **params))
        except NashPayError as e:
            e.uncertain = e.name == "NETWORK_ERROR"
            raise

    async def get_checks(self, check_ids=None, status: str | None = None,
                         offset: int = 0, count: int = 100) -> list[Check]:
        r = await self.call("getChecks", check_ids=_ids(check_ids), status=status, offset=offset, count=count)
        return [Check.from_json(x) for x in r["items"]]

    async def get_check(self, check_id: int) -> Check | None:
        items = await self.get_checks(check_ids=[check_id])
        return items[0] if items else None

    async def delete_check(self, check_id: int) -> bool:
        """Удалить неактивированный чек — деньги вернутся на баланс приложения."""
        return bool(await self.call("deleteCheck", check_id=int(check_id)))

    async def wait_paid(self, invoice_id: int, timeout: float = 900, every: float = 5) -> Invoice | None:
        """Ждать оплату счёта без вебхука. Вернёт счёт, когда оплачен, или None (истёк/отменён/время вышло)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            inv = await self.get_invoice(invoice_id)
            if inv is None or inv.status in ("expired", "cancelled"):
                return None
            if inv.is_paid:
                return inv
            await asyncio.sleep(every)
        return None

    # --- Вебхуки ---
    def check_signature(self, body: bytes, signature: str) -> bool:
        return check_signature(self.token, body, signature)

    def webhook_handler(self, on_paid: Callable[[Invoice], Awaitable[None]]):
        """Обработчик для aiohttp: app.router.add_post('/nashpay', pay.webhook_handler(on_paid))."""
        from aiohttp import web

        async def handler(request):
            body = await request.read()
            sig = request.headers.get("nash-pay-api-signature") or request.headers.get("crypto-pay-api-signature", "")
            if not self.check_signature(body, sig):
                return web.Response(status=401, text="bad signature")
            upd = json.loads(body)
            if upd.get("update_type") == "invoice_paid":
                await on_paid(Invoice.from_json(upd["payload"]))
            return web.Response(text="ok")
        return handler


# ---------- Синхронный клиент (pyTelegramBotAPI, скрипты) ----------

class NashPaySync:
    """То же самое без async: pay = NashPaySync(TOKEN); pay.payout(...)."""

    def __init__(self, token: str, api_url: str | None = None, timeout: float = 15, retries: int = 3):
        self.token, self.api = token.strip(), _url(api_url)
        self.timeout, self.retries = timeout, retries
        self._real: str | None = None
        self._open = urllib.request.build_opener(_NoRedirect()).open

    def _server(self) -> str:
        """См. NashPay._server: один раз узнаём текущий сервер за постоянным адресом."""
        if self._real is None:
            try:
                with self._open(urllib.request.Request(_root(self.api) + "/", headers={
                        "User-Agent": f"nashpay/{__version__}"}), timeout=self.timeout):
                    self._real = self.api
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
                self._real = _api_from_location(loc, self.api) if loc else self.api
            except Exception:
                return self.api
        return self._real

    def call(self, method: str, **params) -> Any:
        attempts = self.retries if method in _RETRY_SAFE else 1
        data = json.dumps(_clean(params)).encode()
        n, moved = 0, False
        while True:
            base = self._server()
            req = urllib.request.Request(f"{base}/{method}", data=data, method="POST", headers={
                "Content-Type": "application/json", "Nash-Pay-API-Token": self.token, "User-Agent": f"nashpay/{__version__}"})
            try:
                with self._open(req, timeout=self.timeout) as r:
                    return _unpack(r.status, r.read(), base)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and not moved:
                    self._real, moved = None, True  # сервер переехал: запрос не выполнен — повторяем по новому адресу
                    continue
                if e.code == 429 or e.code >= 500:
                    err = NashPayError("TOO_MANY_REQUESTS" if e.code == 429 else "NETWORK_ERROR", e.code,
                                       "" if e.code == 429 else f"HTTP {e.code} от {base}")
                else:
                    return _unpack(e.code, e.read(), base)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self._real = None
                err = NashPayError("NETWORK_ERROR", 0, f"{getattr(e, 'reason', None) or e} — адрес {base}")
            n += 1
            if n >= attempts:
                raise err
            time.sleep(0.5 * 2 ** (n - 1))

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_balance(self) -> dict[str, Decimal]:
        return {b["currency_code"]: _dec(b["available"]) for b in self.call("getBalance")}

    def get_currencies(self) -> list[dict]:
        return self.call("getCurrencies")

    def payout(self, user_id: int, asset: str, amount, spend_id: str, comment: str | None = None) -> Transfer:
        params = _payout_params(user_id, asset, amount, spend_id, comment)
        try:
            return Transfer.from_json(self.call("transfer", **params))
        except NashPayError as e:
            e.uncertain = e.name == "NETWORK_ERROR"
            raise

    transfer = payout

    def get_transfers(self, transfer_ids=None, spend_id: str | None = None,
                      offset: int = 0, count: int = 100) -> list[Transfer]:
        r = self.call("getTransfers", transfer_ids=_ids(transfer_ids), spend_id=spend_id, offset=offset, count=count)
        return [Transfer.from_json(t) for t in r["items"]]

    def get_transfer(self, spend_id: str) -> Transfer | None:
        items = self.get_transfers(spend_id=spend_id, count=1)
        return items[0] if items else None

    def create_invoice(self, asset: str, amount, description: str | None = None, payload: str | None = None,
                       paid_btn_url: str | None = None, expires_in: int | None = None, fiat: str | None = None) -> Invoice:
        return Invoice.from_json(self.call(
            "createInvoice", **_invoice_params(asset, amount, description, payload, paid_btn_url, expires_in, fiat)))

    def get_rate(self, asset: str, fiat: str) -> Rate:
        r = self.call("getRate", asset=_asset(asset), fiat=fiat.upper())
        return Rate(r["source"], r["target"], _dec(r["rate"]), r["updated_at"])

    def get_exchange_rates(self) -> list[Rate]:
        return [Rate(r["source"], r["target"], _dec(r["rate"]), r.get("updated_at", ""))
                for r in self.call("getExchangeRates") if r.get("is_valid")]

    def get_invoices(self, invoice_ids=None, status: str | None = None, asset: str | None = None,
                     offset: int = 0, count: int = 100) -> list[Invoice]:
        r = self.call("getInvoices", invoice_ids=_ids(invoice_ids), status=status,
                      asset=_asset(asset) if asset else None, offset=offset, count=count)
        return [Invoice.from_json(i) for i in r["items"]]

    def get_invoice(self, invoice_id: int) -> Invoice | None:
        items = self.get_invoices(invoice_ids=[invoice_id])
        return items[0] if items else None

    def delete_invoice(self, invoice_id: int) -> bool:
        return bool(self.call("deleteInvoice", invoice_id=int(invoice_id)))

    def create_check(self, asset: str, amount, pin_to_user_id: int | None = None, pin_to_username: str | None = None,
                     spend_id: str | None = None, comment: str | None = None, fiat: str | None = None) -> Check:
        params = _check_params(asset, amount, pin_to_user_id, pin_to_username, spend_id, comment, fiat)
        try:
            return Check.from_json(self.call("createCheck", **params))
        except NashPayError as e:
            e.uncertain = e.name == "NETWORK_ERROR"
            raise

    def get_checks(self, check_ids=None, status: str | None = None, offset: int = 0, count: int = 100) -> list[Check]:
        r = self.call("getChecks", check_ids=_ids(check_ids), status=status, offset=offset, count=count)
        return [Check.from_json(x) for x in r["items"]]

    def get_check(self, check_id: int) -> Check | None:
        items = self.get_checks(check_ids=[check_id])
        return items[0] if items else None

    def delete_check(self, check_id: int) -> bool:
        return bool(self.call("deleteCheck", check_id=int(check_id)))

    def check_signature(self, body: bytes, signature: str) -> bool:
        return check_signature(self.token, body, signature)


def check_signature(token: str, body: bytes, signature: str) -> bool:
    """Проверка подписи вебхука: HMAC-SHA256(ключ = sha256(токен), тело запроса)."""
    key = hashlib.sha256(token.strip().encode()).digest()
    good = hmac.new(key, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, (signature or "").strip().lower())


# ---------- Пример: вывод в боте на aiogram 3 ----------
#
# from nashpay import NashPay, NashPayError
# pay = NashPay(NASH_PAY_TOKEN)
#
# @router.callback_query(F.data == "withdraw")
# async def withdraw(c: CallbackQuery):
#     w = await db.create_withdraw(c.from_user.id, amount)          # запись в вашей базе, списали баланс
#     try:
#         t = await pay.payout(c.from_user.id, "USDT", amount, spend_id=f"w{w.id}",
#                              comment="Вывод из MyBot")
#         await c.message.answer(f"✅ Отправлено {t.amount} {t.asset} на ваш кошелёк @Nashkoh_bot")
#     except NashPayError as e:
#         logging.warning("Nash Pay: %s", e)                        # e.text — причина для вас
#         if e.uncertain:                                           # связь оборвалась — деньги могли уйти
#             await c.message.answer("⏳ Вывод обрабатывается, сообщим о результате.")
#             return                                                # позже: pay.payout(..., spend_id=f"w{w.id}") ещё раз
#         await db.refund_withdraw(w.id)                            # точно не ушло — вернули баланс
#         await c.message.answer(e.user_text)
