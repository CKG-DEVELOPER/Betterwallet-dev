from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

print("BetterWallet AI Assistant")
print("Type 'quit' to exit")
print("-" * 30)

while True:
    user_input = input("You: ")

    if user_input.lower() == "quit":
        print("Goodbye!")
        break

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful financial assistant for BetterWallet, a Nigerian fintech app."},
            {"role": "user", "content": user_input}
        ]
    )

    reply = response.choices[0].message.content
    print(f"Bot: {reply}")
    print()
