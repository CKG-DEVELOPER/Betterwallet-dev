from openai import OpenAI
client = OpenAI(api_key="YOUR API-KEY HERE")
print("Betterwallet AI Assistant")
print("type 'quit' to exit")
print("-" * 30)

while True:
    user_input = input("you: ")

    if user_input.lower() == "quit":
        print("goodbye!")
        break

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "you are a helpfull financial assistatn for betterwallet"} ,
            {"role": "user", "content": user_input}

        ]
    )                
    reply = response.choices[0].message.content
    print(f"bot: {reply}")
    print()