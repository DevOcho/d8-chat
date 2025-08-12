from app import create_app

app = create_app()

if __name__ == "__main__":
    # Using debug=True will enable auto-reloading when files change
    # and provide a debugger for errors.
    # In a production environment, you would use a proper WSGI server like Gunicorn.
    app.run(debug=True, host="0.0.0.0", port=5001)
