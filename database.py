import os

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("Supabase URL:", SUPABASE_URL)
print("Supabase Key:", SUPABASE_KEY)
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)
