import logging
import os
import sqlite3
import json
import asyncio
import ccxt.async_support as ccxt
from flask import Flask, request
from celery import Celery
from redis import Redis
from cryptography.fernet import Fernet
from datetime import datetime

# Logowanie na stdout (żeby było widoczne w Koyeb)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# Konfiguracja Celery
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
celery = Celery('main', broker=redis_url, backend=redis_url)
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_queues={
        'default': {'exchange': 'default'},
        'take_profit': {'exchange': 'take_profit'}
    }
)

# Redis do cache'owania cen
redis_client = Redis.from_url(redis_url, decode_responses=True)

# Klucz do szyfrowania kluczy API
encryption_key = os.getenv('ENCRYPTION_KEY', Fernet.generate_key())
fernet = Fernet(encryption_key)

def encrypt_key(key):
    return fernet.encrypt(key.encode()).decode()

def decrypt_key(encrypted_key):
    try:
        return fernet.decrypt(encrypted_key.encode()).decode()
    except Exception as e:
        logging.error(f"Błąd deszyfrowania klucza: {str(e)}")
        return None

def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id TEXT PRIMARY KEY, api_key TEXT, api_secret TEXT, exchange TEXT,
                  initial_capital REAL, preferred_pair TEXT, subscribed INTEGER DEFAULT 1)''')
    conn.commit()
    conn.close()

    conn = sqlite3.connect('trading_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, exchange TEXT, symbol TEXT,
                  action TEXT, price REAL, take_profit REAL, quantity REAL, status TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

def get_subscribed_users():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT user_id, api_key, api_secret, exchange, initial_capital, preferred_pair FROM users WHERE subscribed = 1")
    users = c.fetchall()
    conn.close()
    return users

def log_trade(user_id, exchange, symbol, action, price, take_profit, quantity, status):
    conn = sqlite3.connect('trading_data.db')
    c = conn.cursor()
    timestamp = datetime.utcnow().isoformat()
    c.execute("INSERT INTO trades (user_id, exchange, symbol, action, price, take_profit, quantity, status, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (user_id, exchange, symbol, action, price, take_profit, quantity, status, timestamp))
    conn.commit()
    conn.close()

def map_symbol(exchange, preferred_pair, base_symbol):
    return preferred_pair if preferred_pair else base_symbol

async def get_current_price(exchange_name, symbol, api_key, api_secret, cache_ttl):
    cache_key = f"price:{exchange_name}:{symbol}"
    cached_price = redis_client.get(cache_key)
    if cached_price:
        return float(cached_price)

    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
    })

    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = ticker['last']
        redis_client.setex(cache_key, cache_ttl, price)
        return price
    except Exception as e:
        logging.error(f"Błąd pobierania ceny z {exchange_name}: {str(e)}")
        return None
    finally:
        await exchange.close()

def calculate_quantity(capital, price, risk_percentage=0.01):
    try:
        risk_amount = capital * risk_percentage
        quantity = risk_amount / price
        return round(quantity, 8)
    except Exception as e:
        logging.error(f"Błąd obliczania ilości: {str(e)}")
        return 0

@celery.task
def process_order(user_id, exchange_name, api_key, api_secret, symbol, action, price, take_profit, quantity):
    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
    })

    try:
        if 'Buy Fib' in action:
            order = exchange.create_limit_buy_order(symbol, quantity, price)
            logging.info(f"Ustawiono zlecenie BUY LIMIT: {order} dla {user_id} na {exchange_name}")
            log_trade(user_id, exchange_name, symbol, action, price, take_profit, quantity, 'open')
            order = exchange.create_limit_sell_order(symbol, quantity, take_profit)
            logging.info(f"Ustawiono zlecenie SELL LIMIT (TP): {order} dla {user_id} na {exchange_name}")
            log_trade(user_id, exchange_name, symbol, f"TP for {action}", take_profit, take_profit, quantity, 'open')
        elif 'TP Fib' in action:
            open_trades = exchange.fetch_open_orders(symbol)
            for trade in open_trades:
                if trade['side'] == 'buy' and trade['price'] <= price:
                    exchange.cancel_order(trade['id'], symbol)
                    logging.info(f"Anulowano zlecenie BUY: {trade['id']} dla {user_id} na {exchange_name}")
                    log_trade(user_id, exchange_name, symbol, f"Cancelled {action}", price, take_profit, quantity, 'cancelled')
        elif action == 'Close-all on first TP fill':
            open_trades = exchange.fetch_open_orders(symbol)
            for trade in open_trades:
                exchange.cancel_order(trade['id'], symbol)
                logging.info(f"Anulowano zlecenie: {trade['id']} dla {user_id} na {exchange_name}")
                log_trade(user_id, exchange_name, symbol, action, price, take_profit, trade['amount'], 'cancelled')
    except Exception as e:
        logging.error(f"Błąd zlecenia dla {user_id} na {exchange_name}: {str(e)}")
        log_trade(user_id, exchange_name, symbol, action, price, take_profit, quantity, f'error: {str(e)}')
    finally:
        exchange.close()

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        logging.info(f"Otrzymano dane webhooka: {data}")

        action = data.get('action')
        price = float(data.get('price'))
        take_profit = float(data.get('take_profit'))

        # Dane konfiguracyjne z ENV
        encrypted_api_key = os.getenv('ENCRYPTED_API_KEY')
        encrypted_api_secret = os.getenv('ENCRYPTED_API_SECRET')
        api_key = decrypt_key(encrypted_api_key)
        api_secret = decrypt_key(encrypted_api_secret)

        exchange = os.getenv('EXCHANGE', 'binance')
        capital = float(os.getenv('CAPITAL', 1000))  # możesz też na sztywno ustawić
        symbol = os.getenv('SYMBOL', 'BTC/USDC')     # lub wpisz np. 'BTC/USDC'

        quantity = calculate_quantity(capital, price)

        # Uruchom Celery zlecenie
        process_order.delay("single_user", exchange, api_key, api_secret, symbol, action, price, take_profit, quantity)

        return f"[{action}] na {symbol} wysłane do giełdy", 200
    except Exception as e:
        logging.error(f"Błąd w webhooku: {str(e)}")
        return f"Błąd: {str(e)}", 500


@app.route('/test')
async def test():
    try:
        users = get_subscribed_users()
        if not users:
            return "Brak użytkowników", 400

        user = users[0]
        user_id, encrypted_api_key, encrypted_api_secret, exchange, _, preferred_pair = user
        api_key = decrypt_key(encrypted_api_key)
        api_secret = decrypt_key(encrypted_api_secret)
        symbol = map_symbol(exchange, preferred_pair, 'BTCUSDT')
        price = await get_current_price(exchange, symbol, api_key, api_secret, cache_ttl=30)
        return f"Bot status: Running, Active users: {len(users)}, {exchange} {symbol} Price: {price}", 200
    except Exception as e:
        logging.error(f"Błąd testu: {str(e)}")
        return f"Błąd: {str(e)}", 500
        
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
