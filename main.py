@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        data = request.get_json()
        if not data:
            logging.error("Brak danych w webhooku")
            return "Brak danych", 400

        logging.info(f"Odebrano webhook: {data}")
        
        # Walidacja danych
        required_fields = ['symbol', 'action', 'price', 'takeProfit']
        for field in required_fields:
            if field not in data:
                logging.error(f"Brak wymaganego pola: {field}")
                return f"Brak pola: {field}", 400

        if data.get('symbol') != 'BTCUSDT':
            logging.error(f"Nieprawidłowy symbol: {data.get('symbol')}")
            return f"Nieprawidłowy symbol: {data.get('symbol')}", 400

        base_symbol = data.get('symbol')
        try:
            base_price = float(data.get('price'))
            base_take_profit = float(data.get('takeProfit'))
        except (ValueError, TypeError) as e:
            logging.error(f"Błąd konwersji ceny lub TP: {str(e)}")
            return f"Błąd konwersji ceny lub TP: {str(e)}", 400

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
