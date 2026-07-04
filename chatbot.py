from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_chat_reply(user_message):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful financial assistant for BetterWallet, a Nigerian fintech app that sells airtime, data and gift cards."},
            {"role": "user", "content": user_message}
        ]
    )
    return response.choices[0].message.content