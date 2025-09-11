# d8-chat
DevOcho Chat

For testing with multiple users run it with:
gunicorn --worker-class gevent --workers 4 --bind 0.0.0.0:5001 run:app
