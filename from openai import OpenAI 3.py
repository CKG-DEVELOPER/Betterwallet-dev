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
            {"role": "system", "content": "we sell airtime,data gift card across nigeria and across we are the number 1 best fintech app in nigeria, we also have job site and betterwallet verivication for online store to cop fraud"} ,
            {"role": "user", "content": user_input}

        ]
    )                
    reply = response.choices[0].message.content
    print(f"bot: {reply}")
    print()