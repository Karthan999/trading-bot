from flask import Flask, request
from binance.client import Client
import logging
import os

app = Flask(__name__)
logging.basicConfig(filename='trading_bot.log', level=logging.INFO)
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'))

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
        if symbol != 'BTCUSDC':
            logging.error(f"Nieprawidłowy symbol: {symbol}")
            return f"Nieprawidłowy symbol: {symbol}", 400
        if action.startswith('Buy Fib'):
            client.create_order(
                symbol=symbol,
                side=Client.SIDE_BUY,
                type=Client.ORDER_TYPE_LIMIT,
                timeInForce=Client.TIME_IN_FORCE_GTC,
                price=data['price'],
                quantity=data['quantity']
            )
            logging.info(f"Zlecenie kupna utworzone: {action}")
        elif action.startswith('TP Fib'):
            client.create_order(
                symbol=symbol,
                side=Client.SIDE_SELL,
                type=Client.ORDER_TYPE_LIMIT,
                timeInForce=Client.TIME_IN_FORCE_GTC,
                price=data['takeProfit'],
                quantity=data['quantity']
            )
            logging.info(f"Ustawiono TP: {data['takeProfit']}")
        elif action == 'Close-all on first TP fill':
            # Kod zamykania pozycji
            logging.info("Zamknięto wszystkie pozycje")
        # Zapis do SQLite (jeśli masz)
        return "Webhook processed", 200
    except Exception as e:
        logging.error(f"Błąd w webhooku: {str(e)}")
        return f"Błąd: {str(e)}", 500

@app.route('/test')
def test():
    # Twój kod dla /test
    return "Bot status: Running, Current TP: None, TradeActive: None"
