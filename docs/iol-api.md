# La API de IOL que usa caucionetta

> Esto **no** es documentación oficial. Describe exactamente lo que manda y espera `caucionetta/api.py`. IOL entrega su
> referencia de API a clientes que piden acceso desde su cuenta (en la web de IOL, sección API: "Mi cuenta" →
> "Api"). Si algo de acá no coincide con esa referencia, la referencia manda: abrí un issue.

Base: `https://api.iolinversiones.com`. Respuestas JSON; cuando vienen envueltas en `{"data": …}` el cliente
devuelve `data`. Un error es un status ≥ 400 o un objeto `{"error": {"code", "message"}}`.

Todas las llamadas llevan `Accept: application/json`, `Content-Type: application/json` y un `User-Agent` propio
(`caucionetta/<versión>`): la CDN delante de la API rechaza el User-Agent por defecto de Python.

## Autenticación

| Método | Ruta | Cuerpo | Notas |
|---|---|---|---|
| POST | `/auth/token` | `{"grant_type": "password", "username", "password"}` | devuelve `access_token`, `refresh_token`, `expires_in` |
| POST | `/auth/token` | `{"grant_type": "refresh_token", "refresh_token"}` | renueva sin la contraseña |

El access token dura poco (el cliente usa `expires_in`, o 15 min si no viene) y el refresh token, semanas (el
cliente asume 29 días). Llamadas autenticadas: `Authorization: Bearer <access_token>`. Si IOL revoca un access
token antes de que venza (401), el cliente lo descarta y reintenta **una** vez. Un 400/401/403 en el login con
contraseña marca `auth_failed` y no se reintenta hasta `caucionetta auth`.

## Endpoints permitidos (lista cerrada)

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/health` | salud |
| GET | `/broker/market-data/repos/terms?currency=ARS` | plazos de caución de la rueda: `term_id`, `term_days`, `due_date`, `min_amount`, `max_amount` |
| GET | `/broker/market-data/repos/quotes?term_id=…&currency=ARS` | libro por plazo: niveles con `ask` / `bid` (`rate`, `amount`) |
| POST | `/broker/repos/estimate` | comisiones estimadas: lista de `{fee_type, amount, vat_amount}` |
| POST | `/broker/repos` | colocar caución; header `X-Idempotency-Key: <uuid>`; devuelve `order_id` |
| DELETE | `/broker/repos/{order_id}` | cancelar una orden de caución |
| GET | `/broker/accounts/AR/balances` | `ars.available.t0`, `ars.committed.t0` |
| GET | `/broker/accounts/AR/positions?valuation_currency=ARS` | posiciones: `instrument.symbol`, `availability`, `market_value.current_price`, `profit_loss` |
| GET | `/broker/accounts/activities/operations?start_date&end_date&transaction_type=repo` | estado de las órdenes (`status`, `executed.amount`) |
| GET | `/broker/market-data/{FONDO}/bars?market=BCBA&from&to` | precio diario de un fondo, **sólo** de los listados en `bot.mm.funds` |
| POST | `/broker/mutual-funds/{subscriptions\|redemptions}/validate` | validar suscripción / rescate |
| POST | `/broker/mutual-funds/{subscriptions\|redemptions}` | suscribir monto / rescatar todo; `X-Idempotency-Key`; devuelve `transaction_id` |
| DELETE | `/broker/mutual-funds/{transaction_id}` | cancelar una suscripción pendiente |

Cualquier otra combinación de método y ruta tira `IOLError("endpoint no permitido")` antes de abrir una conexión.
El símbolo de un fondo además se valida contra `bot.mm.funds` en cada llamada.

## Cuerpo de una caución colocadora

```json
{"amount": 100000.0, "currency": "ARS", "term_id": 123, "price_type": "limit", "rate": 32.0}
```

El bot siempre manda `price_type: "limit"`.

## Cosas a tener en cuenta

- **Unidad de las tasas.** No está claro si una TNA viene como `45.5` o `0.455`. El cliente detecta la unidad una
  vez, con la mediana de un libro entero (una caución prácticamente nunca paga menos de 3 % TNA), la guarda y la
  usa también al mandar órdenes.
- **Lados del libro.** Se espera que `ask` sea el tomador. Si en tu cuenta los tomadores aparecen por encima de
  los colocadores en casi todas las lecturas, el bot avisa y ese día no manda nada; `bot.book_sides = "swapped"`
  lee los lados al revés.
- **Plazos por día hábil.** IOL publica un plazo por día hábil, así que la posición de un plazo en la lista son sus
  días hábiles. Antes de un fin de semana largo, el plazo de 1 día hábil tiene varios días corridos.
- **Idempotencia.** Las órdenes llevan una clave UUID guardada antes de mandar; reintentar con la misma clave no
  duplica la orden.
- **Cupo de llamadas.** IOL puede cobrar las llamadas pasado un cupo mensual (mirá su tabla de comisiones vigente).
  El cliente las cuenta en `<estado>/calls.json` y avisa al llegar a `api.max_calls_month`; no bloquea.

## Fuente externa (no IOL)

`caucionetta history` y el bot, después del cierre, leen la historia pública de cauciones de A3 Mercados (ex MAE):
`GET https://api.marketdata.mae.com.ar/api/mercado/titulo/historicocauciones?oTitulo={"fechaDesde","fechaHasta"}`.
No necesita cuenta. Se usa sólo como referencia hasta que el bot junta lecturas propias.
