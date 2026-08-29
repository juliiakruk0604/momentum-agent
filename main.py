"""Railway/Railpack entrypoint.

Keeps the runtime identical to service.py: FastAPI + background market worker
when SERVICE_MODE=all.
"""
from service import main

if __name__ == "__main__":
    main()
