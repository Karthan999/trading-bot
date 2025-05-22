import logging
from flask import Flask, request
from binance.client import Client
import sqlite3
import os

app = Flask(__name__)
logging.basicConfig(filename='trading_bot.log', level=logging.INFO)

client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'))

def get_db_connection():
    conn = sqlite3.connect('trading_data.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            symbol TEXT,
            quantity REAL,
            price REAL,
            tp_price REAL,
            status TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value REAL
        )''')
        conn.commit()

@app.route('/')
def home():
    return "Trading Bot is running! Use /webhook for TradingView alerts."

@app.route('/test')
def test():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM state')
        state = {row['key']: row['value'] for row in cursor.fetchall()}
        conn.close()
        return f"Bot status: Running, Current TP: {state.get('currentTP', 'None')}, TradeActive: {state.get('tradeActive', 'None')}"
    except Exception as e:
        logging.error(f"Test endpoint error: {str(e)}")
        return f"Error: {str(e)}"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        logging.info(f"Odebrano webhook: {data}")
        if not data:
            logging.error("Brak danych w webhooku")
            return "Brak danych", 400

        action = data.get('action')
        symbol = data.get('symbol')
        price = float(data.get('price', 0))
        quantity = float(data.get('quantity', 0))
        tp_price = float(data.get('takeProfit', 0))

        conn = get_db_connection()
        cursor = conn.cursor()

        if action.startswith('Buy Fib'):
            order = client.create_order(
                symbol=symbol,
                side='BUY',
                type='LIMIT',
                price=price,
                quantity=quantity
            )
            cursor.execute('INSERT INTO orders (order_id, symbol, quantity, price, tp_price, status) VALUES (?, ?, ?, ?, ?, ?)',
                          (order['orderId'], symbol, quantity, price, tp_price, 'OPEN'))
            conn.commit()
            logging.info(f"Zlecenie kupna utworzone: order_id={order['orderId']}")

        elif action.startswith('TP Fib'):
            cursor.execute('SELECT value FROM state WHERE key = ?', ('currentTP',))
            current_tp = cursor.fetchone()
            current_tp = current_tp['value'] if current_tp else 0
            if tp_price > 0 and tp_price != current_tp:
                cursor.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', ('currentTP', tp_price))
                cursor.execute('SELECT order_id, symbol, quantity FROM orders WHERE status = ?', ('OPEN',))
                open_orders = cursor.fetchall()
                for order_id, symbol, quantity in open_orders:
                    client.cancel_order(symbol=symbol, orderId=order_id)
                    client.create_order(
                        symbol=symbol,
                        side='SELL',
                        type='LIMIT',
                        price=tp_price,
                        quantity=quantity * 0.4  # 40% pozycji
                    )
                    cursor.execute('UPDATE orders SET tp_price = ? WHERE order_id = ?', (tp_price, order_id))
                conn.commit()
                logging.info(f"Zaktualizowano TP do: {tp_price}")

        elif action == 'Close-all on first TP fill':
            cursor.execute('SELECT order_id, symbol FROM orders WHERE status = ?', ('OPEN',))
            open_orders = cursor.fetchall()
            for order_id, symbol in open_orders:
                client.cancel_order(symbol=symbol, orderId=order_id)
                cursor.execute('UPDATE orders SET status = ? WHERE order_id = ?', ('CLOSED', order_id))
            cursor.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', ('tradeActive', 0.0))
            conn.commit()
            logging.info("Zamknięto wszystkie pozycje")

        conn.close()
        return "Webhook processed", 200
    except Exception as e:
        logging.error(f"Błąd w webhooku: {str(e)}")
        return f"Błąd: {str(e)}", 500

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
