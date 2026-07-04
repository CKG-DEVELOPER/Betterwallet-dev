def save_user(name , email):
    file = open("users.txt", "a")
    file.write(name + "  - " + email + "\n")
    file.close()
    print("user saved sucessfully!")

def view_users():
    file = open("users.txt" , "r")
    print(file.read())
    file.close()
name = input("enter name: ")
email = input("email: ")
referral code = input("enter your referral code")
save_user(name, email)
view_users()