import requests

TOKEN = "8452289327:AAHsH96cPi2IIWE90Fv48w2kTe9Gx_5P9qU"
CHAT_ID = "2131188727"

def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    response = requests.post(url, data=payload)
    print(response.json())  # 🔍 print Telegram’s reply

send_message("🚀 Hello Medic Coin! Your Solana bot is now alive and talking to you.")

