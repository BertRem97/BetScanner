#!/usr/bin/env python3
"""
Entry point for the Telegram bot / scanner.
All logic lives in value_bet_scanner.py.
"""

import os
import sys
from dotenv import load_dotenv
load_dotenv()

from value_bet_scanner import ValueBetScanner, load_config


def main():
    config = load_config()

    if not config.get('telegram_bot_token'):
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    scanner = ValueBetScanner(config)
    scanner.run_interactive()


if __name__ == '__main__':
    main()
