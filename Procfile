web: gunicorn -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:8000
worker_default: celery -A main.celery worker --loglevel=info --concurrency=4 -Q default
worker_take_profit: celery -A main.celery worker --loglevel=info --concurrency=2 -Q take_profit
