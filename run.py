# Make psycopg2 cooperate with gevent BEFORE the app (and its DB pool) import.
# Under the gunicorn gevent worker, gevent monkey-patches the socket module
# first; without this, every psycopg2 query blocks the whole worker's event
# loop — freezing all WebSockets, the ping timer, and the pub/sub listener on
# that worker. Under the plain dev server (python run.py) gevent isn't patched,
# so this is a no-op.
try:
    from gevent import monkey

    if monkey.is_module_patched("socket"):
        from psycogreen.gevent import patch_psycopg

        patch_psycopg()
except ImportError:
    pass

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001, threaded=True)
