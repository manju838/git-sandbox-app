def main() -> None:
    """Start the sandbox web server on http://127.0.0.1:8000."""
    import uvicorn

    from .main import app

    print("Git sandbox running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8000)
