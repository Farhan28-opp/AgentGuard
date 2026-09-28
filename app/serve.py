"""Production entry point: ``python -m app.serve``.

Binds to 0.0.0.0 and the platform-provided PORT (Railway sets it), so no
shell variable expansion is needed in the start command. Local development
can keep using uvicorn directly (see README "Local setup").
"""
import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        workers=int(os.environ.get("WEB_CONCURRENCY", "1")),
    )


if __name__ == "__main__":
    main()
