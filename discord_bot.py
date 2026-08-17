#!/usr/bin/env python3
"""
Discord Bot Utility Module

Reusable module for sending Discord notifications via bot token or webhook.
Can be imported and used in any Python script.

Usage:
    from discord_bot import DiscordBot

    # Using bot token
    bot = DiscordBot(bot_token="YOUR_TOKEN", channel_id="CHANNEL_ID")
    bot.send_message("Hello, Discord!")

    # Using webhook
    bot = DiscordBot(webhook_url="https://discord.com/api/webhooks/...")
    bot.send_embed(title="Status", description="Everything is working!")
"""

import os
import json
import logging
from typing import Optional, Dict, List, Any
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' module is required")
    print("Install with: pip3 install requests")
    raise


class DiscordBot:
    """
    Discord bot wrapper for sending messages and embeds.

    Supports both bot tokens and webhooks.
    """

    # Discord API endpoints
    API_BASE = "https://discord.com/api/v10"

    # Color constants for embeds
    COLORS = {
        'blue': 0x3498db,
        'blurple': 0x5865F2,
        'green': 0x2ecc71,
        'yellow': 0xf1c40f,
        'orange': 0xe67e22,
        'red': 0xe74c3c,
        'purple': 0x9b59b6,
        'gray': 0x95a5a6,
        'dark': 0x2c3e50,
    }

    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize Discord bot.

        Args:
            bot_token: Discord bot token (optional)
            channel_id: Discord channel ID (required if using bot_token)
            webhook_url: Discord webhook URL (optional)
            logger: Python logger instance (optional)

        Note: Must provide either (bot_token + channel_id) OR webhook_url
        """
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.webhook_url = webhook_url
        self.logger = logger or self._setup_default_logger()

        # Validate configuration
        if not self.webhook_url and not (self.bot_token and self.channel_id):
            raise ValueError(
                "Must provide either webhook_url OR (bot_token + channel_id)"
            )

        # Determine which method we're using
        self.use_bot = bool(self.bot_token and self.channel_id)

        if self.use_bot:
            self.logger.debug(f"Initialized Discord bot for channel {self.channel_id}")
        else:
            self.logger.debug("Initialized Discord webhook")

    def _setup_default_logger(self) -> logging.Logger:
        """Setup a basic logger if none provided."""
        logger = logging.getLogger("discord_bot")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('[%(levelname)s] %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

    def send_message(self, content: str, **kwargs) -> bool:
        """
        Send a simple text message.

        Args:
            content: Message text
            **kwargs: Additional Discord message parameters

        Returns:
            True if successful, False otherwise
        """
        payload = {
            "content": content,
            **kwargs
        }
        return self._send_payload(payload)

    def send_embed(
        self,
        title: Optional[str] = None,
        description: Optional[str] = None,
        color: Optional[str] = 'blurple',
        fields: Optional[List[Dict[str, Any]]] = None,
        footer: Optional[str] = None,
        thumbnail: Optional[str] = None,
        image: Optional[str] = None,
        **kwargs
    ) -> bool:
        """
        Send a rich embed message.

        Args:
            title: Embed title
            description: Embed description
            color: Color name or hex value (default: 'blurple')
            fields: List of field dicts with 'name', 'value', 'inline'
            footer: Footer text
            thumbnail: Thumbnail image URL
            image: Large image URL
            **kwargs: Additional embed parameters

        Returns:
            True if successful, False otherwise
        """
        embed = {}

        if title:
            embed['title'] = title
        if description:
            embed['description'] = description

        # Handle color
        if isinstance(color, str):
            embed['color'] = self.COLORS.get(color.lower(), self.COLORS['blurple'])
        else:
            embed['color'] = color

        if fields:
            embed['fields'] = fields
        if footer:
            embed['footer'] = {'text': footer}
        if thumbnail:
            embed['thumbnail'] = {'url': thumbnail}
        if image:
            embed['image'] = {'url': image}

        # Add any additional parameters
        embed.update(kwargs)

        payload = {
            "embeds": [embed]
        }

        return self._send_payload(payload)

    def send_fields_embed(
        self,
        title: str,
        fields: Dict[str, str],
        color: str = 'blurple',
        footer: Optional[str] = None,
        inline: bool = True
    ) -> bool:
        """
        Send an embed with fields (simplified version).

        Args:
            title: Embed title
            fields: Dict of field_name: field_value
            color: Color name or hex
            footer: Footer text
            inline: Whether fields should be inline

        Returns:
            True if successful, False otherwise
        """
        field_list = [
            {
                "name": name,
                "value": str(value),
                "inline": inline
            }
            for name, value in fields.items()
        ]

        return self.send_embed(
            title=title,
            fields=field_list,
            color=color,
            footer=footer
        )

    def send_notification(
        self,
        title: str,
        message: str,
        level: str = 'info',
        **kwargs
    ) -> bool:
        """
        Send a notification with automatic color based on level.

        Args:
            title: Notification title
            message: Notification message
            level: One of 'info', 'success', 'warning', 'error'
            **kwargs: Additional embed parameters

        Returns:
            True if successful, False otherwise
        """
        colors = {
            'info': 'blue',
            'success': 'green',
            'warning': 'yellow',
            'error': 'red'
        }

        icons = {
            'info': 'ℹ️',
            'success': '✅',
            'warning': '⚠️',
            'error': '❌'
        }

        color = colors.get(level, 'blue')
        icon = icons.get(level, '')

        return self.send_embed(
            title=f"{icon} {title}",
            description=message,
            color=color,
            **kwargs
        )

    def _send_payload(self, payload: Dict[str, Any]) -> bool:
        """
        Internal method to send payload via bot or webhook.

        Args:
            payload: Discord API payload

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.use_bot:
                # Use bot token
                url = f"{self.API_BASE}/channels/{self.channel_id}/messages"
                headers = {
                    "Authorization": f"Bot {self.bot_token}",
                    "Content-Type": "application/json"
                }

                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=10
                )

                if response.status_code == 200:
                    self.logger.debug("Discord message sent successfully (bot)")
                    return True
                else:
                    self.logger.error(
                        f"Discord bot API error {response.status_code}: {response.text}"
                    )
                    return False
            else:
                # Use webhook
                response = requests.post(
                    self.webhook_url,
                    json=payload,
                    timeout=10
                )

                if response.status_code == 204:
                    self.logger.debug("Discord message sent successfully (webhook)")
                    return True
                else:
                    self.logger.error(
                        f"Discord webhook error {response.status_code}: {response.text}"
                    )
                    return False

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to send Discord message: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error sending Discord message: {e}")
            return False

    @classmethod
    def from_env(cls, env_file: Optional[str] = None, logger: Optional[logging.Logger] = None):
        """
        Create DiscordBot from environment variables.

        Looks for:
        - DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID, or
        - DISCORD_WEBHOOK_URL

        Args:
            env_file: Path to .env file (optional)
            logger: Logger instance (optional)

        Returns:
            DiscordBot instance
        """
        # Load .env if provided
        if env_file and Path(env_file).exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env_file)
            except ImportError:
                pass  # dotenv not available, continue anyway

        # Get credentials from environment
        bot_token = os.getenv('DISCORD_BOT_TOKEN')
        channel_id = os.getenv('DISCORD_CHANNEL_ID')
        webhook_url = os.getenv('DISCORD_WEBHOOK_URL')

        return cls(
            bot_token=bot_token,
            channel_id=channel_id,
            webhook_url=webhook_url,
            logger=logger
        )


