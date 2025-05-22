from flask import Flask, request
from binance.client import Client
from binance.enums import *
import json
import sqlite3
import os
from datetime import datetime
import logging

# Konfiguracja Flask
app = Flask(__name__)

# Konfiguracja logowania
logging.basicConfig(filename='trading_bot.log', level=logging.INFO)

# Konfiguracja Binance (klucze z zmiennych środowiskowych na Replit)
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
client = Client(api_key, api_secret)

# SQLite do przechowywania danych
conn = sqlite3.connect('trading_data.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value REAL
                 )''')
cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER,
                    symbol TEXT,
                    price REAL,
                    quantity REAL,
                    tp_price REAL,
                    status TEXT
                 )''')
conn.commit()

# Inicjalizacja zmiennych strategii
def init_state():
    defaults = {
        'adjustedCapital': 10000.0,  # Startowy kapitał (euro)
        'accumulatedBTC': 0.0,
        'currentTP': 0.0,
        'tradeActive': 1.0  # 1 = true, 0 = false
    }
    for key, value in defaults.items():
        cursor.execute('INSERT OR IGNORE INTO state (key, value) VALUES (?, ?)', (key, value))
    conn.commit()

init_state()

# Funkcja do aktualizacji zmiennych
def update_state(key, value):
    cursor.execute('UPDATE state SET value = ? WHERE key = ?', (value, key))
    conn.commit()

# Funkcja do pobierania zmiennych
def get_state(key):
    cursor.execute('SELECT value FROM state WHERE key = ?', (key,))
    result = cursor.fetchone()
    return result[0] if result else None

# Funkcja do zapisu zlecenia
def save_order(order_id, symbol, price, quantity, tp_price, status='OPEN'):
    cursor.execute('INSERT INTO orders (order_id, symbol, price, quantity, tp_price, status) VALUES (?, ?, ?, ?, ?, ?)',
                  (order_id, symbol, price, quantity, tp_price, status))
    conn.commit()

# Endpoint webhooka
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        action = data.get('action')
        symbol = data.get('symbol', 'BTCUSDT')
        price = float(data.get('price', 0))
        quantity = float(data.get('quantity', 0))
        tp_price = float(data.get('takeProfit', 0))

        logging.info(f"Odebrano webhook: {data}")

        # Sprawdzenie tradeActive
        trade_active = get_state('tradeActive')
        if trade_active == 0 and not action.startswith('Close'):
            logging.info("TradeActive = false, pomijanie zlecenia.")
            return "TradeActive = false", 200

        # Obsługa zleceń
        if action.startswith('Buy Fib'):
            # Ustaw zlecenie kupna
            order = client.create_order(
                symbol=symbol,
                side=SIDE_BUY,
                type=ORDER_TYPE_LIMIT,
                timeInForce=TIME_IN_FORCE_GTC,
                price=price,
                quantity=quantity
            )
            order_id = order['orderId']
            logging.info(f"Utworzono zlecenie kupna: {order_id}, cena: {price}, ilość: {quantity}")

            # Ustaw TP (40% pozycji)
            if tp_price > 0:
                client.create_order(
                    symbol=symbol,
                    side=SIDE_SELL,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    price=tp_price,
                    quantity=quantity * 0.4
                )
                save_order(order_id, symbol, price, quantity, tp_price)
                logging.info(f"Ustawiono TP dla {order_id}, cena TP: {tp_price}")

        elif action.startswith('TP Fib'):
            # Aktualizacja TP
            current_tp = get_state('currentTP')
            if tp_price > 0 and tp_price != current_tp:
                update_state('currentTP', tp_price)
                # Anuluj stare TP
                cursor.execute('SELECT order_id, symbol, quantity FROM orders WHERE status = ?', ('OPEN',))
                open_orders = cursor.fetchall()
                for order_id, symbol, quantity in open_orders:
                    client.cancel_order(symbol=symbol, orderId=order_id)
                    # Ustaw nowe TP
                    client.create_order(
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_LIMIT,
                        timeInForce=TIME_IN_FORCE_GTC,
                        price=tp_price,
                        quantity=quantity * 0.4
                    )
                    cursor.execute('UPDATE orders SET tp_price = ? WHERE order_id = ?', (tp_price, order_id))
                    conn.commit()
                logging.info(f"Zaktualizowano TP do: {tp_price}")

        elif action == 'Close-all on first TP fill':
            # Zamknij wszystkie pozycje
            cursor.execute('SELECT order_id, symbol FROM orders WHERE status = ?', ('OPEN',))
            open_orders = cursor.fetchall()
            for order_id, symbol in open_orders:
                client.cancel_order(symbol=symbol, orderId=order_id)
                cursor.execute('UPDATE orders SET status = ? WHERE order_id = ?', ('CLOSED', order_id))
            conn.commit()
            update_state('tradeActive', 0.0)
            logging.info("Zamknięto wszystkie pozycje, tradeActive = false")

        # Śledzenie zysków i accumulatedBTC
        closed_trades = client.get_my_trades(symbol=symbol)
        if closed_trades:
            last_trade = closed_trades[-1]
            if last_trade['isBuyer'] == False:  # Sprzedaż (TP)
                profit = float(last_trade['price']) * float(last_trade['qty']) * 0.4
                update_state('adjustedCapital', get_state('adjustedCapital') + profit)
                update_state('accumulatedBTC', get_state('accumulatedBTC') + float(last_trade['qty']) * 0.4)
                logging.info(f"Zrealizowano zysk: {profit}, accumulatedBTC: {get_state('accumulatedBTC')}")

        return "Webhook processed", 200

    except Exception as e:
        logging.error(f"Błąd: {str(e)}")
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
