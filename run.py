"""Development entry point: ``uv run python run.py``.

Do not use this in production; run ``gunicorn "app:create_app()"`` instead (see README).
"""

import os

from app import create_app

if __name__ == "__main__":
    app = create_app(os.environ.get("APP_ENV", "development"))
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        use_reloader=False,
    )
