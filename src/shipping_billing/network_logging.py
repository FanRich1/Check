"""Keep SDK wire diagnostics out of application logs, including in DEBUG mode."""

import logging


def quiet_wire_logs():
    for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2", "langchain_openai", "langsmith"):
        logging.getLogger(name).setLevel(logging.WARNING)
