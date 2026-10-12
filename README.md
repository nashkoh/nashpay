# nashpay

Библиотека **Nash Pay** для Python: выплаты, чеки 🧾 и приём оплаты в **GRAM** и **USDT**
в вашем Telegram-боте через кошелёк [@Nashkoh_bot](https://t.me/Nashkoh_bot).

```bash
pip install nashpay
```

- Выплаты пользователям — мгновенно и без комиссии, деньги приходят им в @Nashkoh_bot.
- Чеки — ссылка, по которой человек сам забирает деньги (удобно для выводов).
- Счета — пополнение баланса в вашем боте в один клик, с вебхуком об оплате.
- Учёт в рублях и других валютах: RUB, UAH, KZT, USD, EUR — сумма в крипте считается по курсу.
- Защита от двойных выплат: один `spend_id` — одна выплата, даже если запрос повторился.

Работает с aiogram и любым асинхронным кодом (`NashPay`), а также с pyTelegramBotAPI
и обычными скриптами (`NashPaySync` — те же методы без `await`).

## Как получить токен

1. Откройте [@Nashkoh_bot](https://t.me/Nashkoh_bot) → **Помощь → 💼 Nash Pay интеграция**.
2. Создайте приложение — бот выдаст API-токен.
3. Пополните баланс приложения: с него идут выплаты и чеки.

Токен — это доступ к деньгам приложения. Храните его в `.env`, не публикуйте в коде.

## Быстрый старт

```python
from nashpay import NashPay, NashPayError

pay = NashPay("ВАШ_ТОКЕН")   # создайте один раз при старте бота

print(await pay.get_balance())   # {'GRAM': Decimal('3.5'), 'USDT': Decimal('20')}
```

## Вывод чеком 🧾

Пользователь нажимает «Вывести» — бот создаёт чек и отправляет ссылку. Забрать его сможет только этот человек.

```python
chk = await pay.create_check(
    "USDT", "2.5",
    pin_to_user_id=user_id,              # забрать сможет только он
    spend_id=f"withdraw-{withdraw_id}",  # номер вашей заявки — второй чек не создастся
    comment="Вывод из моего бота",
)
await message.answer(f"Ваш вывод: {chk.url}")
```

Вывод в рублях — сумма в USDT посчитается по текущему курсу:

```python
chk = await pay.create_check("USDT", 300, fiat="RUB", pin_to_user_id=user_id, spend_id=f"withdraw-{withdraw_id}")
```

## Выплата сразу на кошелёк

```python
try:
    await pay.payout(user_id, "USDT", "1.5", spend_id=f"bonus-{user_id}-{day}")
except NashPayError as e:
    if e.uncertain:      # связь оборвалась во время выплаты — деньги МОГЛИ уйти
        t = await pay.get_transfer(f"bonus-{user_id}-{day}")   # проверьте, прежде чем возвращать баланс
    else:
        await message.answer(e.user_text)   # понятный текст для пользователя
        print(e.text)                       # подробности для вас
```

Пользователь должен хоть раз запустить @Nashkoh_bot.

## Пополнение (счёт на оплату)

```python
inv = await pay.create_invoice("USDT", 5, description="Пополнение баланса", payload=str(user_id))
await message.answer(f"Оплатите: {inv.pay_url}")
```

Узнать об оплате — вебхуком (aiohttp):

```python
from aiohttp import web

async def on_paid(inv):
    user_id = int(inv.payload)
    # зачислите inv.amount (или inv.fiat_amount, если счёт в рублях)

app = web.Application()
app.router.add_post("/nashpay", pay.webhook_handler(on_paid))   # подпись проверяется сама
```

Адрес вебхука укажите в настройках приложения в @Nashkoh_bot.
Без вебхука можно просто подождать: `inv = await pay.wait_paid(inv.invoice_id)`.

Комиссия Nash Pay — с оплаченного счёта, удерживается из суммы, которая приходит на баланс приложения.
Выплаты, чеки и вывод в кошелёк — без комиссии.

## Курсы

```python
rate = await pay.get_rate("USDT", "RUB")   # сколько ₽ стоит 1 USDT
print(rate.rate)
```

## Синхронная версия

```python
from nashpay import NashPaySync

pay = NashPaySync("ВАШ_ТОКЕН")
chk = pay.create_check("USDT", "2.5", pin_to_user_id=user_id, spend_id="withdraw-42")
```

`NashPaySync` использует только стандартную библиотеку Python.

## Ошибки

Все ошибки — `NashPayError`:

| Поле | Что это |
|---|---|
| `e.name` | код: `INSUFFICIENT_FUNDS`, `UNAUTHORIZED`, `NETWORK_ERROR`… |
| `e.text` | описание для вас (по-русски) |
| `e.user_text` | текст, который можно показать пользователю |
| `e.uncertain` | `True` — связь оборвалась во время выплаты или создания чека, результат неизвестен |

При сбоях сети библиотека сама повторяет безопасные запросы.

## Ссылки

- Бот: [@Nashkoh_bot](https://t.me/Nashkoh_bot)
- Сайт: [nashkoh.github.io](https://nashkoh.github.io)
- Канал: [@NashKoh_Chanel](https://t.me/NashKoh_Chanel)

Лицензия MIT.