# Convenience function for quick one-off messages
def send_discord_message(
    message: str,
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    webhook_url: Optional[str] = None
) -> bool:
    """
    Quick function to send a single message.

    Args:
        message: Message text
        bot_token: Bot token (optional)
        channel_id: Channel ID (optional)
        webhook_url: Webhook URL (optional)

    Returns:
        True if successful, False otherwise
    """
    bot = DiscordBot(
        bot_token=bot_token,
        channel_id=channel_id,
        webhook_url=webhook_url
    )
    return bot.send_message(message)


if __name__ == "__main__":
    # Example usage when run directly
    import sys

    print("Discord Bot Utility Module")
    print("=" * 50)
    print()
    print("This module should be imported, not run directly.")
    print()
    print("Example usage:")
    print()
    print("    from discord_bot import DiscordBot")
    print()
    print("    # From environment variables")
    print("    bot = DiscordBot.from_env('.env')")
    print("    bot.send_message('Hello!')")
    print()
    print("    # Direct configuration")
    print("    bot = DiscordBot(")
    print("        bot_token='YOUR_TOKEN',")
    print("        channel_id='CHANNEL_ID'")
    print("    )")
    print("    bot.send_embed(")
    print("        title='Status Report',")
    print("        description='All systems operational',")
    print("        color='green'")
    print("    )")
    print()
    print("See discord_bot_example.py for more examples.")
