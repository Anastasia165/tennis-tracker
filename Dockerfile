FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home tracker && mkdir /app/data && chown tracker:tracker /app/data
COPY bot.py store.py reports.py date_picker.py passes.py pass_ai.py linking.py tennis_store.py navigation.py ./
USER tracker
CMD ["python", "bot.py"]
