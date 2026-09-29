# greeting.py
import random

# Special friends get their own custom message.
# Key = username (lowercase, no @), Value = their special greeting.
SPECIAL_FRIENDS = {
    "alex": "Look who's here — it's @USERNAME! The room just got better. 💖",
    "renu_21": "Hey @USERNAME✨ nice seeing you! 👻👻",
    "sweet_potato123": "Hey @USERNAME✨Good to see you in the room!🍫🍬",
    "lokii_ii": "@USERNAME👑 Master, good to see you! ✨",
    # add more special friends here, e.g. "priya": "Your custom message @USERNAME"
}

# Everyone else gets the same generic message.
GENERIC_WELCOME = "Hey!@USERNAME Welcome to the room!✨🥰"

def generate_greeting(username: str) -> str:
    if not username:
        username = "Guest"
    clean_name = username.strip().replace("@", "")
    key = clean_name.lower()

    if key in SPECIAL_FRIENDS:
        msg = SPECIAL_FRIENDS[key]
    else:
        msg = GENERIC_WELCOME

    return msg.replace("@USERNAME", "@" + clean_name) 