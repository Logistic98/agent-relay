import os

# A developer's real .env must not affect deterministic unit tests. Explicit
# Settings(...) values inside a test still have higher priority than these.
os.environ.update(
    {
        "TELEGRAM_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_ALLOWED_CHAT_IDS": "",
        "TELEGRAM_ALLOWED_USER_IDS": "",
        "TELEGRAM_FREE_TEXT_MODE": "auto",
        "API_ENABLED": "true",
        "API_BEARER_TOKEN": "",
        "API_ACTOR_ID": "api",
    }
)
