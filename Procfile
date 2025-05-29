web: gunicorn -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:8000
worker: celery -A main.celery worker --loglevel=info --concurrency=6 -Q default,take_profit
