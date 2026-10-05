"""
Day 1: Your first Claude API call.

This is the simplest possible example: send a message, get a response.
Everything else we build (the Extractor, Mapper, Auditor) is just this
same pattern, repeated with smarter prompts.
"""

import os
from dotenv import load_dotenv
from anthropic import Anthropic

# Loads the ANTHROPIC_API_KEY from your .env file into the environment
load_dotenv()

# Creates a client - this is your connection to Claude's API
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# This is the actual API call. "messages.create" sends a conversation
# to Claude and gets a response back.
response = client.messages.create(
    model="claude-sonnet-4-5",   # which Claude model to use
    max_tokens=200,               # max length of Claude's reply
    messages=[
        {"role": "user", "content": "In one sentence, what is a compliance gap analysis?"}
    ]
)

# response.content is a list of content blocks. For a simple text reply,
# there's one block, and .text holds the actual words.
print(response.content[0].text)
