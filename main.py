import os
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from binance.client import Client
from binance.enums import *
import json
from typing import Optional
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('trading_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Binance API credentials (stored in Koyeb environment variables)
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

# Initialize FastAPI app
app = FastAPI()

# Initialize Binance client
try:
    binance_client = Client(API_KEY, API_SECRET)
    logger.info("Connected to Binance API successfully")
except Exception as e:
    logger.error(f"Failed to initialize Binance client: {str(e)}")
    raise Exception("Binance client initialization failed")

# Pydantic model for webhook payload
class WebhookData(BaseModel):
    action: str
    symbol: str
    price: str
    quantity: str
    takeProfit: Optional[str] = None

# Fibonacci take-profit levels (example mapping, adjust based on your strategy)
FIB_LEVELS = {
    "200": 2.0,  # 200% Fibonacci extension
    "300": 3.0,
    "400": 4.0,
    "500": 5.0,
    "600": 6.0,
    "700": 7.0
}

def calculate_take_profit(action: str, price: float) -> float:
    """Calculate take-profit price based on action and entry price."""
    try:
        if "TP Fib" in action:
            fib_level = action.split("TP Fib ")[1]
            multiplier = FIB_LEVELS.get(fib_level, 1.0)
            return price * (1 + multiplier / 100)  # Example: Increase price by fib level percentage
        return price  # Default to entry price if no valid fib level
    except Exception as e:
        logger.error(f"Error calculating take-profit for action {action}: {str(e)}")
        raise

def place_buy_order(symbol: str, quantity: float, price: float, take_profit: float):
    """Place a buy order with a take-profit limit order."""
    try:
        # Place market buy order
        order = binance_client.create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=quantity
        )
        logger.info(f"Buy order placed: {order}")

        # Place take-profit limit order
        tp_order = binance_client.create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=quantity,
            price=take_profit
        )
        logger.info(f"Take-profit order placed: {tp_order}")
        return order, tp_order
    except Exception as e:
        logger.error(f"Error placing order for {symbol}: {str(e)}")
        raise

def close_all_positions(symbol: str):
    """Close all open positions for a symbol."""
    try:
        # Cancel all open orders
        binance_client.cancel_open_orders(symbol=symbol)
        logger.info(f"All open orders canceled for {symbol}")

        # Get current position
        account = binance_client.get_account()
        for asset in account['balances']:
            if asset['asset'] == symbol.split("USDC")[0]:  # e.g., BTC from BTCUSDC
                quantity = float(asset['free'])
                if quantity > 0:
                    order = binance_client.create_order(
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_MARKET,
                        quantity=quantity
                    )
                    logger.info(f"Closed position for {symbol}: {order}")
    except Exception as e:
        logger.error(f"Error closing positions for {symbol}: {str(e)}")
        raise

@app.post("/webhook")
async def webhook(data: WebhookData):
    """Handle incoming webhook from TradingView."""
    logger.info(f"Otrzymano dane webhooka: {data.dict()}")

    try:
        # Validate and parse data
        price = float(data.price)
        quantity = float(data.quantity)
        symbol = data.symbol.upper()

        # Handle take-profit
        take_profit = data.takeProfit
        if take_profit == "{{strategy.order.exit}}":
            # Calculate take-profit based on action
            take_profit = calculate_take_profit(data.action, price)
        else:
            take_profit = float(take_profit) if take_profit else price

        # Process actions
        if "Buy Fib" in data.action:
            order, tp_order = place_buy_order(symbol, quantity, price, take_profit)
            return {"status": "success", "order": order, "tp_order": tp_order}

        elif "TP Fib" in data.action:
            # Take-profit order already set in buy action; log for reference
            logger.info(f"Take-profit signal received for {symbol} at {take_profit}")
            return {"status": "success", "message": "Take-profit noted"}

        elif "Close-all" in data.action:
            close_all_positions(symbol)
            return {"status": "success", "message": f"All positions closed for {symbol}"}

        else:
            logger.error(f"Unknown action: {data.action}")
            raise HTTPException(status_code=400, detail=f"Unknown action: {data.action}")

    except ValueError as ve:
        logger.error(f"Błąd w webhooku: {str(ve)}")
        raise HTTPException(status_code=500, detail=str(ve))
    except Exception as e:
        logger.error(f"Błąd w webhooku: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint for Koyeb."""
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
