def create_account():
    name = input("enter your name: ")
    email = input("enter your email ")
    gender = input("enter your gender ")
    bvn = input("enter you bank verification number ") 
    balance = 0
    print("account created for " + name +"!")
    print("your balance is: " + str(balance))
    return name , balance

def deposit(balance):
    amount = int(input("How much to deposit? "))
    balance = balance + amount
    print("new balance: " + str(balance))

name, balance = create_account()
deposit(balance)