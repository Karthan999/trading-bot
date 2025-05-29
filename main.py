import os
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from binance.client import Client
from binance.enums import *
from celery import Celery
from dotenv import load_dotenv
import redis

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
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
REDIS_URL = os.getenv("REDIS_URL")

# Test Redis connection
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logger.info("Connected to Redis successfully")
except Exception as e:
    logger.error(f"Failed to connect to Redis: {str(e)}")
    raise Exception("Redis connection failed")

# Initialize FastAPI
app = FastAPI()

# Initialize Celery
celery = Celery(
    'main',
    broker=REDIS_URL,
    backend=REDIS_URL,
    broker_connection_retry_on_startup=True
)
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_default_queue='default',
    task_queues={
        'default': {'exchange': 'default', 'routing_key': 'default'},
        'take_profit': {'exchange': 'take_profit', 'routing_key': 'take_profit'}
    },
    broker_connection_max_retries=5,
    broker_pool_limit=10
)

# Initialize Binance client
try:
    binance_client = Client(API_KEY, API_SECRET)
    logger.info("Connected to Binance API")
except Exception as e:
    logger.error(f"Failed to initialize Binance client: {str(e)}")
    raise Exception("Binance client initialization failed")

# Webhook payload model
class WebhookData(BaseModel):
    action: str
    symbol: str
    price: str
    quantity: str
    takeProfit: str

@celery.task(queue='default')
def place_buy_order(symbol: str, quantity: float, price: float, take_profit: float):
    """Place a buy limit order with a take-profit limit order."""
    try:
        # Place limit buy order
        order = binance_client.create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=quantity,
            price=price
        )
        logger.info(f"Buy order placed: {order}")

        # Place take-profit limit order (40% as per sellPercentage)
        tp_order = binance_client.create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=quantity * 0.4,
            price=take_profit
        )
        logger.info(f"Take-profit order placed: {tp_order}")
        return {"status": "success", "order": order, "tp_order": tp_order}
    except Exception as e:
        logger.error(f"Error placing order for {symbol}: {str(e)}")
        raise

@celery.task(queue='take_profit')
def update_take_profit(symbol: str, take_profit: float):
    """Update take-profit for all open positions."""
    try:
        # Cancel existing TP orders
        binance_client.cancel_open_orders(symbol=symbol)
        logger.info(f"Canceled existing orders for {symbol}")

        # Get open positions
        account = binance_client.get_account()
        for asset in account['balances']:
            if asset['asset'] == symbol.split("USDC")[0]:
                quantity = float(asset['free']) * 0.4  # 40% of position
                if quantity > 0:
                    tp_order = binance_client.create_order(
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_LIMIT,
                        timeInForce=TIME_IN_FORCE_GTC,
                        quantity=quantity,
                        price=take_profit
                    )
                    logger.info(f"Updated TP for {symbol} to {take_profit}: {tp_order}")
        return {"status": "success", "message": f"Updated TP to {take_profit} for {symbol}"}
    except Exception as e:
        logger.error(f"Error updating TP for {symbol}: {str(e)}")
        raise

@celery.task(queue='default')
def close_all_positions(symbol: str):
    """Close all open positions and cancel orders."""
    try:
        binance_client.cancel_open_orders(symbol=symbol)
        logger.info(f"Canceled all open orders for {symbol}")
        account = binance_client.get_account()
        for asset in account['balances']:
            if asset['asset'] == symbol.split("USDC")[0]:
                quantity = float(asset['free'])
                if quantity > 0:
                    order = binance_client.create_order(
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_MARKET,
                        quantity=quantity
                    )
                    logger.info(f"Closed position for {symbol}: {order}")
        return {"status": "success", "message": f"Closed all positions for {symbol}"}
    except Exception as e:
        logger.error(f"Error closing positions for {symbol}: {str(e)}")
        raise

@app.post("/webhook")
async def webhook(data: WebhookData):
    """Handle TradingView webhook."""
    logger.info(f"Otrzymano dane webhooka: {data.dict()}")
    try:
        price = float(data.price)
        quantity = float(data.quantity)
        take_profit = float(data.takeProfit)
        symbol = data.symbol.upper()

        if "Buy Fib" in data.action:
            task = place_buy_order.apply_async(args=[symbol, quantity, price, take_profit], queue='default')
            return {"status": "queued", "task_id": task.id}

        elif "TP Fib" in data.action:
            task = update_take_profit.apply_async(args=[symbol, take_profit], queue='take_profit')
            return {"status": "queued", "task_id": task.id}

        elif "Close-all" in data.action:
            task = close_all_positions.apply_async(args=[symbol], queue='default')
            return {"status": "queued", "task_id": task.id}

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
    """Health check for Koyeb."""
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
