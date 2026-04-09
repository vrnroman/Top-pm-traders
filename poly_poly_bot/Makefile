.PHONY: setup test lint run-preview run-live deploy backtest

setup:
	pip install -r requirements.txt

test:
	python -m pytest tests/ -v --tb=short

lint:
	ruff check src/ tests/ main.py

run-preview:
	PREVIEW_MODE=true python main.py

run-live:
	PREVIEW_MODE=false python main.py

deploy:
	bash deploy.sh

backtest:
	python -m backtest.weather_backtest
