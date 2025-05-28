from flask import Flask, request
from celery import Celery
from aiohttp import ClientSession
import ccxt.async_support as ccxt
import sqlite3
import logging
import redis
from cryptography.fernet import Fernet
import os
import asyncio

app = Flask(__name__)
logging.basicConfig(filename='trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

celery = Celery('tasks', broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'), backend='redis://localhost:6379/0')
celery.conf.task_queues = {
    'default': {'exchange': 'default'},
    'take_profit': {'exchange': 'take_profit'}
}
redis_client = redis.Redis(host=os.getenv('REDIS_HOST', 'localhost'), port=6379, decode_responses=True)
CIPHER_KEY = os.getenv('CIPHER_KEY') or Fernet.generate_key()
cipher = Fernet(CIPHER_KEY)

def decrypt_key(encrypted_data):
    try:
        return cipher.decrypt(encrypted_data.encode()).decode()
    except Exception as e:
        logging.error(f"Błąd deszyfrowania klucza: {str(e)}")
        return None

def get_subscribed_users():
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, api_key, api_secret, exchange, initial_capital, preferred_pair FROM users WHERE subscribed = 1")
        users = cursor.fetchall()
        conn.close()
        return users
    except Exception as e:
        logging.error(f"Błąd pobierania użytkowników: {str(e)}")
        return []

def calculate_quantity(capital, price, allocation=0.1):
    try:
        quantity = (capital * allocation) / price
        # Zaokrąglenie do 8 miejsc, ale upewniamy się, że jest powyżej minimum giełdy
        return max(round(quantity, 8), 0.0001)  # Minimum dla Binance: 0.0001 BTC
    except Exception as e:
        logging.error(f"Błąd obliczania ilości: {str(e)}")
        return 0.0

def map_symbol(exchange, preferred_pair, base_symbol='BTCUSDT'):
    exchange_pairs = {
        'binance': ['BTCUSDT', 'BTCUSDC'],
        'coinlion': ['BTCUSDC'],
        'kraken': ['BTCUSD', 'BTCUSDC'],
        'bybit': ['BTCUSDT', 'BTCUSDC'],
        'kucoin': ['BTCUSDT', 'BTCUSDC'],
        'coinbase': ['BTCUSD', 'BTCUSDC']
    }
    return preferred_pair if preferred_pair in exchange_pairs.get(exchange, []) else base_symbol

async def get_current_price(exchange, symbol, api_key, api_secret, cache_ttl=30):
    cache_key = f"{exchange}:{symbol}:price"
    cached_price = redis_client.get(cache_key)
    if cached_price:
        return float(cached_price)
    async with ClientSession() as session:
        client = getattr(ccxt, exchange)({'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True})
        try:
            ticker = await client.fetch_ticker(symbol)
            price = ticker['last']
            redis_client.setex(cache_key, cache_ttl, price)
            logging.info(f"Pobrano cenę {symbol} na {exchange}: {price}")
            return price
        except Exception as e:
            logging.error(f"Błąd pobierania ceny dla {symbol} na {exchange}: {str(e)}")
            return None
        finally:
            await client.close()

async def validate_order(exchange, symbol, api_key, api_secret, quantity, price, action):
    client = getattr(ccxt, exchange)({'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True})
    try:
        # Pobierz minimalne loty i saldo
        markets = await client.load_markets()
        market = markets[symbol]
        min_amount = market['limits']['amount']['min']
        if quantity < min_amount:
            logging.error(f"Ilość {quantity} poniżej minimum {min_amount} dla {symbol} na {exchange}")
            return False

        # Sprawdź saldo
        balance = await client.fetch_balance()
        base_currency = symbol.split('/')[1] if action.startswith('Buy') else symbol.split('/')[0]
        available = balance[base_currency]['free'] if base_currency in balance else 0
        required = quantity * price if action.startswith('Buy') else quantity
        if available < required:
            logging.error(f"Niewystarczające saldo: dostępne {available} {base_currency}, wymagane {required} dla {symbol} na {exchange}")
            return False
        return True
    except Exception as e:
        logging.error(f"Błąd walidacji zlecenia dla {symbol} na {exchange}: {str(e)}")
        return False
    finally:
        await client.close()

def log_trade(user_id, exchange, symbol, action, price, adjusted_price, quantity):
    try:
        conn = sqlite3.connect('trading_data.db')
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (user_id, exchange, symbol, action, price, adjusted_price, quantity, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (user_id, exchange, symbol, action, price, adjusted_price, quantity))
        conn.commit()
        conn.close()
        logging.info(f"Zapisano zlecenie: {action} dla {user_id} na {exchange}, qty: {quantity}, price: {price}")
    except Exception as e:
        logging.error(f"Błąd zapisu zlecenia do bazy: {str(e)}")

@celery.task(bind=True, max_retries=5, queue='default')
def process_order(self, user_id, exchange, api_key, api_secret, symbol, action, price, take_profit, quantity):
    async def execute():
        async with ClientSession() as session:
            client = getattr(ccxt, exchange)({'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True})
            try:
                # Walidacja zlecenia
                if not await validate_order(exchange, symbol, api_key, api_secret, quantity, price, action):
                    logging.error(f"Niepowodzenie walidacji zlecenia dla {user_id} na {exchange}")
                    return

                # Składanie zlecenia (limit order)
                if action.startswith('Buy Fib'):
                    order = await client.create_limit_buy_order(symbol, quantity, price)
                    logging.info(f"Ustawiono zlecenie BUY LIMIT: {order} dla {user_id} na {exchange}")
                elif action.startswith('TP Fib'):
                    order = await client.create_limit_sell_order(symbol, quantity, take_profit)
                    logging.info(f"Ustawiono zlecenie SELL LIMIT (TP): {order} dla {user_id} na {exchange}")
                elif action == 'Close-all on first TP fill':
                    await client.cancel_open_orders(symbol)
                    logging.info(f"Anulowano wszystkie otwarte zlecenia dla {symbol} na {exchange}")
                log_trade(user_id, exchange, symbol, action, price, take_profit, quantity)
            except Exception as e:
                logging.error(f"Błąd zlecenia dla {user_id} na {exchange}: {str(e)}")
                raise self.retry(countdown=60)
            finally:
                await client.close()
    asyncio.run(execute())

@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        data = request.get_json()
        logging.info(f"Odebrano webhook: {data}")
        if not data or data.get('symbol') != 'BTCUSDT':
            logging.error(f"Nieprawidłowy symbol: {data.get('symbol')}")
            return f"Nieprawidłowy symbol: {data.get('symbol')}", 400

        base_symbol = data.get('symbol', 'BTCUSDT')
        base_price = float(data.get('price'))
        base_take_profit = float(data.get('takeProfit'))
        action = data.get('action')
        users = get_subscribed_users()

        if not users:
            logging.error("Brak aktywnych użytkowników w bazie")
            return "Brak użytkowników", 400

        for user in users:
            user_id, encrypted_api_key, encrypted_api_secret, exchange, initial_capital, preferred_pair = user
            api_key = decrypt_key(encrypted_api_key)
            api_secret = decrypt_key(encrypted_api_secret)
            if not api_key or not api_secret:
                logging.error(f"Nie udało się odszyfrować kluczy API dla {user_id}")
                continue

            symbol = map_symbol(exchange, preferred_pair, base_symbol)
            cache_ttl = 10 if action.startswith('TP Fib') else 30
            current_price = await get_current_price(exchange, symbol, api_key, api_secret, cache_ttl)
            if not current_price:
                logging.error(f"Brak ceny dla {user_id} na {exchange}")
                continue

            price_ratio = current_price / base_price
            adjusted_price = base_price * price_ratio
            adjusted_take_profit = base_take_profit * price_ratio
            quantity = calculate_quantity(initial_capital, current_price)

            if quantity <= 0:
                logging.error(f"Nieprawidłowa ilość {quantity} dla {user_id} na {exchange}")
                continue

            queue = 'take_profit' if action.startswith('TP Fib') else 'default'
            process_order.apply_async(
                args=[user_id, exchange, api_key, api_secret, symbol, action, adjusted_price, adjusted_take_profit, quantity],
                queue=queue
            )
            logging.info(f"Wysłano zadanie Celery dla {user_id} na {exchange}: {action}, qty: {quantity}, price: {adjusted_price}")
        return "Webhook processed", 200
    except Exception as e:
        logging.error(f"Błąd w webhooku: {str(e)}")
        return f"Błąd: {str(e)}", 500

@app.route('/register_user', methods=['POST'])
def register_user():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        api_key = data.get('api_key')
        api_secret = data.get('api_secret')
        exchange = data.get('exchange')
        initial_capital = float(data.get('initial_capital', 100.0))
        preferred_pair = data.get('preferred_pair', 'BTCUSDT')

        encrypted_api_key = cipher.encrypt(api_key.encode()).decode()
        encrypted_api_secret = cipher.encrypt(api_secret.encode()).decode()

        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, api_key, api_secret, exchange, initial_capital, quantity, subscribed, preferred_pair)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, encrypted_api_key, encrypted_api_secret, exchange, initial_capital, 0.0, True, preferred_pair))
        conn.commit()
        conn.close()

        logging.info(f"Zarejestrowano użytkownika: {user_id} dla {exchange}, para: {preferred_pair}")
        return f"Użytkownik {user_id} zarejestrowany", 200
    except Exception as e:
        logging.error(f"Błąd rejestracji: {str(e)}")
        return f"Błąd: {str(e)}", 500

@app.route('/test')
def test():
    try:
        users = get_subscribed_users()
        binance = ccxt.binance({'enableRateLimit': True})
        binance_price = binance.fetch_ticker('BTCUSDT').get('last', 'N/A')
        status = {
            'bot_status': 'Running',
            'active_users': len(users),
            'binance_btcusdt_price': float(binance_price) if binance_price != 'N/A' else 'N/A'
        }
        logging.info(f"Test endpoint: {status}")
        return (
            f"Bot status: {status['bot_status']}, "
            f"Active users: {status['active_users']}, "
            f"Binance BTCUSDT Price: {status['binance_btcusdt_price']:.2f}"
        )
    except Exception as e:
        logging.error(f"Błąd w /test: {str(e)}")
        return f"Błąd w pobieraniu danych: {str(e)}", 500
