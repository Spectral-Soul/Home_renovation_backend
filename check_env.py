from dotenv import load_dotenv
import os

load_dotenv()
val = os.environ.get("OWNER_PHONE", "")
print(f"OWNER_PHONE = '{val}'")
print(f"length = {len(val)}")